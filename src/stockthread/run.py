"""`stockthread run`: serve Slack, and run the worker until stopped (design section 4).

Startup order: open the store and take the instance lock; put back events an earlier instance left
unfinished; check the bot token (`auth.test`) and InvenTree (version and a first catalog read); then connect
to Slack, start catch-up, and run the worker loop in this thread. The listener stores and acknowledges on
Bolt's threads, catch-up stores what Slack did not deliver on its own thread (slack/catchup.py), and the
worker processes from the queue here. Over HTTP, waitress serves Bolt on a thread of its own
(slack/http_server.py); the rest is the same for both transports.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import ssl
import threading
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, TypeVar

import anyio
import certifi
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from stockthread.applier import GateNow, approve_by_click, approve_by_policy, plan_expired, work_request
from stockthread.apply import Applied, ApplyReader, Sender, undo_apply
from stockthread.catalog import assign_vehicles, load_catalog, with_aliases
from stockthread.config import Settings
from stockthread.core.matching import Catalog
from stockthread.core.model import PostKey, RequestKey
from stockthread.core.requests import RequestState
from stockthread.core.resolve import StockItem, stock_items
from stockthread.gate import Judgement, Policy
from stockthread.inventree import InvenTreeError, InvenTreeReader, VersionCheck, check_version
from stockthread.llm.extract import LLMExtractor, WithinDeadline
from stockthread.llm.openai_compatible import OpenAICompatible
from stockthread.setup_check import Finding, Level, check_posters, check_roles, check_vehicles, lacks_catalog_view
from stockthread.slack.catchup import SWEEP_SECONDS, CatchUp, HistoryClient
from stockthread.slack.http_server import HttpServer, ServeError
from stockthread.slack.listener import IdentityError, Listener, ThreadStores, build_app, identify
from stockthread.slack.replies import MovementView
from stockthread.store import InstanceLocked, LockLost, Store, new_lock_owner
from stockthread.store.db import posters_seen
from stockthread.worker import (
    Approver,
    ClickApprover,
    PosterBusy,
    PostStillRunning,
    Stepper,
    StockReader,
    Undoer,
    Worker,
)
from stockthread.writer import InvenTreeWriter

log = logging.getLogger(__name__)

POLL_SECONDS = 1.0
LOCK_STALE_AFTER = timedelta(seconds=60)
"""Another instance may take the lock over once the heartbeat is this old."""
CATALOG_REFRESH_SECONDS = 5 * 60
CATALOG_RETRY_SECONDS = 30
CATALOG_MAX_AGE_SECONDS = 60 * 60
"""A refresh that fails keeps the last snapshot for dry runs, up to this age; after that, posts wait."""
MAX_ERROR_BACKOFF_SECONDS = 60.0
SLACK_TIMEOUT_SECONDS = 15
"""For one socket operation of a Slack call. It does not bound the call: a connection is tried on every
address in turn and once more after an error, a body can drip, and a DNS lookup has no timeout at all."""
POST_DEADLINE_SECONDS = 30.0
"""For a whole reply (DeadlinePoster). Well under LOCK_STALE_AFTER: the worker beats the lock right before
it posts."""
NOTE_DEADLINE_SECONDS = 5.0
"""For a note to one user (DeadlinePoster.chat_postEphemeral). The worker beats the lock right before each, and
sends none for a while after one failed."""
CATALOG_MARGIN_SECONDS = 30
"""A catalog read is cut off this long before the lock could go stale."""
LLM_MARGIN_SECONDS = 30
"""The lock goes stale this long after a call to the LLM provider is cut off, at the earliest."""
SWEEP_JOIN_SECONDS = 5.0
"""How long a stop waits for a catch-up sweep to end."""
SEND_MARGIN_TIMEOUTS = 2
"""An apply step or undo begins no write with less than this many InvenTree timeouts left before it is cut off."""
CATALOG_UNSET = Catalog(parts=(), locations=())
"""Placeholder until startup has read the catalog; the worker does not run before that."""


class Connection(Protocol):
    def connect(self) -> Any: ...
    def close(self) -> Any: ...


class CatalogCache:
    """The catalog snapshot for the worker, read again every few minutes.

    After a failed read, the next try waits CATALOG_RETRY_SECONDS, so an outage costs one read per interval,
    not one per post. Meanwhile the last snapshot serves, up to CATALOG_MAX_AGE_SECONDS; then the last error
    is raised again and posts wait.
    """

    def __init__(self, load: Callable[[], Catalog], clock: Callable[[], float] = time.monotonic) -> None:
        self._load = load
        self._clock = clock
        self._catalog: Catalog | None = None
        self._loaded_at = 0.0
        self._failed_at: float | None = None
        self._error: InvenTreeError | None = None

    def __call__(self) -> Catalog:
        now = self._clock()
        if self._catalog is not None and now - self._loaded_at < CATALOG_REFRESH_SECONDS:
            return self._catalog
        if self._failed_at is None or now - self._failed_at >= CATALOG_RETRY_SECONDS:
            try:
                self._catalog, self._loaded_at, self._failed_at = self._load(), now, None
                return self._catalog
            except InvenTreeError as error:
                self._failed_at, self._error = now, error
                log.warning("catalog read failed: %s", error)
        if self._catalog is None or now - self._loaded_at >= CATALOG_MAX_AGE_SECONDS:
            raise self._error or InvenTreeError("no catalog has been read yet")
        return self._catalog


class _Call:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Any = None
        self.error: Exception | None = None


class DeadlinePoster:
    """Posts on a helper thread, and gives the worker control back after `deadline` seconds.

    The worker then keeps the instance lock's heartbeat going, so no other instance takes over while a call
    hangs, and no reply is posted twice by two instances. A call that ran out of time is left running: the
    next try of the same message (thread and text) takes its result (or its error) and does not post again, and
    until it has ended nothing else is posted. A receipt and a dry-run reply in one thread have different texts, so
    neither takes the other's result.
    """

    def __init__(
        self, client: Any, deadline: float = POST_DEADLINE_SECONDS, note_deadline: float = NOTE_DEADLINE_SECONDS
    ) -> None:
        self._client = client
        self._deadline = deadline
        self._note_deadline = note_deadline
        self._side: _Call | None = None
        self._calls: dict[tuple[Any, Any, Any], _Call] = {}

    def chat_postMessage(self, **kwargs: Any) -> Any:  # slack_sdk's name
        key = (kwargs.get("channel"), kwargs.get("thread_ts"), kwargs.get("text"))
        call = self._calls.get(key)
        if call is None:
            if any(not other.done.is_set() for other in self._calls.values()):
                raise PosterBusy
            call = self._calls[key] = _Call()
            threading.Thread(
                target=self._run, args=(call, self._client.chat_postMessage, kwargs), name="slack-post", daemon=True
            ).start()
            call.done.wait(self._deadline)
        if not call.done.is_set():
            raise PostStillRunning
        del self._calls[key]
        if call.error is not None:
            raise call.error
        return call.result

    def chat_postEphemeral(self, **kwargs: Any) -> Any:  # slack_sdk's name
        """A note to one user (a side call)."""
        return self._side_call(self._client.chat_postEphemeral, kwargs)

    def conversations_replies(self, **kwargs: Any) -> Any:  # slack_sdk's name
        """Read a reply back before its buttons are taken down (a side call)."""
        return self._side_call(self._client.conversations_replies, kwargs)

    def chat_update(self, **kwargs: Any) -> Any:  # slack_sdk's name
        """Edit a reply to take its buttons down (a side call)."""
        return self._side_call(self._client.chat_update, kwargs)

    def _side_call(self, method: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
        """A call besides the replies, on its own helper thread, that may be sent twice without harm. One that
        runs out of time is left running and not tried again; until it has ended no other side call is made,
        so stuck calls never pile up. Replies never wait for side calls, nor side calls for replies."""
        if self._side is not None and not self._side.done.is_set():
            raise PosterBusy
        call = self._side = _Call()
        threading.Thread(target=self._run, args=(call, method, kwargs), name="slack-side", daemon=True).start()
        if not call.done.wait(self._note_deadline):
            raise PostStillRunning
        if call.error is not None:
            raise call.error
        return call.result

    @staticmethod
    def _run(call: _Call, method: Callable[..., Any], kwargs: dict[str, Any]) -> None:
        try:
            call.result = method(**kwargs)
        except Exception as error:
            call.error = error
        finally:
            call.done.set()


def inventree_catalog(settings: Settings) -> Callable[[], Catalog]:
    """Read the whole catalog, within a time limit: a read of many slow pages must end before another
    instance could take the lock over. A cut-off read is an outage like any other."""
    limit = (lock_stale_after(settings) - timedelta(seconds=CATALOG_MARGIN_SECONDS)).total_seconds()

    async def read() -> Catalog:
        async with InvenTreeReader(settings.inventree, settings.secrets.inventree_token) as reader:
            return await load_catalog(reader)

    return with_vehicles(lambda: read_within(read, limit), settings)


def inventree_stock(settings: Settings) -> StockReader:
    """Read the stock items of each (part, location) pair, within the same time limit as a catalog read. A row
    that is not a valid stock item is an outage like a failed read: no pick is made from a partial list."""
    limit = (lock_stale_after(settings) - timedelta(seconds=CATALOG_MARGIN_SECONDS)).total_seconds()

    def read(pairs: Sequence[tuple[int, int]]) -> dict[tuple[int, int], list[StockItem]]:
        async def rows() -> dict[tuple[int, int], list[dict[str, Any]]]:
            async with InvenTreeReader(settings.inventree, settings.secrets.inventree_token) as reader:
                return {pair: await reader.stock_items(*pair) for pair in pairs}

        found = read_within(rows, limit, "stock")
        try:
            return {pair: stock_items(listed) for pair, listed in found.items()}
        except ValueError as error:
            raise InvenTreeError(f"a stock item from InvenTree is not valid ({error})") from None

    return read


Connect = Callable[[], AbstractAsyncContextManager[tuple[ApplyReader, Sender]]]
"""Opens an InvenTree reader and writer for one apply step or undo, and closes them after it."""


@asynccontextmanager
async def _clients(settings: Settings) -> AsyncIterator[tuple[ApplyReader, Sender]]:
    token = settings.secrets.inventree_token
    async with (
        InvenTreeReader(settings.inventree, token) as reader,
        InvenTreeWriter(settings.inventree, token) as writer,
    ):
        yield reader, writer


def inventree_steps(
    settings: Settings,
    catalog: Callable[[], Catalog],
    store: Store,
    owner: str,
    *,
    beat: Callable[[], None],
    connect: Connect | None = None,
    limit: float | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[Stepper, Undoer]:
    """The worker's apply step (`applier.work_request`) and undo (`apply.undo_apply`) against InvenTree, for
    `writes.enabled`. Each call opens its own reader and writer (`connect`) and reads the server's version again: a
    server that is no longer tested for writes blocks the apply at the gate (ADR 0016) and refuses the undo. The
    vehicles are the catalog's (`catalog`, the cache), read before the step of an `approved` request only, and the
    lock is beaten (`beat`) after that read. A plan expires `slack.plan_expiry_hours` after its post, on `clock`, as
    the worker counts it: one that expired before its first write goes back to review (`applier.work_request`).

    Each call is cut off after `limit` seconds (by default before the instance lock could go stale), and begins no
    write once less than SEND_MARGIN_TIMEOUTS times `inventree.timeout_seconds` is left: a cut-off then ends a read,
    and the request stays as it was. A write cut off all the same is `unknown`, which recovery concludes (ADR 0015).
    Every call holds one lock for the whole process, so there is never more than one write in flight, whatever
    thread asks."""
    if limit is None:
        limit = (lock_stale_after(settings) - timedelta(seconds=CATALOG_MARGIN_SECONDS)).total_seconds()
    margin = SEND_MARGIN_TIMEOUTS * settings.inventree.timeout_seconds
    settle = timedelta(seconds=settings.apply.settle_seconds)
    opened = connect or (lambda: _clients(settings))
    writing = threading.Lock()

    def expired(post: PostKey) -> bool:
        at = Decimal(str(clock().timestamp()))
        return plan_expired(store, post, at, settings.slack.plan_expiry_hours)

    def step(key: RequestKey) -> Applied:
        vehicles: dict[str, int] = {}
        if store.request(key).state is RequestState.APPROVED:
            vehicles = dict(catalog().vehicles)  # outside the step's loop: a refresh of the cache runs one of its own
            beat()

        async def run() -> Applied:
            send_by = anyio.current_time() + limit - margin
            async with opened() as (reader, writer):
                now = GateNow(Policy.of(settings, check_version(await reader.server_info())), vehicles)
                return await work_request(
                    key, store=store, reader=reader, writer=writer, lock=anyio.Lock(), lock_owner=owner,
                    gate_now=lambda: now, expired=expired, settle=settle, send_by=send_by,
                )  # fmt: skip

        with writing:
            return read_within(run, limit, "apply step's")

    def undo(key: RequestKey, movement: MovementView, user: str) -> Applied:
        async def run() -> Applied:
            send_by = anyio.current_time() + limit - margin
            async with opened() as (reader, writer):
                version = check_version(await reader.server_info())
                if not version.writes_allowed:
                    return Applied(store.request(key).state, f"no undo: {version.message}")
                return await undo_apply(
                    key, movement, store=store, reader=reader, writer=writer, lock=anyio.Lock(), lock_owner=owner,
                    user=user, send_by=send_by,
                )  # fmt: skip

        with writing:
            return read_within(run, limit, "undo's")

    return step, undo


def with_vehicles(load: Callable[[], Catalog], settings: Settings) -> Callable[[], Catalog]:
    """Every snapshot gets the vehicles from the `[vehicles]` settings. A person whose vehicle is not a stock
    location in the snapshot is named in the log, once until the list changes."""
    reported: list[tuple[str, ...] | None] = [None]

    def read() -> Catalog:
        catalog, missing = assign_vehicles(load(), settings.vehicles)
        if missing != reported[0]:
            reported[0] = missing
            if missing:
                log.warning(
                    'vehicles: no stock location at the path set for %s; their "my truck" will not be matched',
                    ", ".join(missing),
                )
            elif settings.vehicles:
                log.info("vehicles: every one of the %d paths is a stock location", len(settings.vehicles))
        return catalog

    return read


_T = TypeVar("_T")


def read_within(read: Callable[[], Coroutine[Any, Any, _T]], limit: float, what: str = "catalog") -> _T:
    async def limited() -> _T:
        try:
            return await asyncio.wait_for(read(), limit)
        except TimeoutError:
            raise InvenTreeError(
                f"the {what} read took longer than {limit:.0f} s; raise inventree.timeout_seconds"
            ) from None

    return asyncio.run(limited())


def check_inventree(settings: Settings) -> VersionCheck:
    """The server's version check, which every write judgement needs (`gate.Policy`). Raises InvenTreeError when
    the server cannot be read."""

    async def read() -> VersionCheck:
        async with InvenTreeReader(settings.inventree, settings.secrets.inventree_token) as reader:
            return check_version(await reader.server_info())

    return asyncio.run(read())


def policy_approver(
    settings: Settings,
    catalog: Callable[[], Catalog],
    store: Store,
    owner: str,
    *,
    beat: Callable[[], None],
    check: Callable[[Settings], VersionCheck] = check_inventree,
) -> Approver:
    """The worker's approval by the policy (`applier.approve_by_policy`, ADR 0004), for `writes.enabled`: judged
    with the settings, the server's version read again (`check`) and the catalog's vehicles (`catalog`, the cache),
    the lock beaten (`beat`) after each read. Approving writes nothing: the write is judged again before it is sent
    (`inventree_steps`)."""

    def approve(post: PostKey) -> Judgement | None:
        vehicles = dict(catalog().vehicles)
        beat()
        version = check(settings)
        beat()
        return approve_by_policy(store, owner, post, GateNow(Policy.of(settings, version), vehicles))

    return approve


def click_approver(
    settings: Settings,
    catalog: Callable[[], Catalog],
    store: Store,
    owner: str,
    *,
    beat: Callable[[], None],
    check: Callable[[Settings], VersionCheck] = check_inventree,
) -> ClickApprover:
    """The worker's approval of a plan for an approver who clicked Apply (`applier.approve_by_click`), for
    `writes.enabled`: judged with the settings, the server's version read again (`check`) and the catalog's vehicles
    (`catalog`, the cache), the lock beaten (`beat`) after each read; the plan expires `slack.plan_expiry_hours` after
    its post, as the worker counts it. Approving writes nothing: the write is judged again before it is sent
    (`inventree_steps`)."""

    def approve(post: PostKey, revision: int, user: str, at: Decimal) -> Judgement | None:
        vehicles = dict(catalog().vehicles)
        beat()
        version = check(settings)
        beat()
        now = GateNow(Policy.of(settings, version), vehicles)
        return approve_by_click(
            store, owner, post, revision, user, now, at=at, expiry_hours=settings.slack.plan_expiry_hours
        )

    return approve


def check_setup(settings: Settings) -> list[Finding]:
    """The setup check's findings (setup_check.py). Raises InvenTreeError when the server cannot be read."""

    async def read() -> list[Finding]:
        async with InvenTreeReader(settings.inventree, settings.secrets.inventree_token) as reader:
            return await inventree_findings(reader, settings.vehicles, writes=settings.writes.enabled)

    return [
        *asyncio.run(read()),
        *check_posters(posters_seen(settings.storage.database), settings.vehicles),
    ]


async def inventree_findings(reader: InvenTreeReader, vehicles: Mapping[str, str], *, writes: bool) -> list[Finding]:
    """Version, roles, and `[vehicles]` paths. A catalog that cannot be read is a finding, so the roles still print."""
    version = check_version(await reader.server_info())
    roles = await reader.user_roles()
    found = [
        Finding(Level.OK if version.writes_allowed else Level.WARN, f"inventree: {version.message}"),
        *check_roles(roles, writes=writes),
    ]
    if lacks_catalog_view(roles):
        return [*found, Finding(Level.WARN, "vehicles: not checked until part and stock_location have view")]
    try:
        catalog = await load_catalog(reader)
    except InvenTreeError as exc:
        return [*found, Finding(Level.ERROR, f"vehicles: not checked, the catalog could not be read: {exc}")]
    return [*found, *check_vehicles(catalog, vehicles)]


def llm_provider(settings: Settings) -> OpenAICompatible | None:
    """The provider from the `[llm]` section, or None without one."""
    if settings.llm is None:
        return None
    return OpenAICompatible(
        settings.llm.base_url,
        settings.llm.model,
        settings.secrets.llm_api_key,
        timeout_seconds=settings.llm.timeout_seconds,
    )


def slack_tls() -> ssl.SSLContext:
    """The system's certificate authorities plus certifi's, for the Web API and Socket Mode alike.

    Python from python.org on macOS knows no authority until its "Install Certificates" step is run, and
    every Slack call then fails with CERTIFICATE_VERIFY_FAILED. Verification itself stays as strict as the
    default. SSL_CERT_FILE and SSL_CERT_DIR still add authorities, for a TLS-inspecting proxy.
    """
    context = ssl.create_default_context()
    context.load_verify_locations(certifi.where())
    return context


def serve(app: Any, settings: Settings) -> Connection:
    """The connection for the configured transport."""
    if settings.slack.transport == "http":
        signing_secret = settings.secrets.slack_signing_secret
        if signing_secret is None:  # the settings refuse this already
            raise ValueError("HTTP mode needs SLACK_SIGNING_SECRET")
        return HttpServer(app, settings.slack.http, signing_secret.get_secret_value())
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app_token = settings.secrets.slack_app_token
    if app_token is None:  # the settings refuse this already
        raise ValueError("Socket Mode needs SLACK_APP_TOKEN")
    return SocketModeHandler(app, app_token.get_secret_value())


def run(
    settings: Settings,
    stop: threading.Event,
    *,
    slack_client: WebClient | None = None,
    catalog: Callable[[], Catalog] | None = None,
    stock: StockReader | None = None,
    check: Callable[[Settings], VersionCheck] = check_inventree,
    connect: Callable[[Any, Settings], Connection] = serve,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    steps: Callable[..., tuple[Stepper, Undoer]] = inventree_steps,
    approver: Callable[..., Approver] = policy_approver,
    clicker: Callable[..., ClickApprover] = click_approver,
) -> int:
    """Serve until `stop` is set. Returns the process exit code. The worker gets its apply step and undo (`steps`),
    approval by the policy (`approver`) and by an Apply click (`clicker`), only when `writes.enabled` is set and the
    server's version is tested for writes."""
    http = settings.slack.transport == "http"
    signing_secret = settings.secrets.slack_signing_secret
    if (signing_secret if http else settings.secrets.slack_app_token) is None:  # the settings refuse this already
        return 2
    store = Store.open(settings.storage.database)
    owner = new_lock_owner()
    try:
        store.acquire_lock(owner, stale_after=lock_stale_after(settings))
    except InstanceLocked:
        log.error("another instance is running on %s", settings.storage.database)
        store.close()
        return 3
    connection: Connection | None = None
    swept = threading.Event()
    sweeping: threading.Thread | None = None
    try:
        worker = Worker(settings, store, owner, WebClient(), lambda: CATALOG_UNSET, clock, stopping=stop.is_set)
        requeued = store.requeue_unfinished(owner)
        if requeued:
            log.info("picked up %d events left unfinished", requeued)
        # Before this process could send a write: one of its own in flight would become unknown too.
        for key in store.conclude_crashed_writes(owner):
            log.warning(
                "post %s: request r%d/%d was left in flight by a crash: its outcome is unknown",
                key.post.ts,
                key.revision,
                key.sequence,
            )
        log.info(settings.llm_line())
        bot_token = settings.secrets.slack_bot_token.get_secret_value()
        client = slack_client or WebClient(token=bot_token, ssl=slack_tls())
        # No handler that sleeps for Slack's Retry-After inside a call: it could outlast the instance lock. A
        # rate-limited reply fails instead, and the worker tries it again once the time Slack named has passed.
        client.retry_handlers = [
            handler for handler in client.retry_handlers if not isinstance(handler, RateLimitErrorRetryHandler)
        ]
        client.timeout = SLACK_TIMEOUT_SECONDS
        cache = CatalogCache(catalog or inventree_catalog(settings))
        try:
            identity = identify(client, settings.slack)
            worker.beat(now=True)
            if stop.is_set():
                return 0
            version = check(settings)
            log.log(logging.INFO if version.writes_allowed else logging.WARNING, "%s", version.message)
            worker.beat(now=True)
            cache()
            worker.beat(now=True)
            if stop.is_set():
                return 0
            listener = Listener(settings.slack, ThreadStores(settings.storage.database))
            app = build_app(
                settings.slack,
                identity,
                listener,
                bot_token=bot_token,
                signing_secret=signing_secret.get_secret_value() if http and signing_secret is not None else None,
                client=client,
            )
            connection = connect(app, settings)
            if not http:
                log.info("connecting to Slack over Socket Mode")
            connection.connect()
            if settings.slack.catch_up_hours:
                # From here on Slack delivers; what was posted before is read from the history. The time is
                # taken after the connection is made: a message from the moment in between may then come
                # both ways, and is placed once.
                connected = Decimal(str(clock().timestamp()))
                history = slack_client or history_client(bot_token)
                sweeping = threading.Thread(
                    target=catch_up, args=(settings, history, clock, connected, swept), name="catch-up", daemon=True
                )
                sweeping.start()
            else:
                log.info("catch-up is off (slack.catch_up_hours = 0): posts made during downtime are not read")
        except (IdentityError, InvenTreeError, ServeError) as error:
            log.error("cannot start: %s", error)
            return 2
        except SlackApiError as error:
            # Only Slack's error code: the whole response is not needed, and it is not ours to log.
            log.error("cannot start: Slack answered %s", error.response.get("error", "an error"))
            return 2
        except OSError as error:
            log.error("cannot start: %s reaching Slack", type(error).__name__)
            return 2
        worker.slack, worker.catalog = DeadlinePoster(client), with_aliases(cache, store)
        worker.stock = stock or inventree_stock(settings)
        if settings.writes.enabled and version.writes_allowed:
            beat = functools.partial(worker.beat, now=True)
            worker.apply, worker.undo = steps(settings, cache, store, owner, beat=beat)
            worker.approve = approver(settings, cache, store, owner, beat=beat, check=check)
            worker.approve_click = clicker(settings, cache, store, owner, beat=beat, check=check)
            auto = ", ".join(settings.policy.auto_apply) or "none"
            log.warning(
                "stock writes are ON: movements the policy approves (%s) are written, read back and can be undone",
                auto,
            )
        else:
            why = "writes.enabled = false" if not settings.writes.enabled else "this InvenTree is not tested for writes"
            log.info("stock writes are off (%s): only dry runs are posted", why)
            if left := store.requests_to_work():
                # Read-only steps too: they wait for writes to be on again, and their posts with them.
                log.warning("%d requests from a run with writes on wait for writes to be on again", len(left))
        provider = llm_provider(settings)
        alive: Callable[[], bool] = getattr(connection, "alive", lambda: True)
        if provider is None or settings.llm is None:
            return _loop(worker, stop, alive)
        try:
            worker.llm = LLMExtractor(WithinDeadline(provider, settings.llm.timeout_seconds))
            return _loop(worker, stop, alive)
        finally:
            provider.close()
    except LockLost:
        log.error("another instance took over the database lock; stopping")
        return 1
    finally:
        swept.set()
        if sweeping is not None:
            sweeping.join(SWEEP_JOIN_SECONDS)  # a call that hangs is left to end with the process
        try:
            if connection is not None:
                connection.close()
        finally:
            try:
                store.release_lock(owner)
            finally:
                store.close()


def history_client(bot_token: str) -> WebClient:
    """A client of its own for catch-up. It keeps slack_sdk's handler that waits out a rate limit: on the
    catch-up thread a wait holds back nothing, and the instance lock is not its concern."""
    client = WebClient(token=bot_token, ssl=slack_tls(), timeout=SLACK_TIMEOUT_SECONDS)
    client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=2))
    return client


def catch_up(
    settings: Settings, client: HistoryClient, clock: Callable[[], datetime], first: Decimal, stop: threading.Event
) -> None:
    """Sweep the channel history until `stop` is set: first up to `first` (when the connection to Slack was
    made), then every SWEEP_SECONDS. Runs on its own thread, with its own store connection."""
    store: Store | None = None
    latest: Decimal | None = first
    try:
        while not stop.is_set():
            try:
                store = store or Store.open(settings.storage.database)
                CatchUp(settings.slack, store, client, clock, stop.is_set).sweep(latest)
                latest = None  # the first sweep is tried until it has run: it alone reaches up to `first`
            except Exception as error:
                # The class only: a message could quote a post.
                log.warning("catch-up failed (%s); trying again at the next sweep", type(error).__name__)
            stop.wait(SWEEP_SECONDS)
    finally:
        if store is not None:
            store.close()


def lock_stale_after(settings: Settings) -> timedelta:
    """Longer than the worker can block between two beats: a reply (POST_DEADLINE_SECONDS) or a note to a
    clicker (NOTE_DEADLINE_SECONDS) with the beat before it (worker.HEARTBEAT_SECONDS), a catalog read,
    which is cut off CATALOG_MARGIN_SECONDS before this, or a call to the LLM provider, which is cut off
    LLM_MARGIN_SECONDS before this."""
    llm = 0 if settings.llm is None else settings.llm.timeout_seconds + LLM_MARGIN_SECONDS
    return max(LOCK_STALE_AFTER, timedelta(seconds=max(4 * settings.inventree.timeout_seconds + 60, llm)))


def _loop(worker: Worker, stop: threading.Event, alive: Callable[[], bool] = lambda: True) -> int:
    """Exit code 1 when the connection to Slack stops serving by itself (HTTP): Slack's deliveries then fail
    while the process looks healthy, and a supervisor should start it again."""
    errors = 0
    while not stop.is_set():
        if not alive():
            log.error("the connection to Slack has stopped serving; stopping")
            return 1
        wait = POLL_SECONDS
        try:
            worker.beat()
            worker.step()
            errors = 0
        except LockLost:
            raise
        except Exception as error:
            # An outage (InvenTree, Slack, the disk): wait and try again. The class only: a message could
            # quote a post.
            errors += 1
            wait = min(2.0 ** min(errors, 10), MAX_ERROR_BACKOFF_SECONDS)  # a long outage must not overflow
            log.warning("worker step failed (%s); retrying in %.0f s", type(error).__name__, wait)
        stop.wait(wait)
    return 0
