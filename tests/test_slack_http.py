# SPDX-License-Identifier: MIT
"""HTTP serving: recorded payloads over a real socket, through waitress and the real Bolt app (ADR 0013)."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import socket
import sqlite3
import threading
import time
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from waitress import wasyncore

from stockthread.config import SlackHttpSettings, SlackSettings
from stockthread.slack import http_server
from stockthread.slack.http_server import HttpServer, ServeError
from stockthread.slack.listener import BotIdentity, Listener, ThreadStores, build_app

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "slack"
SECRET = json.loads((FIXTURES / "signatures.json").read_text(encoding="utf-8"))["signing_secret"]
IDENTITY = BotIdentity(team_id="T0ACME001", bot_id="B0STOCKBOT", bot_user_id="U0STOCKBOT")
SLACK = SlackSettings(
    transport="http",
    workspace_id="T0ACME001",
    app_id="A0ACME001",
    channels=("C0ACME001",),
    approvers=("U0LUIS0005",),
)
LOCAL = SlackHttpSettings.model_construct(port=0)  # the system picks a free port: the settings refuse 0


def fixture(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / "events" / f"{name}.json").read_text(encoding="utf-8"))
    return loaded


def server_for(db: Path, settings: SlackHttpSettings = LOCAL) -> HttpServer:
    listener = Listener(SLACK, ThreadStores(db))
    app = build_app(SLACK, IDENTITY, listener, bot_token="xoxb-synthetic", signing_secret=SECRET)
    return HttpServer(app, settings, SECRET)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "stockthread.db"


@pytest.fixture
def server(db: Path) -> Iterator[HttpServer]:
    served = server_for(db)
    served.connect()
    yield served
    served.close()


def send(
    server: HttpServer,
    raw: str,
    *,
    signed: bool = True,
    secret: str = SECRET,
    age: int = 0,
    path: str = "/slack/events",
    method: str = "POST",
    timestamp: str | None = None,
) -> tuple[int, bytes]:
    timestamp = str(int(time.time()) - age) if timestamp is None else timestamp
    content_type = "application/x-www-form-urlencoded" if raw.startswith("payload=") else "application/json"
    headers = {"Content-Type": content_type}
    if signed:
        digest = hmac.new(secret.encode(), f"v0:{timestamp}:{raw}".encode(), hashlib.sha256).hexdigest()
        headers |= {"X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": f"v0={digest}"}
    assert server.port is not None
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        connection.request(method, path, body=raw.encode(), headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def body_of(name: str) -> str:
    loaded = fixture(name)
    if loaded["kind"] == "interactive":
        return "payload=" + urllib.parse.quote(json.dumps(loaded["body"]))
    return json.dumps(loaded["body"])


def stored(db: Path) -> list[tuple[str, str]]:
    if not db.exists():
        return []
    with sqlite3.connect(db) as connection:
        return [(row[0], row[1]) for row in connection.execute("SELECT event_key, kind FROM slack_events ORDER BY id")]


def test_a_signed_message_is_stored_once_even_when_slack_delivers_it_again(server: HttpServer, db: Path) -> None:
    raw = body_of("message_plain")
    assert send(server, raw)[0] == 200
    assert send(server, raw)[0] == 200  # a retry: acknowledged, not stored twice
    assert stored(db) == [(fixture("message_plain")["body"]["event_id"], "event")]


def test_a_signed_click_is_stored(server: HttpServer, db: Path) -> None:
    assert send(server, body_of("block_actions_apply"))[0] == 200
    assert [kind for _, kind in stored(db)] == ["interactive"]


@pytest.mark.parametrize(
    "how",
    [{"signed": False}, {"secret": "another-synthetic-secret"}, {"age": 10 * 60}],
    ids=["unsigned", "wrong secret", "stale"],
)
def test_a_request_without_a_valid_signature_is_refused_and_not_stored(
    server: HttpServer, db: Path, how: dict[str, Any]
) -> None:
    assert send(server, body_of("message_plain"), **how)[0] == 401
    assert send(server, body_of("url_verification"), **how)[0] == 401
    assert stored(db) == []


@pytest.mark.parametrize("timestamp", ["not-a-number", "1e3", "", "1.5", "-1", "0x10", "²", "9" * 400])
def test_a_timestamp_that_is_not_plain_digits_is_refused_and_never_logged(
    server: HttpServer, db: Path, caplog: pytest.LogCaptureFixture, timestamp: str
) -> None:
    """Signed with the right secret over that very timestamp: still refused, as a 401 and not a 500 whose
    traceback would quote the header."""
    caplog.set_level(logging.DEBUG)
    assert send(server, body_of("message_plain"), timestamp=timestamp)[0] == 401
    assert "Traceback" not in caplog.text
    assert "Exception while serving" not in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert stored(db) == []


def test_the_url_verification_is_answered_with_the_challenge(server: HttpServer, db: Path) -> None:
    status, body = send(server, body_of("url_verification"))
    assert status == 200
    assert fixture("url_verification")["body"]["challenge"] in body.decode()
    assert stored(db) == []


@pytest.mark.parametrize(("method", "path"), [("POST", "/"), ("POST", "/slack/events/x"), ("GET", "/slack/events")])
def test_only_a_post_to_the_configured_path_reaches_bolt(server: HttpServer, db: Path, method: str, path: str) -> None:
    assert send(server, body_of("message_plain"), method=method, path=path)[0] == 404
    assert stored(db) == []


def test_the_path_can_be_set(db: Path) -> None:
    served = server_for(db, SlackHttpSettings.model_construct(port=0, path="/hooks/stock"))
    served.connect()
    try:
        assert send(served, body_of("message_plain"), path="/slack/events")[0] == 404
        assert send(served, body_of("message_plain"), path="/hooks/stock")[0] == 200
    finally:
        served.close()
    assert len(stored(db)) == 1


def test_a_body_over_the_limit_is_refused_before_bolt_reads_it(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_server, "MAX_BODY_BYTES", 64)
    served = server_for(db)
    served.connect()
    try:
        assert send(served, body_of("message_plain"))[0] == 413
    finally:
        served.close()
    assert stored(db) == []


def test_close_stops_serving_and_frees_the_port(db: Path) -> None:
    served = server_for(db)
    served.connect()
    port = served.port
    assert port is not None
    idle = http.client.HTTPConnection("127.0.0.1", port, timeout=10)  # a kept-alive connection must not hold it up
    idle.request("POST", "/", body=b"")
    idle.getresponse().read()
    served.close()
    assert not any(thread.name == "http" and thread.is_alive() for thread in threading.enumerate())
    again = server_for(db, SlackHttpSettings(port=port))
    again.connect()  # the address is free again
    again.close()
    idle.close()


def test_an_address_in_use_is_a_serve_error_that_names_only_the_address(db: Path) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        served = server_for(db, SlackHttpSettings(port=port))
        with pytest.raises(ServeError) as raised:
            served.connect()
    assert str(raised.value).startswith(f"cannot listen on 127.0.0.1:{port} (OSError, errno ")
    served.close()  # never connected: nothing to stop


def test_a_refused_request_leaves_nothing_of_its_body_in_the_log(
    server: HttpServer, db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for raw in (body_of("message_plain"), body_of("block_actions_apply"), '{"not json', "payload=%ZZ"):
        for how in ({"signed": False}, {"secret": "another-synthetic-secret"}, {"age": 10 * 60}):
            assert send(server, raw, **how)[0] == 401  # never 500: an unsigned body is not parsed
    assert "took" not in caplog.text
    assert "not json" not in caplog.text
    assert "Ev" not in caplog.text
    assert stored(db) == []


def test_a_serving_thread_that_dies_is_no_longer_alive(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served = server_for(db)
    assert served.alive()  # not connected yet
    served.connect()
    assert served.alive()

    def broken(**_: Any) -> None:
        raise OSError("synthetic")

    monkeypatch.setattr(wasyncore, "loop", broken)
    deadline = time.monotonic() + 5
    while served.alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not served.alive()
    served.close()
    assert not served.alive()


def test_a_host_that_does_not_resolve_is_a_serve_error_that_leaves_nothing_running(db: Path) -> None:
    before = threading.active_count()
    served = server_for(db, SlackHttpSettings.model_construct(port=0, host="nonexistent.invalid"))
    with pytest.raises(ServeError, match=r"^cannot listen on nonexistent\.invalid:0 \(ValueError, not a valid"):
        served.connect()
    assert threading.active_count() == before
