# SPDX-License-Identifier: MIT
"""Ingest against every Slack fixture's expected result, plus what the fixtures do not cover."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from stockthread.config import SlackSettings
from stockthread.slack import ingest
from stockthread.slack.ingest import (
    Challenge,
    Click,
    Deleted,
    Drop,
    DropReason,
    Edited,
    NewMessage,
    RecentEvents,
    Revised,
    ingest_action,
    ingest_event,
    revised_message,
    unwrap_socket,
)

EVENTS = Path(__file__).resolve().parent / "fixtures" / "slack" / "events"
SETTINGS = SlackSettings(
    workspace_id="T0ACME001", app_id="A0ACME001", channels=("C0ACME001",), approvers=("U0LUIS0005",)
)


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def payload_of(fixture: dict[str, Any]) -> Any:
    if fixture["transport"] == "socket":
        return unwrap_socket(fixture["body"])
    return fixture["body"]


def run(fixture: dict[str, Any], seen: RecentEvents) -> Any:
    payload = payload_of(fixture)
    if fixture["kind"] == "interactive":
        return ingest_action(payload, SETTINGS)
    return ingest_event(payload, SETTINGS, seen)


@pytest.mark.parametrize("transport", ["http", "socket"])
def test_every_fixture_gets_its_expected_result(transport: str) -> None:
    seen = RecentEvents()  # one per transport, fed in file name order, as the fixture README says
    checked = 0
    for path in sorted(EVENTS.glob("*.json")):
        fixture = load(path)
        if fixture["transport"] != transport:
            continue
        expected, got = fixture["expected"], run(fixture, seen)
        checked += 1
        match expected["action"]:
            case "drop":
                assert got == Drop(DropReason(expected["reason"])), path.name
            case "challenge":
                assert got == Challenge(expected["challenge"]), path.name
            case "process" if fixture["kind"] == "interactive":
                assert isinstance(got, Click), path.name
                assert (got.user, got.value) == (expected["user"], expected["button"])
            case "process":
                assert isinstance(got, NewMessage | Edited | Deleted), path.name
                if "edit_of" in expected:
                    assert isinstance(got, Edited)
                    assert got.message.ts == expected["edit_of"]
                elif "delete_of" in expected:
                    assert got == Deleted(got.event_id, "C0ACME001", expected["delete_of"])
                else:
                    assert isinstance(got, NewMessage)
                    assert got.message.thread_ts == expected.get("thread_of")
    assert checked >= 3


def test_a_message_keeps_its_files_and_an_edit_its_edit_time() -> None:
    shared = ingest_event(load(EVENTS / "message_file_share.json")["body"], SETTINGS, RecentEvents())
    assert isinstance(shared, NewMessage)
    assert shared.message.file_ids == ("F0ACME0001",)
    edit = ingest_event(load(EVENTS / "message_changed.json")["body"], SETTINGS, RecentEvents())
    assert isinstance(edit, Edited)
    assert (edit.message.text, edit.message.edited_ts) == ("3 Disc 40 on my truck", "1790000200.000000")


def test_an_edit_of_a_bot_message_is_a_bot_message() -> None:
    body = copy.deepcopy(load(EVENTS / "message_changed.json")["body"])
    body["event"]["message"]["bot_id"] = "B0STOCKBOT"
    assert ingest_event(body, SETTINGS, RecentEvents()) == Drop(DropReason.BOT)


def test_an_empty_allowlist_drops_every_channel() -> None:
    nowhere = SETTINGS.model_copy(update={"channels": ()})
    body = load(EVENTS / "message_plain.json")["body"]
    assert ingest_event(body, nowhere, RecentEvents()) == Drop(DropReason.CHANNEL_NOT_ALLOWED)


def test_a_dropped_event_is_not_remembered_as_seen() -> None:
    # A retry of an event dropped for its channel is dropped for its channel again, not as a duplicate.
    seen = RecentEvents()
    body = load(EVENTS / "message_other_channel.json")["body"]
    assert ingest_event(body, SETTINGS, seen) == ingest_event(body, SETTINGS, seen)


@pytest.mark.parametrize(
    "damage",
    [
        lambda body: body.update(event="not an object"),
        lambda body: body["event"].update(text=["not", "text"]),
        lambda body: body["event"].update(ts=None),
        lambda body: body["event"].update(files="F1"),
        lambda body: body["event"].update(files=[{"id": 7}]),
        lambda body: body.pop("event_id"),
    ],
)
def test_malformed_payloads_are_dropped_not_raised(damage: Any) -> None:
    body = copy.deepcopy(load(EVENTS / "message_plain.json")["body"])
    damage(body)
    assert ingest_event(body, SETTINGS, RecentEvents()) == Drop(DropReason.MALFORMED)


def test_only_apply_needs_an_approver() -> None:
    body = copy.deepcopy(load(EVENTS / "block_actions_not_approver.json")["body"])
    assert ingest_action(body, SETTINGS) == Drop(DropReason.NOT_AUTHORIZED)
    body["actions"][0]["action_id"] = "answer"  # the poster may answer a question; review checks who posted
    click = ingest_action(body, SETTINGS)
    assert isinstance(click, Click)
    assert click.action_id == "answer"


@pytest.mark.parametrize("action_id", ["answer-0", "answer-7", "answer-99"])
def test_a_numbered_question_button_is_an_answer(action_id: str) -> None:
    body = copy.deepcopy(load(EVENTS / "block_actions_not_approver.json")["body"])
    body["actions"][0]["action_id"] = action_id
    click = ingest_action(body, SETTINGS)
    assert isinstance(click, Click)
    assert click.action_id == "answer"


def test_clicks_from_another_workspace_app_or_channel_are_dropped() -> None:
    for path, reason in (
        (("team", "id"), DropReason.WRONG_WORKSPACE),
        (("api_app_id",), DropReason.WRONG_APP),
        (("channel", "id"), DropReason.CHANNEL_NOT_ALLOWED),
    ):
        body = copy.deepcopy(load(EVENTS / "block_actions_apply.json")["body"])
        target = body
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = "X0OTHER999"
        assert ingest_action(body, SETTINGS) == Drop(reason)


def test_recent_events_forgets_the_oldest() -> None:
    seen = RecentEvents(size=2)
    # A repeat refreshes an id, so after "a" again, "b" is the oldest and "c" pushes it out.
    assert [seen.first_time(e) for e in ("a", "b", "a", "c", "a", "b")] == [True, True, False, True, False, True]


def test_other_socket_envelopes_have_no_payload() -> None:
    assert unwrap_socket({"type": "hello"}) is None
    assert unwrap_socket({"type": "events_api", "payload": "x"}) is None


def plain() -> dict[str, Any]:
    return copy.deepcopy(load(EVENTS / "message_plain.json")["body"])


def changed() -> dict[str, Any]:
    return copy.deepcopy(load(EVENTS / "message_changed.json")["body"])


def test_a_malformed_copy_does_not_mark_the_event_as_seen() -> None:
    seen = RecentEvents()
    broken = plain()
    broken["event"]["ts"] = None
    assert ingest_event(broken, SETTINGS, seen) == Drop(DropReason.MALFORMED)
    assert isinstance(ingest_event(plain(), SETTINGS, seen), NewMessage)


@pytest.mark.parametrize("key", ["user_team", "team"])
def test_a_poster_from_another_organization_is_dropped(key: str) -> None:
    body = plain()
    body["event"]["is_ext_shared_channel"] = True
    body["event"][key] = "T0OTHER999"
    assert ingest_event(body, SETTINGS, RecentEvents()) == Drop(DropReason.WRONG_WORKSPACE)
    body["event"][key] = "T0ACME001"
    assert isinstance(ingest_event(body, SETTINGS, RecentEvents()), NewMessage)


def test_a_click_from_another_organization_is_dropped() -> None:
    body = copy.deepcopy(load(EVENTS / "block_actions_apply.json")["body"])
    body["user"]["team_id"] = "T0OTHER999"
    assert ingest_action(body, SETTINGS) == Drop(DropReason.WRONG_WORKSPACE)


def test_an_edit_by_a_bot_is_dropped() -> None:
    body = changed()
    body["event"]["message"]["subtype"] = "bot_message"
    assert ingest_event(body, SETTINGS, RecentEvents()) == Drop(DropReason.BOT)


@pytest.mark.parametrize("subtype", ["channel_join", "tombstone"])  # tombstone: a deleted parent with replies
def test_an_edit_that_is_no_longer_a_post_is_kept_as_a_revision(subtype: str) -> None:
    body = changed()
    body["event"]["message"]["subtype"] = subtype
    seen = RecentEvents()
    assert ingest_event(body, SETTINGS, seen) == Revised(body["event_id"], "C0ACME001", body["event"]["message"]["ts"])
    assert ingest_event(body, SETTINGS, seen) == Drop(DropReason.DUPLICATE_EVENT)


def test_a_change_without_an_edit_is_not_an_edit() -> None:
    body = changed()  # a link preview arrives as message_changed with no "edited" and the same text
    event = body["event"]
    del event["message"]["edited"]
    event["message"]["text"] = event["previous_message"]["text"]
    event["message"]["attachments"] = [{"title": "synthetic preview"}]
    assert ingest_event(body, SETTINGS, RecentEvents()) == Drop(DropReason.UNCHANGED)


@pytest.mark.parametrize("missing", ["edited", "user", "previous_message"])
def test_a_changed_text_the_checks_cannot_read_in_full_is_kept_as_a_revision(missing: str) -> None:
    body = changed()
    event = body["event"]
    if missing == "previous_message":
        del event["message"]["edited"], event["previous_message"]  # nothing says what was there before
    else:
        del event["message"][missing]
    assert ingest_event(body, SETTINGS, RecentEvents()) == Revised(
        body["event_id"], "C0ACME001", event["message"]["ts"]
    )


def test_a_deletion_that_names_its_message_only_in_the_previous_one_is_kept() -> None:
    body = copy.deepcopy(load(EVENTS / "message_deleted.json")["body"])
    event = body["event"]
    ts = event.pop("deleted_ts")
    event["previous_message"] = {"type": "message", "user": "U0SAM0001", "text": "synthetic", "ts": ts}
    assert ingest_event(body, SETTINGS, RecentEvents()) == Revised(body["event_id"], "C0ACME001", ts)


@pytest.mark.parametrize(
    "field", ["team_id", "api_app_id", "channel", "event_id", "bot_id", "user_team", "team", "previous_team"]
)
def test_a_revision_from_elsewhere_stays_dropped(field: str) -> None:
    body = changed()
    body["event"]["message"]["subtype"] = "tombstone"
    if field == "channel":
        body["event"]["channel"] = "C0OTHER01"
    elif field == "user_team":
        body["event"]["message"]["user_team"] = "T0OTHER99"
    elif field == "team":
        body["event"]["team"] = "T0OTHER99"
    elif field == "previous_team":
        body["event"]["previous_message"]["user_team"] = "T0OTHER99"
    elif field == "bot_id":
        body["event"]["previous_message"]["bot_id"] = "B0OTHER01"
    elif field == "event_id":
        del body["event_id"]
    else:
        body[field] = "X0OTHER01"
    assert isinstance(ingest_event(body, SETTINGS, RecentEvents()), Drop)


@pytest.mark.parametrize("field", ["team_id", "api_app_id", "channel", "user_team"])
def test_the_lenient_reading_checks_where_a_revision_comes_from_by_itself(field: str) -> None:
    body = changed()
    assert revised_message(body, SETTINGS) == ("C0ACME001", body["event"]["message"]["ts"])
    if field == "channel":
        body["event"]["channel"] = "C0OTHER01"
    elif field == "user_team":
        body["event"]["message"]["user_team"] = "T0OTHER99"
    else:
        body[field] = "X0OTHER01"
    assert revised_message(body, SETTINGS) is None


@pytest.mark.parametrize("reason", [DropReason.WRONG_APP, DropReason.WRONG_WORKSPACE, DropReason.CHANNEL_NOT_ALLOWED])
def test_a_drop_for_where_a_payload_comes_from_is_never_read_again(reason: DropReason) -> None:
    assert reason not in ingest._MAY_HIDE_A_REVISION
    assert DropReason.BOT not in ingest._MAY_HIDE_A_REVISION


def test_an_edit_without_its_own_ts_is_found_by_the_previous_message() -> None:
    body = changed()
    ts = body["event"]["message"].pop("ts")
    assert body["event"]["previous_message"]["ts"] == ts
    assert ingest_event(body, SETTINGS, RecentEvents()) == Revised(body["event_id"], "C0ACME001", ts)


def test_a_reply_under_a_post_edited_earlier_is_not_another_edit() -> None:
    body = changed()  # Slack keeps the old "edited" block when a thread reply updates the parent
    event = body["event"]
    event["previous_message"] = copy.deepcopy(event["message"])
    event["message"]["reply_count"] = 1
    assert ingest_event(body, SETTINGS, RecentEvents()) == Drop(DropReason.UNCHANGED)


def test_a_second_edit_is_an_edit() -> None:
    body = changed()
    event = body["event"]
    event["previous_message"] = copy.deepcopy(event["message"])
    event["message"]["text"] = "4 Disc 40 on my truck"
    event["message"]["edited"]["ts"] = "1790000300.000000"
    assert isinstance(ingest_event(body, SETTINGS, RecentEvents()), Edited)


def test_a_changed_text_under_the_same_edit_stamp_is_still_an_edit() -> None:
    body = changed()
    event = body["event"]
    event["previous_message"] = copy.deepcopy(event["message"])
    event["message"]["text"] = "9 Disc 40 on my truck"
    assert isinstance(ingest_event(body, SETTINGS, RecentEvents()), Edited)


@pytest.mark.parametrize("key", ["bot_id", "bot_profile", "app_id"])
def test_any_bot_marker_on_any_part_is_a_bot(key: str) -> None:
    body = plain()
    body["event"][key] = {"id": "B0X"} if key == "bot_profile" else "B0X"
    assert ingest_event(body, SETTINGS, RecentEvents()) == Drop(DropReason.BOT)
    deletion = copy.deepcopy(load(EVENTS / "message_deleted.json")["body"])
    deletion["event"]["previous_message"][key] = "B0X"
    assert ingest_event(deletion, SETTINGS, RecentEvents()) == Drop(DropReason.BOT)


def test_enterprise_org_ids_are_refused_in_settings() -> None:
    with pytest.raises(ValueError, match="workspace_id"):
        SlackSettings(workspace_id="E0ACME001", app_id="A0ACME001")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda body: body["actions"][0].update(action_id="Apply"), DropReason.UNKNOWN_ACTION),
        (lambda body: body["actions"][0].update(action_id="apply "), DropReason.UNKNOWN_ACTION),
        (lambda body: body["actions"][0].update(action_id="apply-0"), DropReason.UNKNOWN_ACTION),
        (lambda body: body["actions"][0].update(action_id="answer-01"), DropReason.UNKNOWN_ACTION),
        (lambda body: body["actions"][0].update(action_id="answer-100"), DropReason.UNKNOWN_ACTION),
        (lambda body: body["actions"][0].update(action_id="answer-"), DropReason.UNKNOWN_ACTION),
        (lambda body: body["actions"][0].update(action_id="answer-1\n"), DropReason.UNKNOWN_ACTION),
        (lambda body: body["actions"][0].update(action_id="answer-\u0663"), DropReason.UNKNOWN_ACTION),
        (lambda body: body["actions"][0].update(type="static_select"), DropReason.UNKNOWN_ACTION),
        (lambda body: body["container"].update(type="view"), DropReason.MALFORMED),
        (lambda body: body["container"].update(channel_id="C0OTHER999"), DropReason.MALFORMED),
        (lambda body: body["container"].update(message_ts="1.0"), DropReason.MALFORMED),
        (lambda body: body.update(actions=[body["actions"][0], body["actions"][0]]), DropReason.MALFORMED),
    ],
)
def test_only_known_buttons_on_our_message_count(change: Any, reason: DropReason) -> None:
    body = copy.deepcopy(load(EVENTS / "block_actions_apply.json")["body"])
    change(body)
    assert ingest_action(body, SETTINGS) == Drop(reason)


@pytest.mark.parametrize("payload", [None, [], "s", 7])
def test_a_payload_that_is_not_an_object_is_dropped(payload: Any) -> None:
    assert ingest_event(payload, SETTINGS, RecentEvents()) == Drop(DropReason.MALFORMED)
    assert ingest_action(payload, SETTINGS) == Drop(DropReason.MALFORMED)


@pytest.mark.parametrize("where", ["message", "previous_message"])
@pytest.mark.parametrize("how", ["app_id", "bot_profile"])
def test_an_edit_of_the_connectors_own_reply_is_never_a_revision(where: str, how: str) -> None:
    """The connector edits its own replies to take buttons down; Slack sends that edit back as an event."""
    body = changed()
    assert revised_message(body, SETTINGS) is not None
    body["event"][where]["bot_id"] = "B0STOCKBOT"
    body["event"][where][how] = {"app_id": "A0ACME001"} if how == "bot_profile" else "A0ACME001"
    assert revised_message(body, SETTINGS) is None
    assert ingest_event(body, SETTINGS, RecentEvents()) == Drop(DropReason.BOT)


@pytest.mark.parametrize(
    "fields", [{"app_id": "A0OTHER999"}, {"bot_id": "B0STOCKBOT"}, {"bot_id": "B0OTHER01", "app_id": "A0OTHER999"}]
)
def test_an_edit_that_does_not_name_this_app_is_still_a_revision(fields: dict[str, str]) -> None:
    """A person's post edited through some other app, or an edit that names no app: still read as a revision."""
    body = changed()
    body["event"]["message"].update(fields)
    assert revised_message(body, SETTINGS) == ("C0ACME001", body["event"]["message"]["ts"])
