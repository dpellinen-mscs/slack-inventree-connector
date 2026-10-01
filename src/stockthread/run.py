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
import logging
import ssl
import threading
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import certifi
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from stockthread.catalog import assign_vehicles, load_catalog
from stockthread.config import Settings
from stockthread.core.matching import Catalog
from stockthread.inventree import InvenTreeError, InvenTreeReader, check_version
from stockthread.llm.extract import LLMExtractor, WithinDeadline
from stockthread.llm.openai_compatible import OpenAICompatible
from stockthread.slack.catchup import SWEEP_SECONDS, CatchUp, HistoryClient
from stockthread.slack.http_server import HttpServer, ServeError
from stockthread.slack.listener import IdentityError, Listener, ThreadStores, build_app, identify
from stockthread.store import InstanceLocked, LockLost, Store, new_lock_owner
from stockthread.worker import PostStillRunning, Worker

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


class PosterBusy(Exception):
    """Another thread's call is still running, and this one was not sent. An ordinary failure: try again."""


class _Call:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Any = None
        self.error: Exception | None = None


class DeadlinePoster:
    """Posts on a helper thread, and gives the worker control back after `deadline` seconds.

    The worker then keeps the instance lock's heartbeat going, so no other instance takes over while a call
    hangs, and no reply is posted twice by two instances. A call that ran out of time is left running: the
    next try for the same thread takes its result (or its error) and does not post again, and until it has
    ended nothing else is posted.
    """

    def __init__(
        self, client: Any, deadline: float = POST_DEADLINE_SECONDS, note_deadline: float = NOTE_DEADLINE_SECONDS
    ) -> None:
        self._client = client
        self._deadline = deadline
        self._note_deadline = note_deadline
        self._note: _Call | None = None
        self._calls: dict[tuple[Any, Any], _Call] = {}

    def chat_postMessage(self, **kwargs: Any) -> Any:  # slack_sdk's name
        key = (kwargs.get("channel"), kwargs.get("thread_ts"))
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
        """A note to one user, on its own helper thread, apart from the replies. A note that runs out of time is
        left running and not tried again; until it has ended no other note is sent, so stuck calls never pile
        up."""
        if self._note is not None and not self._note.done.is_set():
            raise PosterBusy
        call = self._note = _Call()
        threading.Thread(
            target=self._run, args=(call, self._client.chat_postEphemeral, kwargs), name="slack-note", daemon=True
        ).start()
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


def read_within(read: Callable[[], Coroutine[Any, Any, Catalog]], limit: float) -> Catalog:
    async def limited() -> Catalog:
        try:
            return await asyncio.wait_for(read(), limit)
        except TimeoutError:
            raise InvenTreeError(
                f"the catalog read took longer than {limit:.0f} s; raise inventree.timeout_seconds"
            ) from None

    return asyncio.run(limited())


def check_inventree(settings: Settings) -> str:
    """The version line to log. Raises InvenTreeError when the server cannot be read."""

    async def read() -> str:
        async with InvenTreeReader(settings.inventree, settings.secrets.inventree_token) as reader:
            return check_version(await reader.server_info()).message

    return asyncio.run(read())


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
    check: Callable[[Settings], str] = check_inventree,
    connect: Callable[[Any, Settings], Connection] = serve,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Serve until `stop` is set. Returns the process exit code."""
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
        if settings.writes.enabled:
            log.warning("writes.enabled has no effect in this version: it has no apply step yet")
        log.info("This version only posts dry runs: no stock is ever written.")
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
            log.info(check(settings))
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
        worker.slack, worker.catalog = DeadlinePoster(client), cache
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
