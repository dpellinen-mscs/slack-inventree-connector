"""`stockthread run`: startup checks, the lock, the catalog cache, and one post from the queue to its reply."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.client
import json
import logging
import signal
import socket
import sqlite3
import ssl
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.error import URLError

import httpx
import pytest
from pydantic import SecretStr
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from stockthread import run as run_module
from stockthread.__main__ import install_stop_handlers
from stockthread.catalog import assign_vehicles, load_catalog
from stockthread.config import InvenTreeSettings, Settings, parse_settings
from stockthread.core.matching import Catalog, LocationRecord, PartRecord
from stockthread.core.model import PostKey
from stockthread.inventree import InvenTreeError, InvenTreeReader
from stockthread.llm.extract import WithinDeadline
from stockthread.llm.openai_compatible import OpenAICompatible
from stockthread.run import CatalogCache, run, with_vehicles
from stockthread.store import DryRunOutcome, Store, new_lock_owner
from stockthread.worker import RETRY_FOR_SECONDS, PosterBusy, PostStillRunning, Worker

WORKSPACE, APP, CHANNEL = "T0ACME001", "A0ACME001", "C0ACME001"
CATALOG = Catalog(
    parts=(PartRecord(1, "DISC-40", "Disc 40"),),
    locations=(LocationRecord(10, "Warehouse/Main Stock"), LocationRecord(11, "Vehicles/Truck 1")),
)
ENV = {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic", "INVENTREE_TOKEN": "inv-synthetic"}


def settings(
    database: Path,
    transport: str = "socket",
    llm: dict[str, Any] | None = None,
    llm_key: str = "",
    vehicles: dict[str, str] | None = None,
    catch_up_hours: int = 72,
    http_port: int = 3000,
) -> Settings:
    data: dict[str, Any] = {
        "slack": {
            "workspace_id": WORKSPACE,
            "app_id": APP,
            "channels": [CHANNEL],
            "transport": transport,
            "catch_up_hours": catch_up_hours,
            "http": {"port": http_port},
        },
        "inventree": {"url": "http://inventree.local:8000"},
        "storage": {"database": str(database)},
    }
    if llm:
        data["llm"] = llm
    if vehicles:
        data["vehicles"] = vehicles
    return parse_settings(data, {**ENV, "SLACK_SIGNING_SECRET": "synthetic-secret", "LLM_API_KEY": llm_key})


class FakeClient(WebClient):
    """Bolt takes only a real WebClient: the calls the connector makes are replaced."""

    def __init__(self, stop: threading.Event, team: str = WORKSPACE) -> None:
        super().__init__()
        self.stop = stop
        self.team = team
        self.posted: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.listed: list[dict[str, Any]] = []

    def conversations_history(self, **kwargs: Any) -> Any:
        self.listed.append(kwargs)
        return {"ok": True, "messages": self.history, "has_more": False}

    def conversations_replies(self, **kwargs: Any) -> Any:
        raise AssertionError("no thread in these tests")

    def auth_test(self, **kwargs: Any) -> Any:
        return {"team_id": self.team, "bot_id": "B0STOCKBOT", "user_id": "U0STOCKBOT"}

    def chat_postMessage(self, **kwargs: Any) -> Any:
        self.posted.append(kwargs)
        self.stop.set()
        return {"ok": True, "ts": "1790000099.000100"}


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def connect(self) -> None:
        self.calls.append("connect")

    def close(self) -> None:
        self.calls.append("close")


def queued_message(database: Path, ts: str, text: str) -> None:
    store = Store.open(database)
    body = {
        "type": "event_callback",
        "team_id": WORKSPACE,
        "api_app_id": APP,
        "event_id": f"Ev{ts}",
        "event": {"type": "message", "channel": CHANNEL, "user": "U0SAM0001", "text": text, "ts": ts},
    }
    store.enqueue_event(f"Ev{ts}", "event", json.dumps(body))
    store.close()


def lock_is_free(database: Path) -> bool:
    store = Store.open(database)
    try:
        store.acquire_lock(new_lock_owner(), stale_after=timedelta(hours=1))
        return True
    except Exception:
        return False
    finally:
        store.close()


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_module, "POLL_SECONDS", 0.01)


@pytest.fixture
def stop() -> Iterator[threading.Event]:
    event = threading.Event()
    timer = threading.Timer(10, event.set)  # a failing test ends instead of hanging
    timer.start()
    yield event
    timer.cancel()


def test_a_queued_post_gets_its_dry_run_then_the_lock_is_released(tmp_path: Path, stop: threading.Event) -> None:
    database = tmp_path / "stockthread.db"
    queued_message(database, "1790000000.000100", "took 2 Disc 40 from main stock to truck 1")
    client = FakeClient(stop)
    connection = FakeConnection()
    code = run(
        settings(database),
        stop,
        slack_client=client,
        catalog=lambda: CATALOG,
        check=lambda _: "InvenTree 1.5.6 (API 530) is tested.",
        connect=lambda app, token: connection,
        clock=lambda: datetime.fromtimestamp(1790000030, UTC),
    )
    assert code == 0
    assert [reply["thread_ts"] for reply in client.posted] == ["1790000000.000100"]
    assert "*Disc 40*" in json.dumps(client.posted[0]["blocks"])
    assert connection.calls == ["connect", "close"]
    # Nothing may sleep for Slack's Retry-After inside a call, and a call is short: the lock outlives both.
    assert not any(isinstance(handler, RateLimitErrorRetryHandler) for handler in client.retry_handlers)
    assert run_module.LOCK_STALE_AFTER.total_seconds() > run_module.POST_DEADLINE_SECONDS
    assert lock_is_free(database)


def test_a_second_instance_on_the_same_database_refuses_to_start(tmp_path: Path, stop: threading.Event) -> None:
    database = tmp_path / "stockthread.db"
    holder = Store.open(database)
    holder.acquire_lock(new_lock_owner(), stale_after=timedelta(minutes=1))
    connection = FakeConnection()
    code = run(
        settings(database),
        stop,
        slack_client=FakeClient(stop),
        catalog=lambda: CATALOG,
        check=lambda _: "",
        connect=lambda app, token: connection,
    )
    holder.close()
    assert code == 3
    assert connection.calls == []


@pytest.mark.parametrize("failure", ["token", "inventree", "catalog", "revoked", "offline", "app_token"])
def test_startup_checks_stop_before_connecting_and_free_the_lock(
    tmp_path: Path, stop: threading.Event, failure: str
) -> None:
    database = tmp_path / "stockthread.db"
    connection = FakeConnection()

    def check(_: Settings) -> str:
        if failure == "inventree":
            raise InvenTreeError("GET /api/ failed: ConnectError")
        return ""

    def catalog() -> Catalog:
        if failure == "catalog":
            raise InvenTreeError("a part has no valid name")
        return CATALOG

    client = FakeClient(stop, team="T0OTHER01" if failure == "token" else WORKSPACE)
    if failure == "revoked":
        client.auth_test = lambda **_: _raise(revoked())  # type: ignore[method-assign]
    if failure == "offline":
        client.auth_test = lambda **_: _raise(URLError("synthetic"))  # type: ignore[method-assign]
    if failure == "app_token":
        connection.connect = lambda: _raise(revoked())  # type: ignore[method-assign]
    code = run(
        settings(database),
        stop,
        slack_client=client,
        catalog=catalog,
        check=check,
        connect=lambda app, token: connection,
    )
    assert code == 2
    assert connection.calls == (["close"] if failure == "app_token" else [])
    assert lock_is_free(database)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def post_signed(port: int, body: dict[str, Any], secret: str = "synthetic-secret") -> int:
    """POST an event as Slack would, once the server listens. Returns the status."""
    raw, timestamp = json.dumps(body), int(time.time())
    digest = hmac.new(secret.encode(), f"v0:{timestamp}:{raw}".encode(), hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Slack-Request-Timestamp": str(timestamp),
        "X-Slack-Signature": f"v0={digest}",
    }
    deadline = time.monotonic() + 5
    while True:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("POST", "/slack/events", body=raw.encode(), headers=headers)
            return connection.getresponse().status
        except ConnectionRefusedError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)  # startup is still checking InvenTree
        finally:
            connection.close()


def test_a_post_delivered_over_http_gets_its_dry_run(tmp_path: Path, stop: threading.Event) -> None:
    database, port = tmp_path / "stockthread.db", free_port()
    client = FakeClient(stop)
    codes: list[int] = []
    running = threading.Thread(
        target=lambda: codes.append(
            run(
                settings(database, transport="http", http_port=port),
                stop,
                slack_client=client,
                catalog=lambda: CATALOG,
                check=lambda _: "",
                clock=lambda: datetime.fromtimestamp(1790000030, UTC),
            )
        )
    )
    running.start()
    event = {"type": "message", "channel": CHANNEL, "user": "U0SAM0001", "ts": "1790000000.000100"}
    event["text"] = "took 2 Disc 40 from main stock to truck 1"
    body = {
        "type": "event_callback",
        "team_id": WORKSPACE,
        "api_app_id": APP,
        "event_id": "Ev0HTTP0001",
        "event": event,
    }
    try:
        assert post_signed(port, body, secret="another-synthetic-secret") == 401
        assert post_signed(port, body) == 200
    finally:
        running.join(10)
    assert codes == [0]
    assert [reply["thread_ts"] for reply in client.posted] == ["1790000000.000100"]
    with socket.socket() as again:
        again.bind(("127.0.0.1", port))  # the server is closed: the port is free
    assert lock_is_free(database)


def test_http_mode_stops_with_exit_2_when_the_port_is_taken(tmp_path: Path, stop: threading.Event) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        code = run(
            settings(tmp_path / "stockthread.db", transport="http", http_port=port),
            stop,
            slack_client=FakeClient(stop),
            catalog=lambda: CATALOG,
            check=lambda _: "",
        )
    assert code == 2
    assert lock_is_free(tmp_path / "stockthread.db")


def test_the_catalog_is_refreshed_and_a_failed_refresh_keeps_the_last_snapshot_for_a_while() -> None:
    now = [0.0]
    loads: list[int] = []
    failing = [False]

    def load() -> Catalog:
        loads.append(1)
        if failing[0]:
            raise InvenTreeError("GET /api/part/ failed: ConnectError")
        return CATALOG

    cache = CatalogCache(load, clock=lambda: now[0])
    assert cache() is CATALOG
    now[0] = run_module.CATALOG_REFRESH_SECONDS - 1
    cache()
    assert len(loads) == 1
    now[0] = run_module.CATALOG_REFRESH_SECONDS
    failing[0] = True
    assert cache() is CATALOG  # kept
    now[0] = run_module.CATALOG_MAX_AGE_SECONDS
    with pytest.raises(InvenTreeError):
        cache()  # too old: posts wait rather than match against an hour-old catalog


def reader(rows: dict[str, list[dict[str, Any]]]) -> InvenTreeReader:
    def handler(request: httpx.Request) -> httpx.Response:
        results = rows[request.url.path]
        return httpx.Response(200, json={"count": len(results), "results": results})

    return InvenTreeReader(
        InvenTreeSettings(url="http://inventree.local:8000"),
        SecretStr("inv-synthetic"),
        transport=httpx.MockTransport(handler),
    )


PART = {
    "pk": 1,
    "IPN": None,
    "name": "Disc 40",
    "keywords": None,
    "active": True,
    "is_template": False,
    "virtual": False,
}
LOCATION: dict[str, Any] = {"pk": 10, "pathstring": "Warehouse/Main Stock", "description": "", "structural": False}


@pytest.mark.anyio
async def test_the_catalog_is_read_from_inventree() -> None:
    async with reader({"/api/part/": [PART], "/api/stock/location/": [LOCATION]}) as client:
        catalog = await load_catalog(client)
    assert catalog.parts == (PartRecord(1, "", "Disc 40"),)
    assert catalog.locations == (LocationRecord(10, "Warehouse/Main Stock"),)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "row"),
    [
        ("/api/part/", {**PART, "name": ""}),
        ("/api/part/", {**PART, "pk": "1"}),
        ("/api/part/", {key: value for key, value in PART.items() if key != "virtual"}),
        ("/api/stock/location/", {**LOCATION, "pathstring": None}),
        ("/api/stock/location/", {**LOCATION, "structural": 0}),
    ],
)
async def test_a_malformed_record_fails_the_whole_catalog(path: str, row: dict[str, Any]) -> None:
    rows: dict[str, list[dict[str, Any]]] = {"/api/part/": [PART], "/api/stock/location/": [LOCATION]}
    rows[path] = [row]
    async with reader(rows) as client:
        with pytest.raises(InvenTreeError, match="has no valid"):
            await load_catalog(client)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def revoked() -> SlackApiError:
    return SlackApiError("synthetic", {"ok": False, "error": "invalid_auth"})  # type: ignore[no-untyped-call]


def _raise(error: Exception) -> Any:
    raise error


def run_with(database: Path, stop: threading.Event, connection: FakeConnection, **overrides: Any) -> int:
    arguments: dict[str, Any] = {
        "slack_client": FakeClient(stop),
        "catalog": lambda: CATALOG,
        "check": lambda _: "",
        "connect": lambda app, token: connection,
        "clock": lambda: datetime.fromtimestamp(1790000030, UTC),
    }
    return run(settings(database), stop, **{**arguments, **overrides})


def test_a_stop_during_startup_ends_before_connecting(tmp_path: Path, stop: threading.Event) -> None:
    connection = FakeConnection()

    def check(_: Settings) -> str:
        stop.set()  # Ctrl-C while InvenTree is being checked
        return ""

    assert run_with(tmp_path / "stockthread.db", stop, connection, check=check) == 0
    assert connection.calls == []
    assert lock_is_free(tmp_path / "stockthread.db")


def test_a_lost_lock_stops_with_exit_1_and_leaves_the_new_owner_alone(tmp_path: Path, stop: threading.Event) -> None:
    database = tmp_path / "stockthread.db"
    connection = FakeConnection()

    def connect(app: Any, _: Settings) -> FakeConnection:
        with sqlite3.connect(database) as raw:  # another instance takes the lock over
            raw.execute("UPDATE instance_lock SET owner = 'instance-b'")
        return connection

    assert run_with(database, stop, connection, connect=connect) == 1
    assert connection.calls == ["connect", "close"]
    with sqlite3.connect(database) as raw:
        assert raw.execute("SELECT owner FROM instance_lock").fetchall() == [("instance-b",)]


def test_a_connection_that_stops_serving_stops_the_run_with_exit_1(tmp_path: Path, stop: threading.Event) -> None:
    connection = FakeConnection()
    connection.alive = lambda: False  # type: ignore[attr-defined]
    assert run_with(tmp_path / "stockthread.db", stop, connection) == 1
    assert connection.calls == ["connect", "close"]
    assert lock_is_free(tmp_path / "stockthread.db")


def test_a_failing_close_still_frees_the_lock(tmp_path: Path, stop: threading.Event) -> None:
    database = tmp_path / "stockthread.db"
    connection = FakeConnection()
    connection.close = lambda: _raise(OSError("synthetic"))  # type: ignore[method-assign]

    def connect(app: Any, _: Settings) -> FakeConnection:
        stop.set()
        return connection

    with pytest.raises(OSError, match="synthetic"):
        run_with(database, stop, connection, connect=connect)
    assert lock_is_free(database)


def test_a_catalog_outage_costs_one_read_per_retry_interval() -> None:
    now = [0.0]
    loads: list[int] = []

    def load() -> Catalog:
        loads.append(1)
        if len(loads) > 1:
            raise InvenTreeError("GET /api/part/ failed: ConnectError")
        return CATALOG

    cache = CatalogCache(load, clock=lambda: now[0])
    cache()
    now[0] = run_module.CATALOG_REFRESH_SECONDS
    for _ in range(10):
        assert cache() is CATALOG
    assert len(loads) == 2
    now[0] += run_module.CATALOG_RETRY_SECONDS
    cache()
    assert len(loads) == 3


def test_a_second_signal_stops_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: dict[int, Any] = {}
    monkeypatch.setattr(signal, "signal", lambda signum, handler: installed.__setitem__(signum, handler))
    stop = threading.Event()
    install_stop_handlers(stop)
    assert set(installed) == {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert stop.wait(1)
    with pytest.raises(KeyboardInterrupt):
        installed[signal.SIGTERM](signal.SIGTERM, None)
    assert installed[signal.SIGTERM] is signal.SIG_DFL


@pytest.mark.anyio
async def test_empty_optional_fields_and_unusable_parts_are_read_as_they_are() -> None:
    template = {**PART, "pk": 2, "keywords": None, "is_template": True, "virtual": True}
    location = {**LOCATION, "description": None, "structural": True}
    async with reader({"/api/part/": [template], "/api/stock/location/": [location]}) as client:
        catalog = await load_catalog(client)
    assert catalog.parts == (PartRecord(2, "", "Disc 40", "", True, True, True),)
    assert catalog.locations == (LocationRecord(10, "Warehouse/Main Stock", "", True),)


def test_a_long_outage_keeps_backing_off_without_overflowing(monkeypatch: pytest.MonkeyPatch) -> None:
    stop = threading.Event()
    waits: list[float] = []

    class Broken:
        steps = 0

        def beat(self) -> None:
            pass

        def step(self) -> int:
            self.steps += 1
            if self.steps > 1100:  # 2.0 ** 1024 overflows a float
                stop.set()
            raise ConnectionError("synthetic outage")

    monkeypatch.setattr(stop, "wait", lambda seconds: waits.append(seconds))
    assert run_module._loop(Broken(), stop) == 0  # type: ignore[arg-type]
    assert len(waits) == 1101
    assert waits[:7] == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
    assert set(waits[6:]) == {60.0}


def test_a_catalog_read_is_cut_off_before_the_lock_could_go_stale() -> None:
    async def slow() -> Catalog:
        await asyncio.sleep(30)
        return CATALOG

    async def quick() -> Catalog:
        return CATALOG

    with pytest.raises(InvenTreeError, match="longer than 0 s"):
        run_module.read_within(slow, 0.05)
    assert run_module.read_within(quick, 5) == CATALOG


class SlowClient:
    """A Slack client whose call hangs until it is released, like a connection to a black-holed address."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if not self.release.wait(10):
            raise AssertionError("never released")
        if self.error is not None:
            raise self.error
        return {"ok": True, "ts": "1790000099.000100"}

    def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        return self.chat_postEphemeral(**kwargs)

    def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if not self.release.wait(10):
            raise AssertionError("never released")
        if self.error is not None:
            raise self.error
        return {"ok": True, "message_ts": "1790000099.000200"}


def wait_until_idle() -> None:
    for thread in threading.enumerate():
        if thread.name in ("slack-post", "slack-side"):
            thread.join(5)


def test_a_hung_post_gives_control_back_and_is_never_sent_twice() -> None:
    client = SlowClient()
    poster = run_module.DeadlinePoster(client, deadline=0.05)
    first = {"channel": CHANNEL, "thread_ts": "1790000000.000100", "text": "one"}
    other = {"channel": CHANNEL, "thread_ts": "1790000000.000200", "text": "two"}
    with pytest.raises(PostStillRunning):
        poster.chat_postMessage(**first)  # back after the deadline: the worker keeps its heartbeat going
    with pytest.raises(PostStillRunning):
        poster.chat_postMessage(**first)  # still running: not sent again
    with pytest.raises(PosterBusy):
        poster.chat_postMessage(**other)  # and nothing else is sent meanwhile: a failure, not a call on its way
    assert len(client.calls) == 1

    client.release.set()  # the network is back, and the first call goes through
    wait_until_idle()
    assert poster.chat_postMessage(**first)["ts"] == "1790000099.000100"  # its result, not a second post
    assert len(client.calls) == 1
    assert poster.chat_postMessage(**other)["ok"]
    assert [call["text"] for call in client.calls] == ["one", "two"]


def test_a_hung_post_that_ends_in_an_error_reports_it_once_and_is_then_tried_again() -> None:
    client = SlowClient()
    client.error = ConnectionError("synthetic")
    poster = run_module.DeadlinePoster(client, deadline=0.05)
    reply = {"channel": CHANNEL, "thread_ts": "1790000000.000100", "text": "one"}
    with pytest.raises(PostStillRunning):
        poster.chat_postMessage(**reply)
    client.release.set()
    wait_until_idle()
    with pytest.raises(ConnectionError):
        poster.chat_postMessage(**reply)
    client.error = None
    assert poster.chat_postMessage(**reply)["ok"]
    assert len(client.calls) == 2


def test_a_quick_post_passes_through_with_its_error() -> None:
    client = SlowClient()
    client.release.set()
    poster = run_module.DeadlinePoster(client, deadline=5)
    assert poster.chat_postMessage(channel=CHANNEL, thread_ts="1790000000.000100", text="one")["ok"]
    client.error = revoked()
    with pytest.raises(SlackApiError):
        poster.chat_postMessage(channel=CHANNEL, thread_ts="1790000000.000100", text="one")


def test_a_note_has_its_own_deadline_and_never_waits_for_a_reply() -> None:
    client = SlowClient()
    poster = run_module.DeadlinePoster(client, deadline=0.05, note_deadline=0.05)
    reply = {"channel": CHANNEL, "thread_ts": "1790000000.000100", "text": "one"}
    note = {"channel": CHANNEL, "user": "U0SAM0001", "thread_ts": "1790000000.000100", "text": "noted"}
    with pytest.raises(PostStillRunning):
        poster.chat_postMessage(**reply)
    with pytest.raises(PostStillRunning):
        poster.chat_postEphemeral(**note)  # not PosterBusy: a note is not held back by a hung reply
    client.release.set()
    wait_until_idle()
    assert poster.chat_postEphemeral(**note)["ok"]  # a note is not tracked: a new call sends it again
    assert poster.chat_postMessage(**reply)["ts"] == "1790000099.000100"  # the reply's own result
    assert [call["text"] for call in client.calls] == ["one", "noted", "noted"]
    client.error = revoked()
    with pytest.raises(SlackApiError):
        poster.chat_postEphemeral(**note)


def test_only_one_side_call_is_in_flight_so_stuck_calls_never_pile_up() -> None:
    client = SlowClient()
    poster = run_module.DeadlinePoster(client, deadline=0.05, note_deadline=0.05)
    note = {"channel": CHANNEL, "user": "U0SAM0001", "thread_ts": "1790000000.000100", "text": "noted"}
    with pytest.raises(PostStillRunning):
        poster.chat_postEphemeral(**note)
    with pytest.raises(PosterBusy):
        poster.chat_postEphemeral(**note)  # no second thread while the first is stuck
    with pytest.raises(PosterBusy):
        poster.chat_update(channel=CHANNEL, ts="1790000099.000100", text="edit", blocks=[])  # nor another side call
    assert len(client.calls) == 1
    client.release.set()
    wait_until_idle()
    assert poster.chat_postEphemeral(**note)["ok"]
    assert len(client.calls) == 2


def test_slack_tls_verifies_against_known_authorities_on_any_python_build() -> None:
    context = run_module.slack_tls()
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.cert_store_stats()["x509_ca"] > 50  # certifi's, even where the system store is empty


def hung_worker(tmp_path: Path, catalog: Any) -> tuple[Worker, Store, SlowClient, Any]:
    """A worker that posts through a DeadlinePoster whose Slack call hangs, with one post due at 1790000030."""
    store = Store.open(tmp_path / "stockthread.db")
    store.acquire_lock("instance-a", stale_after=timedelta(minutes=1))
    client = SlowClient()
    now = [datetime.fromtimestamp(1790000030, UTC)]
    poster = run_module.DeadlinePoster(client, deadline=0.05)
    worker = Worker(settings(tmp_path / "stockthread.db"), store, "instance-a", poster, catalog, lambda: now[0])
    event = {"type": "message", "channel": CHANNEL, "user": "U0SAM0001", "ts": "1790000000.000100"}
    event["text"] = "took 2 Disc 40 from main stock to truck 1"
    body = {"type": "event_callback", "team_id": WORKSPACE, "api_app_id": APP, "event_id": "Ev1", "event": event}
    assert store.enqueue_event("Ev1", "event", json.dumps(body))

    def at(second: int) -> None:
        now[0] = datetime.fromtimestamp(second, UTC)

    return worker, store, client, at


def reply_ts(tmp_path: Path) -> list[str | None]:
    with sqlite3.connect(tmp_path / "stockthread.db") as raw:
        return [row[0] for row in raw.execute("SELECT reply_ts FROM posts ORDER BY id")]


HUNG_POST = PostKey(WORKSPACE, CHANNEL, "1790000000.000100")
DISC = Catalog(parts=(PartRecord(1, "DISC-40", "Disc 40"),), locations=(LocationRecord(10, "Main stock"),))
RENUMBERED = Catalog(parts=(PartRecord(99, "DISC-40", "Disc 40"),), locations=(LocationRecord(10, "Main stock"),))


def test_a_slow_reply_is_recorded_with_the_decision_that_was_sent(tmp_path: Path) -> None:
    catalogs = [DISC]
    worker, store, client, at = hung_worker(tmp_path, lambda: catalogs[0])
    worker.step()  # the call hangs; the worker has control back
    assert store.dry_run(HUNG_POST) is None
    catalogs[0] = RENUMBERED  # the catalog is read again before the next try
    at(1790000040)
    worker.step()
    assert store.dry_run(HUNG_POST) is None  # still on its way: not decided again, not finished
    client.release.set()
    wait_until_idle()
    at(1790000050)
    worker.step()
    record = store.dry_run(HUNG_POST)
    assert record is not None
    assert record.result is not None
    assert record.outcome is DryRunOutcome.REPLIED
    assert reply_ts(tmp_path) == ["1790000099.000100"]
    assert len(client.calls) == 1
    assert record.result["movements"][0]["items"][0]["part"]["record"] == 1  # what the reply in Slack shows
    store.close()


def test_a_reply_still_on_its_way_is_never_given_up_on_and_holds_the_others_back(tmp_path: Path) -> None:
    worker, store, client, at = hung_worker(tmp_path, lambda: DISC)
    worker.step()
    event = {"type": "message", "channel": CHANNEL, "user": "U0SAM0001", "ts": "1790000100.000100"}
    event["text"] = "took 1 Disc 40 from main stock to truck 1"
    body = {"type": "event_callback", "team_id": WORKSPACE, "api_app_id": APP, "event_id": "Ev2", "event": event}
    assert store.enqueue_event("Ev2", "event", json.dumps(body))
    for second in range(1790000040, 1790000040 + 2 * RETRY_FOR_SECONDS, 30):  # twice the time a failure gets
        at(second)
        worker.step()
    assert store.dry_run(HUNG_POST) is None  # the call can still send: "gave up" would deny a reply in Slack
    assert len(client.calls) == 1  # and the second post waits for it
    client.release.set()
    wait_until_idle()
    at(1790000040 + 2 * RETRY_FOR_SECONDS + 60)
    worker.step()
    first, second_post = store.dry_run(HUNG_POST), store.dry_run(PostKey(WORKSPACE, CHANNEL, "1790000100.000100"))
    assert first is not None
    assert first.outcome is DryRunOutcome.REPLIED
    assert reply_ts(tmp_path) == ["1790000099.000100"] * 2
    assert second_post is not None
    assert second_post.outcome is DryRunOutcome.REPLIED
    assert len(client.calls) == 2
    store.close()


def test_an_edit_while_the_reply_is_on_its_way_waits_for_the_call(tmp_path: Path) -> None:
    worker, store, client, at = hung_worker(tmp_path, lambda: DISC)
    worker.step()
    message = {"type": "message", "user": "U0SAM0001", "ts": HUNG_POST.ts}
    event: dict[str, Any] = {"type": "message", "subtype": "message_changed", "channel": CHANNEL}
    event["message"] = {**message, "text": "took 5 Disc 40 from main stock to truck 1"}
    event["previous_message"] = {**message, "text": "took 2 Disc 40 from main stock to truck 1"}
    body = {"type": "event_callback", "team_id": WORKSPACE, "api_app_id": APP, "event_id": "Ev2", "event": event}
    assert store.enqueue_event("Ev2", "event", json.dumps(body))
    at(1790000040)
    worker.step()
    assert store.dry_run(HUNG_POST) is None  # not withheld: the reply may yet appear
    client.error = ConnectionError("synthetic")
    client.release.set()
    wait_until_idle()
    at(1790000050)
    worker.step()  # the call ended in an error: nothing was posted
    at(1790000060)
    worker.step()  # so the next try reads the post again, and finds it changed
    record = store.dry_run(HUNG_POST)
    assert record is not None
    assert record.outcome is DryRunOutcome.WITHHELD
    assert len(client.calls) == 1
    store.close()


def test_a_reply_on_its_way_is_checked_with_backoff_and_not_while_stopping(tmp_path: Path) -> None:
    worker, store, client, at = hung_worker(tmp_path, lambda: DISC)
    assert worker.step() == 2  # the event, and the reply whose call hangs (next check in 2 seconds)
    at(1790000031)
    assert worker.step() == 0  # backing off
    client.release.set()
    wait_until_idle()
    at(1790000033)
    worker.stopping = lambda: True
    assert worker.step() == 0  # a stop request does not wait for it
    assert store.dry_run(HUNG_POST) is None
    worker.stopping = lambda: False
    assert worker.step() == 1
    assert reply_ts(tmp_path) == ["1790000099.000100"]
    store.close()


def test_a_post_the_rules_decline_is_sent_to_the_configured_model(
    tmp_path: Path, stop: threading.Event, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    database = tmp_path / "stockthread.db"
    queued_message(database, "1790000000.000100", "gave truck 1 two Disc 40 from main stock")
    asked: list[dict[str, Any]] = []
    answer = {
        "readable": True,
        "post_flags": [],
        "movements": [
            {
                "movement_type": "take_to_vehicle",
                "line_items": [{"item_text": "Disc 40", "quantity": 2, "unit": None, "pack_size": None, "serials": []}],
                "source_text": "main stock",
                "destination_text": "truck 1",
                "flags": [],
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer key-synthetic"
        choice = {"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}
        return httpx.Response(200, json={"choices": [choice]})

    closed: list[bool] = []

    def provider(chosen: Settings) -> OpenAICompatible:
        assert chosen.llm is not None
        built = OpenAICompatible(
            chosen.llm.base_url, chosen.llm.model, chosen.secrets.llm_api_key, transport=httpx.MockTransport(handler)
        )
        monkeypatch.setattr(built, "close", lambda: closed.append(True))
        return built

    monkeypatch.setattr(run_module, "llm_provider", provider)
    deadlines: list[float] = []

    class Counted(WithinDeadline):
        def __init__(self, inner: Any, deadline: float) -> None:
            deadlines.append(deadline)
            super().__init__(inner, deadline)

    monkeypatch.setattr(run_module, "WithinDeadline", Counted)
    llm = {"base_url": "https://llm.acme-fiber.example/v1", "model": "small-1"}
    chosen = settings(database, llm=llm, llm_key="key-synthetic")
    client = FakeClient(stop)
    caplog.set_level(logging.INFO)
    code = run(
        chosen,
        stop,
        slack_client=client,
        catalog=lambda: CATALOG,
        check=lambda _: "InvenTree 1.5.6 (API 530) is tested.",
        connect=lambda app, token: FakeConnection(),
        clock=lambda: datetime.fromtimestamp(1790000030, UTC),
    )
    assert code == 0
    assert [call["model"] for call in asked] == ["small-1"]
    assert "gave truck 1 two Disc 40" in asked[0]["messages"][1]["content"]
    assert [reply["thread_ts"] for reply in client.posted] == ["1790000000.000100"]
    assert closed == [True]
    assert deadlines == [30]  # the whole call is bounded by llm.timeout_seconds
    assert "sent to the model small-1 at llm.acme-fiber.example" in caplog.text
    assert "key-synthetic" not in caplog.text
    assert "Disc 40" not in caplog.text


def test_without_an_llm_section_no_provider_is_built(tmp_path: Path) -> None:
    assert run_module.llm_provider(settings(tmp_path / "stockthread.db")) is None
    built = run_module.llm_provider(
        settings(tmp_path / "stockthread.db", llm={"base_url": "http://localhost:11434/v1", "model": "small-1"})
    )
    assert built is not None
    assert built.model == "small-1"
    assert built._api_key is None
    built.close()
    hosted = {"base_url": "https://llm.acme-fiber.example/v1", "model": "small-1", "timeout_seconds": 7}
    built = run_module.llm_provider(settings(tmp_path / "stockthread.db", llm=hosted, llm_key="key-synthetic"))
    assert built is not None
    assert built._api_key is not None
    assert built._api_key.get_secret_value() == "key-synthetic"
    assert built._client.timeout == httpx.Timeout(7)
    built.close()


def test_the_lock_outlives_a_call_to_the_model(tmp_path: Path) -> None:
    llm = {"base_url": "http://localhost:11434/v1", "model": "small-1", "timeout_seconds": 120}
    stale = run_module.lock_stale_after(settings(tmp_path / "stockthread.db", llm=llm)).total_seconds()
    assert stale == 150  # the call's deadline plus half a minute
    assert run_module.lock_stale_after(settings(tmp_path / "stockthread.db")) == timedelta(seconds=100)  # as before


# `[vehicles]`: whose vehicle "my truck" is.

SAM, LUIS, MIKE = "U0SAM0001", "U0LUIS0005", "U0MIKE0002"
FLEET = Catalog(
    parts=CATALOG.parts,
    locations=(
        LocationRecord(10, "Warehouse/Main Stock"),
        LocationRecord(11, "Vehicles/Truck 1"),
        LocationRecord(12, "Vehicles/Truck 2"),
        LocationRecord(13, "Vehicles", structural=True),
        LocationRecord(14, "Yard/Spare"),
        LocationRecord(15, "Yard/Spare"),  # InvenTree should never hold a path twice; if it does, neither is chosen
    ),
)


def test_a_vehicle_is_the_location_with_exactly_that_path() -> None:
    catalog, missing = assign_vehicles(
        FLEET, {SAM: "Vehicles/Truck 1", LUIS: "Vehicles/Truck 1", MIKE: "Vehicles/Truck 2"}
    )
    assert catalog.vehicles == {SAM: 11, LUIS: 11, MIKE: 12}  # two people may share one
    assert missing == ()
    assert (catalog.parts, catalog.locations) == (FLEET.parts, FLEET.locations)
    assert FLEET.vehicles == {}


@pytest.mark.parametrize(
    "path",
    [
        "Truck 1",  # the name alone: the whole path is asked for
        "vehicles/truck 1",
        "Vehicles/Truck 1/",
        "Vehicles / Truck 1",
        "Vehicles/Truck 10",
        "Vehicles/Truck",
        "Vehicles",  # structural: it cannot hold stock
        "Yard/Spare",  # twice in the snapshot
        "11",
    ],
)
def test_a_path_that_is_not_one_stock_location_gives_no_vehicle(path: str) -> None:
    catalog, missing = assign_vehicles(FLEET, {SAM: path, MIKE: "Vehicles/Truck 2"})
    assert catalog.vehicles == {MIKE: 12}
    assert missing == (SAM,)


def test_a_vehicle_that_is_not_found_is_logged_once_until_the_list_changes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    snapshots = [CATALOG, CATALOG, FLEET, FLEET, CATALOG]
    configured = settings(tmp_path / "stockthread.db", vehicles={SAM: "Vehicles/Truck 1", MIKE: "Vehicles/Truck 2"})
    read = with_vehicles(lambda: snapshots.pop(0), configured)
    assert read().vehicles == {SAM: 11}
    assert read().vehicles == {SAM: 11}
    assert [record.levelname for record in caplog.records] == ["WARNING"]
    assert MIKE in caplog.text
    assert SAM not in caplog.text
    assert "Truck 2" not in caplog.text
    assert read().vehicles == {SAM: 11, MIKE: 12}  # the location was created meanwhile
    assert read().vehicles == {SAM: 11, MIKE: 12}
    assert [record.levelname for record in caplog.records] == ["WARNING", "INFO"]
    assert read().vehicles == {SAM: 11}  # and removed again
    assert [record.levelname for record in caplog.records] == ["WARNING", "INFO", "WARNING"]


def test_without_a_vehicles_section_nothing_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    read = with_vehicles(lambda: CATALOG, settings(tmp_path / "stockthread.db"))
    assert read().vehicles == {}
    assert caplog.records == []


def test_a_catalog_outage_passes_through_the_vehicles(tmp_path: Path) -> None:
    def down() -> Catalog:
        raise InvenTreeError("GET /api/part/ failed: ConnectError")

    with pytest.raises(InvenTreeError):
        with_vehicles(down, settings(tmp_path / "stockthread.db", vehicles={SAM: "Vehicles/Truck 1"}))()


@pytest.mark.parametrize(
    ("vehicles", "matched"), [({SAM: "Vehicles/Truck 1"}, True), ({MIKE: "Vehicles/Truck 1"}, False)]
)
def test_my_truck_in_a_post_is_the_posters_vehicle_and_nobody_elses(
    tmp_path: Path, stop: threading.Event, vehicles: dict[str, str], matched: bool
) -> None:
    database = tmp_path / "stockthread.db"
    queued_message(database, "1790000000.000100", "took 2 Disc 40 from main stock to my truck")  # posted by Sam
    client = FakeClient(stop)
    configured = settings(database, vehicles=vehicles)
    code = run(
        configured,
        stop,
        slack_client=client,
        catalog=with_vehicles(lambda: CATALOG, configured),
        check=lambda _: "InvenTree 1.5.6 (API 530) is tested.",
        connect=lambda app, token: FakeConnection(),
        clock=lambda: datetime.fromtimestamp(1790000030, UTC),
    )
    assert code == 0
    store = Store.open(database)
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, "1790000000.000100"))
    store.close()
    assert record is not None
    assert record.result is not None
    destination = record.result["movements"][0]["destination"]
    if matched:
        assert (destination["status"], destination["rule"], destination["record"]) == ("matched", "alias", 11)
    else:
        assert (destination["status"], destination["record"]) == ("unmatched", None)


# Catch-up


def watched_since(database: Path, ts: str) -> None:
    """An earlier run made its first sweep of the channel at `ts`."""
    store = Store.open(database)
    store.mark_caught_up(WORKSPACE, CHANNEL, ts)
    store.close()


def test_a_post_made_while_the_connector_was_down_is_found_at_restart_and_answered_as_late(
    tmp_path: Path, stop: threading.Event
) -> None:
    database = tmp_path / "stockthread.db"
    watched_since(database, "1790000000.000000")
    client = FakeClient(stop)
    client.history = [
        {"type": "message", "user": "U0SAM0001", "text": "took 2 Disc 40 from main stock to truck 1", "ts": MISSED}
    ]
    assert run_with(database, stop, FakeConnection(), slack_client=client) == 0
    assert [reply["thread_ts"] for reply in client.posted] == [MISSED]
    assert "The post was read late" in json.dumps(client.posted[0]["blocks"])
    assert [(call["oldest"], call["latest"]) for call in client.listed] == [("1789740830.000000", "1790000030.000000")]
    assert lock_is_free(database)
    assert not [thread for thread in threading.enumerate() if thread.name == "catch-up"]


MISSED = "1790000010.000100"


def test_a_first_run_reads_no_history_and_with_zero_hours_no_run_does(tmp_path: Path, stop: threading.Event) -> None:
    database = tmp_path / "stockthread.db"
    queued_message(database, "1790000000.000100", "took 2 Disc 40 from main stock to truck 1")

    class AfterTheFirstSweep(FakeClient):
        def chat_postMessage(self, **kwargs: Any) -> Any:
            store = Store.open(database)
            deadline = time.monotonic() + 5
            while store.catch_up_mark(WORKSPACE, CHANNEL) is None and time.monotonic() < deadline:
                time.sleep(0.01)
            store.close()
            return super().chat_postMessage(**kwargs)

    client = AfterTheFirstSweep(stop)
    assert run_with(database, stop, FakeConnection(), slack_client=client) == 0
    store = Store.open(database)
    assert store.catch_up_mark(WORKSPACE, CHANNEL) == ("1790000030.000000", "1790000030.000000")
    store.close()
    assert client.listed == []

    off = tmp_path / "off.db"
    watched_since(off, "1790000000.000000")
    queued_message(off, "1790000000.000100", "took 2 Disc 40 from main stock to truck 1")
    stop.clear()
    code = run(
        settings(off, catch_up_hours=0),
        stop,
        slack_client=client,
        catalog=lambda: CATALOG,
        check=lambda _: "",
        connect=lambda app, token: FakeConnection(),
        clock=lambda: datetime.fromtimestamp(1790000030, UTC),
    )
    assert code == 0
    assert client.listed == []


def test_catch_up_sweeps_again_after_a_sweep_that_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    database = tmp_path / "stockthread.db"
    watched_since(database, "1790000000.000000")
    monkeypatch.setattr(run_module, "SWEEP_SECONDS", 0.01)
    halt = threading.Event()
    sweeps: list[Decimal | None] = []

    class Sweeps(FakeClient):
        def conversations_history(self, **kwargs: Any) -> Any:
            sweeps.append(Decimal(kwargs["latest"]))
            if len(sweeps) == 1:
                raise RuntimeError("took 2 Disc 40")  # not a class catch-up expects
            halt.set()
            return super().conversations_history(**kwargs)

    now = [1790000030]

    def clock() -> datetime:
        now[0] += 1000  # each later sweep stands further on
        return datetime.fromtimestamp(now[0], UTC)

    with caplog.at_level(logging.WARNING):
        run_module.catch_up(settings(database), Sweeps(halt), clock, Decimal(1790000030), halt)
    assert len(sweeps) == 2
    assert sweeps[0] == Decimal(1790000030)
    assert sweeps[1] == Decimal(now[0] - 120)  # the later sweeps leave the newest two minutes alone
    assert "failed (RuntimeError)" in caplog.text
    assert "Disc" not in caplog.text


def test_catch_up_that_cannot_open_the_database_keeps_trying_with_its_first_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    database = tmp_path / "stockthread.db"
    watched_since(database, "1790000000.000000")
    monkeypatch.setattr(run_module, "SWEEP_SECONDS", 0.01)
    halt = threading.Event()
    opened: list[int] = []
    real = Store.open

    def open_store(path: Any) -> Store:
        opened.append(1)
        if len(opened) == 1:
            raise sqlite3.OperationalError("synthetic")
        return real(path)

    monkeypatch.setattr(Store, "open", open_store)

    class Once(FakeClient):
        def conversations_history(self, **kwargs: Any) -> Any:
            halt.set()
            return super().conversations_history(**kwargs)

    client = Once(halt)
    clock = lambda: datetime.fromtimestamp(1790009000, UTC)  # noqa: E731
    with caplog.at_level(logging.WARNING):
        run_module.catch_up(settings(database), client, clock, Decimal(1790000030), halt)
    assert "catch-up failed (OperationalError)" in caplog.text
    assert [call["latest"] for call in client.listed] == ["1790000030.000000"]
