# SPDX-License-Identifier: MIT
"""The Slack fixtures are coherent, and the signature vectors agree with a reference implementation."""

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "slack"
EVENT_FILES = sorted((FIXTURES / "events").glob("*.json"))
DROP_REASONS = {
    "bot",
    "channel_not_allowed",
    "wrong_workspace",
    "wrong_app",
    "subtype_not_allowed",
    "duplicate_event",
    "not_authorized",
}
BOT_USER = "U0STOCKBOT"


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def catalog_users() -> set[str]:
    return {user["id"] for user in load(ROOT / "eval" / "catalog" / "users.json")["users"]}


def inner(fixture: dict[str, Any]) -> dict[str, Any]:
    """The Events API or interactivity payload, without a Socket Mode envelope."""
    body: dict[str, Any] = fixture["body"]
    return body["payload"] if fixture["transport"] == "socket" else body


def user_ids(value: Any) -> set[str]:
    """Every string in the payload that looks like a Slack user id."""
    if isinstance(value, dict):
        return set().union(*(user_ids(item) for item in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(user_ids(item) for item in value)) if value else set()
    if isinstance(value, str) and value.startswith("U0") and value.isalnum() and value.isupper():
        return {value}
    return set()


def test_there_are_fixtures() -> None:
    assert len(EVENT_FILES) >= 20


@pytest.mark.parametrize("path", EVENT_FILES, ids=lambda path: path.stem)
def test_fixture_structure(path: Path) -> None:
    fixture = load(path)
    assert set(fixture) == {"description", "kind", "transport", "headers", "body", "expected"}
    assert fixture["kind"] in {"event", "interactive"}
    assert fixture["transport"] in {"http", "socket"}
    expected = fixture["expected"]
    assert expected["action"] in {"process", "drop", "challenge"}
    if expected["action"] == "drop":
        assert expected["reason"] in DROP_REASONS
    if fixture["transport"] == "socket":
        assert fixture["headers"] == {}
        assert fixture["body"]["type"] == ("interactive" if fixture["kind"] == "interactive" else "events_api")


@pytest.mark.parametrize("path", EVENT_FILES, ids=lambda path: path.stem)
def test_fixture_uses_only_synthetic_people(path: Path) -> None:
    assert user_ids(load(path)["body"]) <= catalog_users() | {BOT_USER}


def test_event_ids_repeat_only_for_retries() -> None:
    # Each transport is a separate run: socket_message is message_plain delivered another way.
    seen: dict[tuple[str, str], str] = {}
    for path in EVENT_FILES:
        fixture = load(path)
        event_id = inner(fixture).get("event_id")
        if event_id is None:
            continue
        key = (fixture["transport"], event_id)
        if key in seen:
            assert fixture["expected"] == {"action": "drop", "reason": "duplicate_event"}, path.stem
            assert path.stem > seen[key], "a retry must sort after its original"
        else:
            assert fixture["expected"].get("reason") != "duplicate_event", path.stem
            seen[key] = path.stem


def test_every_drop_reason_has_a_fixture() -> None:
    reasons = {load(path)["expected"].get("reason") for path in EVENT_FILES}
    assert reasons >= DROP_REASONS


def reference_verify(secret: str, headers: dict[str, str], raw_body: str, now: int, max_age: int) -> bool:
    """The algorithm from Slack's documentation and its Python SDK. The connector's verifier must agree."""
    timestamp = headers.get("X-Slack-Request-Timestamp")
    signature = headers.get("X-Slack-Signature")
    if not timestamp or not signature or not timestamp.isdigit():
        return False
    if abs(now - int(timestamp)) > max_age:
        return False
    base = f"v0:{timestamp}:{raw_body}".encode()
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


SIGNATURES = load(FIXTURES / "signatures.json")


@pytest.mark.parametrize("case", SIGNATURES["cases"], ids=lambda case: case["name"])
def test_signature_vector(case: dict[str, Any]) -> None:
    result = reference_verify(
        SIGNATURES["signing_secret"], case["headers"], case["raw_body"], case["now"], SIGNATURES["max_age_seconds"]
    )
    assert result is case["valid"], case["why"]


def test_signature_vectors_cover_both_outcomes_and_both_body_kinds() -> None:
    cases = SIGNATURES["cases"]
    assert sum(case["valid"] for case in cases) >= 3
    assert sum(not case["valid"] for case in cases) >= 10
    assert any(case["raw_body"].startswith("payload=") for case in cases if case["valid"])
