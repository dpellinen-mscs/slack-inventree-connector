"""`stockthread run`: serve Slack, and run the worker until stopped (design section 4).

Startup order: open the store and take the instance lock; put back events an earlier instance left
unfinished; check the bot token (`auth.test`) and InvenTree (version and a first catalog read); then connect
to Slack and run the worker loop in this thread. The listener stores and acknowledges on Bolt's threads;
the worker processes from the queue here. Socket Mode only for now: HTTP serving needs a production WSGI
server and comes in a later release.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from stockthread.catalog import load_catalog
from stockthread.config import Settings
from stockthread.core.matching import Catalog
from stockthread.inventree import InvenTreeError, InvenTreeReader, check_version
from stockthread.slack.listener import IdentityError, Listener, ThreadStores, build_app, identify
from stockthread.store import InstanceLocked, LockLost, Store, new_lock_owner
from stockthread.worker import Worker

log = logging.getLogger(__name__)

POLL_SECONDS = 1.0
LOCK_STALE_AFTER = timedelta(seconds=60)
"""Another instance may take the lock over once the heartbeat is this old."""
CATALOG_REFRESH_SECONDS = 5 * 60
CATALOG_RETRY_SECONDS = 30
CATALOG_MAX_AGE_SECONDS = 60 * 60
"""A refresh that fails keeps the last snapshot for dry runs, up to this age; after that, posts wait."""
MAX_ERROR_BACKOFF_SECONDS = 60.0
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


def inventree_catalog(settings: Settings) -> Callable[[], Catalog]:
    async def read() -> Catalog:
        async with InvenTreeReader(settings.inventree, settings.secrets.inventree_token) as reader:
            return await load_catalog(reader)

    return lambda: asyncio.run(read())


def check_inventree(settings: Settings) -> str:
    """The version line to log. Raises InvenTreeError when the server cannot be read."""

    async def read() -> str:
        async with InvenTreeReader(settings.inventree, settings.secrets.inventree_token) as reader:
            return check_version(await reader.server_info()).message

    return asyncio.run(read())


def socket_mode(app: Any, app_token: str) -> Connection:
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    return SocketModeHandler(app, app_token)


def run(
    settings: Settings,
    stop: threading.Event,
    *,
    slack_client: WebClient | None = None,
    catalog: Callable[[], Catalog] | None = None,
    check: Callable[[Settings], str] = check_inventree,
    connect: Callable[[Any, str], Connection] = socket_mode,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Serve until `stop` is set. Returns the process exit code."""
    if settings.slack.transport != "socket":
        log.error('HTTP serving is not built yet: set slack.transport = "socket"')
        return 2
    app_token = settings.secrets.slack_app_token
    if app_token is None:  # the settings refuse this already
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
    try:
        worker = Worker(settings, store, owner, WebClient(), lambda: CATALOG_UNSET, clock, stopping=stop.is_set)
        requeued = store.requeue_unfinished(owner)
        if requeued:
            log.info("picked up %d events left unfinished", requeued)
        if settings.writes.enabled:
            log.warning("writes.enabled has no effect in this version: it has no apply step yet")
        log.info("This version only posts dry runs: no stock is ever written.")
        bot_token = settings.secrets.slack_bot_token.get_secret_value()
        client = slack_client or WebClient(token=bot_token)
        if not any(isinstance(handler, RateLimitErrorRetryHandler) for handler in client.retry_handlers):
            client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=2))
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
            app = build_app(settings.slack, identity, listener, bot_token=bot_token, signing_secret=None, client=client)
            connection = connect(app, app_token.get_secret_value())
            log.info("connecting to Slack over Socket Mode")
            connection.connect()
        except (IdentityError, InvenTreeError) as error:
            log.error("cannot start: %s", error)
            return 2
        except SlackApiError as error:
            # Only Slack's error code: the whole response is not needed, and it is not ours to log.
            log.error("cannot start: Slack answered %s", error.response.get("error", "an error"))
            return 2
        except OSError as error:
            log.error("cannot start: %s reaching Slack", type(error).__name__)
            return 2
        worker.slack, worker.catalog = client, cache
        return _loop(worker, stop)
    except LockLost:
        log.error("another instance took over the database lock; stopping")
        return 1
    finally:
        try:
            if connection is not None:
                connection.close()
        finally:
            try:
                store.release_lock(owner)
            finally:
                store.close()


def lock_stale_after(settings: Settings) -> timedelta:
    """Longer than a worker step can block between two beats: a Slack call with its retries, or a few slow
    InvenTree pages."""
    return max(LOCK_STALE_AFTER, timedelta(seconds=4 * settings.inventree.timeout_seconds + 60))


def _loop(worker: Worker, stop: threading.Event) -> int:
    errors = 0
    while not stop.is_set():
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
            wait = min(2.0**errors, MAX_ERROR_BACKOFF_SECONDS)
            log.warning("worker step failed (%s); retrying in %.0f s", type(error).__name__, wait)
        stop.wait(wait)
    return 0
