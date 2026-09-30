"""`stockthread run`: startup checks, the lock, the catalog cache, and one post from the queue to its reply."""

from __future__ import annotations

import json
import signal
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
from stockthread.catalog import load_catalog
from stockthread.config import InvenTreeSettings, Settings, parse_settings
from stockthread.core.matching import Catalog, LocationRecord, PartRecord
from stockthread.inventree import InvenTreeError, InvenTreeReader
from stockthread.run import CatalogCache, run
from stockthread.store import Store, new_lock_owner

WORKSPACE, APP, CHANNEL = "T0ACME001", "A0ACME001", "C0ACME001"
CATALOG = Catalog(
    parts=(PartRecord(1, "DISC-40", "Disc 40"),),
    locations=(LocationRecord(10, "Warehouse/Main Stock"), LocationRecord(11, "Vehicles/Truck 1")),
)
ENV = {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic", "INVENTREE_TOKEN": "inv-synthetic"}


def settings(database: Path, transport: str = "socket") -> Settings:
    data = {
        "slack": {"workspace_id": WORKSPACE, "app_id": APP, "channels": [CHANNEL], "transport": transport},
        "inventree": {"url": "http://inventree.local:8000"},
        "storage": {"database": str(database)},
    }
    return parse_settings(data, {**ENV, "SLACK_SIGNING_SECRET": "synthetic-secret"})


class FakeClient(WebClient):
    """Bolt takes only a real WebClient: the two calls the connector makes are replaced."""

    def __init__(self, stop: threading.Event, team: str = WORKSPACE) -> None:
        super().__init__()
        self.stop = stop
        self.team = team
        self.posted: list[dict[str, Any]] = []

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
    assert any(isinstance(handler, RateLimitErrorRetryHandler) for handler in client.retry_handlers)
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


def test_http_serving_is_refused_for_now(tmp_path: Path) -> None:
    assert run(settings(tmp_path / "stockthread.db", transport="http"), threading.Event()) == 2


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

    def connect(app: Any, token: str) -> FakeConnection:
        with sqlite3.connect(database) as raw:  # another instance takes the lock over
            raw.execute("UPDATE instance_lock SET owner = 'instance-b'")
        return connection

    assert run_with(database, stop, connection, connect=connect) == 1
    assert connection.calls == ["connect", "close"]
    with sqlite3.connect(database) as raw:
        assert raw.execute("SELECT owner FROM instance_lock").fetchall() == [("instance-b",)]


def test_a_failing_close_still_frees_the_lock(tmp_path: Path, stop: threading.Event) -> None:
    database = tmp_path / "stockthread.db"
    connection = FakeConnection()
    connection.close = lambda: _raise(OSError("synthetic"))  # type: ignore[method-assign]

    def connect(app: Any, token: str) -> FakeConnection:
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
