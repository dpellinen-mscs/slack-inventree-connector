"""The Slack listener: every recorded payload through the real Bolt app, over both transports (design section 4)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import slack_sdk.signature
from slack_bolt import App
from slack_bolt.request import BoltRequest
from slack_bolt.response import BoltResponse

from stockthread.config import SlackSettings
from stockthread.slack.ingest import Click, Drop, DropReason, unwrap_socket
from stockthread.slack.listener import (
    BotIdentity,
    IdentityError,
    Listener,
    ThreadStores,
    build_app,
    identify,
)
from stockthread.store import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "slack"
EVENT_FILES = sorted((FIXTURES / "events").glob("*.json"))
SIGNATURES = json.loads((FIXTURES / "signatures.json").read_text(encoding="utf-8"))
SECRET = SIGNATURES["signing_secret"]
TOKEN = "xoxb-synthetic-bot-token"
IDENTITY = BotIdentity(team_id="T0ACME001", bot_id="B0STOCKBOT", bot_user_id="U0STOCKBOT")
SETTINGS = {"workspace_id": "T0ACME001", "app_id": "A0ACME001", "channels": ("C0ACME001",)}
APPROVERS = ("U0LUIS0005",)


def settings(transport: str) -> SlackSettings:
    return SlackSettings(transport=transport, approvers=APPROVERS, **SETTINGS)  # type: ignore[arg-type]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "stockthread.db"


@pytest.fixture
def stores(db: Path) -> Iterator[Callable[[], Store]]:
    opened = Store.open(db)
    yield lambda: opened
    opened.close()


def app_for(transport: str, stores: Callable[[], Store]) -> App:
    return build_app(
        settings(transport),
        IDENTITY,
        Listener(settings(transport), stores),
        bot_token=TOKEN,
        signing_secret=SECRET if transport == "http" else None,
    )


def stored(db: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(db) as connection:
        return [(row[0], row[1]) for row in connection.execute("SELECT event_key, kind FROM slack_events ORDER BY id")]


def sign(raw: str, timestamp: int, secret: str = SECRET) -> dict[str, str]:
    digest = hmac.new(secret.encode(), f"v0:{timestamp}:{raw}".encode(), hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": str(timestamp), "X-Slack-Signature": f"v0={digest}"}


def http_request(fixture: dict[str, Any]) -> BoltRequest:
    body = fixture["body"]
    if fixture["kind"] == "interactive":
        raw = "payload=" + urllib.parse.quote(json.dumps(body))
        content_type = "application/x-www-form-urlencoded"
    else:
        raw = json.dumps(body)
        content_type = "application/json"
    headers = {"Content-Type": content_type, **fixture.get("headers", {}), **sign(raw, int(time.time()))}
    return BoltRequest(body=raw, headers=headers)


def socket_request(fixture: dict[str, Any]) -> BoltRequest:
    payload = unwrap_socket(fixture["body"])
    assert payload is not None
    return BoltRequest(body=dict(payload), mode="socket_mode")


def expected_key(fixture: dict[str, Any]) -> str:
    body = unwrap_socket(fixture["body"]) if fixture["transport"] == "socket" else fixture["body"]
    assert body is not None
    if fixture["kind"] == "event":
        return str(body["event_id"])
    action = body["actions"][0]
    return f"click:{body['channel']['id']}:{body['message']['ts']}:{body['user']['id']}:{action['action_ts']}"


@pytest.mark.parametrize("transport", ["http", "socket"])
def test_every_fixture_is_acknowledged_and_only_accepted_payloads_are_stored(
    transport: str, db: Path, stores: Callable[[], Store]
) -> None:
    app = app_for(transport, stores)
    expected: list[tuple[str, str]] = []
    fixtures = [json.loads(path.read_text(encoding="utf-8")) for path in EVENT_FILES]
    for fixture in (fixture for fixture in fixtures if fixture["transport"] == transport):
        request = http_request(fixture) if transport == "http" else socket_request(fixture)
        response = app.dispatch(request)
        assert response.status == 200, fixture["description"]
        if fixture["expected"]["action"] == "challenge":
            assert fixture["body"]["challenge"] in response.body
        if fixture["expected"]["action"] == "process":
            expected.append((expected_key(fixture), fixture["kind"]))
        assert stored(db) == expected, fixture["description"]
    assert expected, "the fixtures of each transport include accepted payloads"


@pytest.mark.parametrize("case", SIGNATURES["cases"], ids=[case["name"] for case in SIGNATURES["cases"]])
def test_signature_vectors_over_http(
    case: dict[str, Any], db: Path, stores: Callable[[], Store], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(slack_sdk.signature.Clock, "now", lambda self: float(case["now"]))
    content_type = (
        "application/x-www-form-urlencoded" if case["raw_body"].startswith("payload=") else "application/json"
    )
    request = BoltRequest(body=case["raw_body"], headers={"Content-Type": content_type, **case["headers"]})
    response = app_for("http", stores).dispatch(request)
    if case["valid"]:
        assert response.status == 200
        assert len(stored(db)) == 1
    else:
        # 401, except a timestamp that is not a number, which the SDK fails on (500): refused either way.
        assert response.status == (500 if case["name"] == "timestamp_not_a_number" else 401)
        assert stored(db) == []


def test_an_unsigned_http_request_is_refused(db: Path, stores: Callable[[], Store]) -> None:
    fixture = json.loads((FIXTURES / "events" / "message_plain.json").read_text(encoding="utf-8"))
    request = BoltRequest(body=json.dumps(fixture["body"]), headers={"Content-Type": "application/json"})
    assert app_for("http", stores).dispatch(request).status == 401
    assert stored(db) == []


def test_http_mode_needs_a_signing_secret(stores: Callable[[], Store]) -> None:
    with pytest.raises(ValueError, match="signing secret"):
        build_app(settings("http"), IDENTITY, Listener(settings("http"), stores), bot_token=TOKEN, signing_secret=None)


def test_an_event_that_cannot_be_stored_is_not_acknowledged(db: Path, stores: Callable[[], Store]) -> None:
    """Slack delivers an unacknowledged event again, and the retry is stored."""
    fixture = json.loads((FIXTURES / "events" / "message_plain.json").read_text(encoding="utf-8"))
    fail = True

    def flaky() -> Store:
        if fail:
            raise sqlite3.OperationalError("disk I/O error")
        return stores()

    app = app_for("http", flaky)
    response: BoltResponse = app.dispatch(http_request(fixture))
    assert response.status != 200
    fail = False
    assert app.dispatch(http_request(fixture)).status == 200
    assert stored(db) == [(fixture["body"]["event_id"], "event")]


def click_body() -> dict[str, Any]:
    body: dict[str, Any] = json.loads((FIXTURES / "events" / "block_actions_apply.json").read_text(encoding="utf-8"))[
        "body"
    ]
    return body


def test_each_click_is_stored_once(db: Path, stores: Callable[[], Store]) -> None:
    listener = Listener(settings("http"), stores)
    first = click_body()
    assert isinstance(listener.on_action(first), Click)
    assert listener.on_action(first) == Drop(DropReason.DUPLICATE_EVENT)
    second = click_body()
    second["actions"][0]["action_ts"] = "1790000301.000500"
    assert isinstance(listener.on_action(second), Click)
    assert len(stored(db)) == 2


@pytest.mark.parametrize("action_ts", [None, "", "soon", 1790000300, "1790000300.000400 "])
def test_a_click_without_a_valid_action_ts_is_dropped(action_ts: object, db: Path, stores: Callable[[], Store]) -> None:
    body = click_body()
    body["actions"][0]["action_ts"] = action_ts
    assert Listener(settings("http"), stores).on_action(body) == Drop(DropReason.MALFORMED)
    assert stored(db) == []


def test_each_thread_gets_its_own_store_connection(db: Path) -> None:
    stores = ThreadStores(db)
    here = stores()
    assert stores() is here
    there: list[bool] = []

    def other_thread() -> None:
        store = stores()
        there.append(store is not here and store is stores())
        store.close()

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join()
    assert there == [True]
    here.close()


class FakeClient:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result

    def auth_test(self) -> dict[str, Any]:
        return self.result


def test_identify_checks_the_workspace_of_the_token() -> None:
    result = {"ok": True, "team_id": "T0ACME001", "bot_id": "B0STOCKBOT", "user_id": "U0STOCKBOT"}
    assert identify(FakeClient(result), settings("socket")) == IDENTITY
    with pytest.raises(IdentityError, match="another workspace"):
        identify(FakeClient({**result, "team_id": "T0OTHER01"}), settings("socket"))
    with pytest.raises(IdentityError, match="bot token"):
        identify(FakeClient({"ok": True, "team_id": "T0ACME001", "user_id": "U0SAM0001"}), settings("socket"))


def other_event() -> dict[str, Any]:
    body = json.loads((FIXTURES / "events" / "message_plain.json").read_text(encoding="utf-8"))["body"]
    body["event"] = {"type": "member_joined_channel", "user": "U0SAM0001", "channel": "C0ACME001"}
    body["event_id"] = "Ev0ACME9001"
    return dict(body)


def test_other_event_types_are_acknowledged_and_not_stored(db: Path, stores: Callable[[], Store]) -> None:
    raw = json.dumps(other_event())
    request = BoltRequest(body=raw, headers={"Content-Type": "application/json", **sign(raw, int(time.time()))})
    assert app_for("http", stores).dispatch(request).status == 200
    assert app_for("socket", stores).dispatch(BoltRequest(body=other_event(), mode="socket_mode")).status == 200
    assert stored(db) == []


def test_an_app_answers_only_its_own_transport(db: Path, stores: Callable[[], Store]) -> None:
    fixture = json.loads((FIXTURES / "events" / "message_plain.json").read_text(encoding="utf-8"))
    assert app_for("socket", stores).dispatch(http_request(fixture)).status == 401
    assert app_for("http", stores).dispatch(BoltRequest(body=fixture["body"], mode="socket_mode")).status == 401
    assert stored(db) == []


def test_a_bot_token_in_the_environment_causes_no_warning_from_bolt(
    stores: Callable[[], Store], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The quickstart exports SLACK_BOT_TOKEN. Bolt reads it on its own and warns twice that it is unused.
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKEN)
    logger = logging.getLogger("stockthread.slack.listener")
    with caplog.at_level(logging.WARNING):
        app_for("socket", stores)
        assert caplog.records == []
        logger.warning("As you gave `client` as well, `token` will be unused.")
    assert len(caplog.records) == 1  # only while the app is built: the filter is gone afterwards
