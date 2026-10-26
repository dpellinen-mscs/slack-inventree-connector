"""Socket Mode end to end: the real `SocketModeHandler` from `serve`, against a local fake of Slack's endpoint
(`tests/support/fake_socket_mode.py`). The connection breaks the ways it does in the field, and every post is still
answered exactly once."""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fake_socket_mode import FakeSocketMode
from slack_sdk import WebClient
from slack_sdk.socket_mode.builtin import SocketModeClient

from stockthread import run as run_module
from stockthread.config import parse_settings
from stockthread.core.matching import Catalog, LocationRecord, PartRecord
from stockthread.core.resolve import StockItem
from stockthread.inventree import ServerInfo, check_version
from stockthread.run import run

WORKSPACE, APP, CHANNEL = "T0ACME001", "A0ACME001", "C0ACME001"
CATALOG = Catalog(
    parts=(PartRecord(1, "DISC-40", "Disc 40"),),
    locations=(LocationRecord(10, "Warehouse/Main Stock"), LocationRecord(11, "Vehicles/Truck 1")),
)
ENV = {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic", "INVENTREE_TOKEN": "inv-synthetic"}
TESTED = check_version(ServerInfo("1.5.6", 530))
WAIT_SECONDS = 30.0
QUICK_PING_SECONDS = 1.0
"""slack_sdk notices a dead link only on its ping check, every 10 seconds by default: two checks after a drop,
twenty seconds. These tests check every second instead; the connection is otherwise the one `serve` makes."""


class SlackClient(WebClient):
    """Bolt takes only a real WebClient: the calls the connector makes are replaced, and `apps.connections.open`
    names the fake endpoint."""

    def __init__(self, endpoint: FakeSocketMode) -> None:
        super().__init__()
        self.endpoint = endpoint
        self.posted: list[dict[str, Any]] = []
        self.opened = 0
        self.changed = threading.Condition()

    def auth_test(self, **kwargs: Any) -> Any:
        return {"team_id": WORKSPACE, "bot_id": "B0STOCKBOT", "user_id": "U0STOCKBOT"}

    def apps_connections_open(self, **kwargs: Any) -> Any:
        self.opened += 1
        return {"ok": True, "url": self.endpoint.url}

    def chat_postMessage(self, **kwargs: Any) -> Any:
        with self.changed:
            self.posted.append(kwargs)
            self.changed.notify_all()
            return {"ok": True, "ts": f"1790000099.{len(self.posted):06d}"}

    def replies_to(self) -> list[str]:
        with self.changed:
            return [reply["thread_ts"] for reply in self.posted]

    def wait_replies(self, count: int) -> list[str]:
        with self.changed:
            if not self.changed.wait_for(lambda: len(self.posted) >= count, WAIT_SECONDS):
                raise TimeoutError(f"{len(self.posted)} replies, waited for {count}")
        return self.replies_to()


def body(ts: str, text: str = "took 2 Disc 40 from main stock to truck 1") -> dict[str, Any]:
    event = {"type": "message", "channel": CHANNEL, "user": "U0SAM0001", "text": text, "ts": ts}
    return {"type": "event_callback", "team_id": WORKSPACE, "api_app_id": APP, "event_id": f"Ev{ts}", "event": event}


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_module, "POLL_SECONDS", 0.01)
    made = SocketModeClient.__init__

    def quick_ping(client: SocketModeClient, *args: Any, **kwargs: Any) -> None:
        made(client, *args, **{**kwargs, "ping_interval": QUICK_PING_SECONDS})

    monkeypatch.setattr(SocketModeClient, "__init__", quick_ping)


@pytest.fixture
def endpoint() -> Iterator[FakeSocketMode]:
    with FakeSocketMode() as fake:
        yield fake


@pytest.fixture
def running(tmp_path: Path, endpoint: FakeSocketMode) -> Iterator[SlackClient]:
    """The connector over Socket Mode as `serve` connects it; stopped and checked for a clean exit afterwards."""
    data = {
        "slack": {"workspace_id": WORKSPACE, "app_id": APP, "channels": [CHANNEL], "catch_up_hours": 0},
        "inventree": {"url": "http://inventree.local:8000"},
        "storage": {"database": str(tmp_path / "stockthread.db")},
    }
    settings = parse_settings(data, ENV)
    client, stop, codes = SlackClient(endpoint), threading.Event(), []
    serving = threading.Thread(
        target=lambda: codes.append(
            run(
                settings,
                stop,
                slack_client=client,
                catalog=lambda: CATALOG,
                stock=lambda pairs: {pair: [StockItem(100, Decimal(50), 10, Decimal(0))] for pair in pairs},
                check=lambda _: TESTED,
                clock=lambda: datetime.fromtimestamp(1790000030, UTC),
            )
        ),
        name="connector",
    )
    serving.start()
    try:
        endpoint.wait_connected()
        yield client
    finally:
        stop.set()
        serving.join(WAIT_SECONDS)
    assert not serving.is_alive()
    assert codes == [0]


def test_a_post_over_socket_mode_is_acked_and_answered(endpoint: FakeSocketMode, running: SlackClient) -> None:
    endpoint.wait_acked(endpoint.deliver(body("1790000000.000100")))
    assert running.wait_replies(1) == ["1790000000.000100"]


def test_after_a_dropped_connection_the_client_reconnects_and_the_next_post_is_answered(
    endpoint: FakeSocketMode, running: SlackClient
) -> None:
    endpoint.wait_acked(endpoint.deliver(body("1790000000.000100")))
    endpoint.drop()
    endpoint.wait_connected(2)
    endpoint.wait_acked(endpoint.deliver(body("1790000001.000100")))
    assert running.wait_replies(2) == ["1790000000.000100", "1790000001.000100"]
    assert running.opened == 2  # a new URL for the new connection


def test_slack_s_refresh_request_moves_the_client_to_a_new_connection(
    endpoint: FakeSocketMode, running: SlackClient
) -> None:
    endpoint.refresh()
    endpoint.wait_connected(2)
    endpoint.wait_acked(endpoint.deliver(body("1790000000.000100")))
    assert running.wait_replies(1) == ["1790000000.000100"]


def test_a_post_sent_while_the_link_was_dead_comes_again_on_the_new_one_and_is_answered_once(
    endpoint: FakeSocketMode, running: SlackClient
) -> None:
    endpoint.go_deaf()
    lost = endpoint.deliver(body("1790000000.000100"))
    endpoint.drop()
    endpoint.wait_connected(2)
    endpoint.wait_acked(lost)
    assert running.wait_replies(1) == ["1790000000.000100"]


def test_a_post_slack_delivers_twice_is_stored_and_answered_once(
    tmp_path: Path, endpoint: FakeSocketMode, running: SlackClient
) -> None:
    """Slack sends an envelope again when it missed the ack. Both copies carry the same event id: the second is
    acked and dropped before the queue."""
    endpoint.wait_acked(endpoint.deliver(body("1790000000.000100")))
    running.wait_replies(1)
    endpoint.wait_acked(endpoint.deliver(body("1790000000.000100")))
    endpoint.wait_acked(endpoint.deliver(body("1790000001.000100")))
    assert running.wait_replies(2) == ["1790000000.000100", "1790000001.000100"]
    with contextlib.closing(sqlite3.connect(tmp_path / "stockthread.db")) as db:
        assert db.execute("SELECT event_key FROM slack_events ORDER BY id").fetchall() == [
            ("Ev1790000000.000100",),
            ("Ev1790000001.000100",),
        ]


def test_a_link_that_stays_open_but_answers_nothing_is_replaced_and_its_post_answered(
    endpoint: FakeSocketMode, running: SlackClient
) -> None:
    """A half-open connection, as after a network change: no close, no pong. Only the client's ping check sees
    it, after four missed intervals, and only once a pong has come: slack_sdk never calls a link stale that went
    dead before its first one."""
    endpoint.wait_ponged()
    endpoint.go_deaf()
    lost = endpoint.deliver(body("1790000000.000100"))
    endpoint.wait_connected(2, timeout=WAIT_SECONDS)
    endpoint.wait_acked(lost)
    assert running.wait_replies(1) == ["1790000000.000100"]
