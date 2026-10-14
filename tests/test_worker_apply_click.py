"""The Apply button on a dry-run reply that made a plan (ADR 0019: a plan the policy does not approve waits for Apply):
the button, the worker's checks against the stored reply (`Worker._handle_apply`), `applier.approve_by_click` as
`run.click_approver` calls it, and what the clicker alone is told."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from stockthread.applier import GateNow, approve_by_click
from stockthread.config import Settings, parse_settings
from stockthread.core.matching import Catalog, LocationRecord, PartRecord
from stockthread.core.model import MovementType, PostKey, RequestKey
from stockthread.core.requests import RequestState
from stockthread.core.resolve import STATUS_OK, StockItem
from stockthread.gate import Judgement, Policy, Verdict
from stockthread.inventree import DEPLETING_MARKER_FOUND_FROM, ServerInfo, VersionCheck, check_version
from stockthread.run import click_approver
from stockthread.slack.replies import APPLY_ACTION, APPLY_BLOCK, CLICK_FEEDBACK, with_apply
from stockthread.store import DryRunOutcome, EventStatus, StateConflict, Store, WriteBlocked
from stockthread.worker import APPLY_FEEDBACK, Worker

S = RequestState
OWNER = "instance-a"
WORKSPACE, APP, CHANNEL = "T0ACME001", "A0ACME001", "C0ACME001"
SAM, LUIS = "U0SAM0001", "U0LUIS0005"
ROOT = "1790000000.000100"
POST = PostKey(WORKSPACE, CHANNEL, ROOT)
KEY = RequestKey(POST, 1, 1)
TRUCK = 11
"""Vehicles/Truck 1: Sam's mapped vehicle."""
TESTED = VersionCheck(ServerInfo("1.5.6", DEPLETING_MARKER_FOUND_FROM), True, "tested")
ON = GateNow(
    Policy(True, frozenset({MovementType.TAKE_TO_VEHICLE, MovementType.TAKE_TO_SITE}), frozenset({LUIS}), TESTED),
    {SAM: TRUCK},
)
BACK = "returned 2 Disc 40 from truck 1 to main stock"
"""A return: the policy never approves it, so it waits for Apply."""
CATALOG = Catalog(
    parts=(PartRecord(1, "DISC-40", "Disc 40"),),
    locations=(LocationRecord(10, "Main stock"), LocationRecord(TRUCK, "Vehicles/Truck 1")),
)
ENV = {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic", "INVENTREE_TOKEN": "inv-synthetic"}


def settings() -> Settings:
    data = {
        "slack": {"workspace_id": WORKSPACE, "app_id": APP, "channels": [CHANNEL], "approvers": [LUIS]},
        "inventree": {"url": "http://inventree.local:8000"},
        "writes": {"enabled": True},
    }
    return parse_settings(data, ENV)


class FakeSlack:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.notes: list[dict[str, Any]] = []

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posted.append(kwargs)
        return {"ok": True, "ts": f"1790009999.{len(self.posted):06d}"}

    def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:
        self.notes.append(kwargs)
        return {"ok": True}


class Clock:
    def __init__(self) -> None:
        self.now = datetime.fromtimestamp(1790000001, UTC)

    def __call__(self) -> datetime:
        return self.now

    def at(self, ts: str) -> None:
        self.now = datetime.fromtimestamp(float(Decimal(ts)), UTC)


class Clicking:
    """`approve_by_click` with `now`, as `run.click_approver` calls it; raises the errors in `fail` first."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.calls: list[tuple[PostKey, int, str]] = []
        self.fail: list[Exception] = []

    def __call__(self, post: PostKey, revision: int, user: str, at: Decimal) -> Judgement | None:
        self.calls.append((post, revision, user))
        if self.fail:
            raise self.fail.pop(0)
        return approve_by_click(self.store, OWNER, post, revision, user, ON, at=at, expiry_hours=24)


def stock(pairs: Sequence[tuple[int, int]]) -> dict[tuple[int, int], list[StockItem]]:
    """Five pieces of Disc 40 in stock item 57 at main stock, and five in stock item 58 on the truck."""
    items = {(1, 10): 57, (1, TRUCK): 58}
    return {
        pair: [StockItem(items[pair], Decimal(5), STATUS_OK, Decimal(0))] if pair in items else [] for pair in pairs
    }


def message(ts: str, text: str) -> dict[str, Any]:
    event = {"type": "message", "channel": CHANNEL, "user": SAM, "text": text, "ts": ts}
    return {"type": "event_callback", "team_id": WORKSPACE, "api_app_id": APP, "event_id": f"Ev{ts}", "event": event}


def edit(at: str, before: str, after: str) -> dict[str, Any]:
    edited = message(at, "")
    edited["event"]["subtype"] = "message_changed"
    base = {"type": "message", "user": SAM, "ts": ROOT}
    edited["event"]["message"] = {**base, "text": after, "edited": {"user": SAM, "ts": at}}
    edited["event"]["previous_message"] = {**base, "text": before}
    return edited


def enqueue(store: Store, payload: dict[str, Any]) -> None:
    assert store.enqueue_event(f"{payload['event_id']}:event", "event", json.dumps(payload))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store.open(tmp_path / "stockthread.db")
    opened.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    yield opened
    opened.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


def replied(store: Store, clock: Clock, clicking: Clicking | None, text: str = BACK) -> Worker:
    """The worker after the post's dry-run reply, which made a plan."""
    worker = Worker(
        settings(), store, OWNER, FakeSlack(), lambda: CATALOG, clock, stock=stock, approve_click=clicking,  # type: ignore[arg-type]
    )  # fmt: skip
    enqueue(store, message(ROOT, text))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    assert store.current_plan(POST) is not None, "the reply made no plan"
    return worker


def slack(worker: Worker) -> FakeSlack:
    assert isinstance(worker.slack, FakeSlack)
    return worker.slack


def apply_button(body: dict[str, Any]) -> dict[str, Any] | None:
    found = [block for block in body["blocks"] if block.get("block_id") == APPLY_BLOCK]
    if not found:
        return None
    [button] = found[0]["elements"]
    return dict(button)


def click(store: Store, worker: Worker, user: str = LUIS, *, reply: int = 0, value: str | None = None) -> None:
    """Enqueue the payload Slack sends when `user` clicks the Apply button of the reply posted `reply`-th."""
    posted = slack(worker).posted[reply]
    ts = f"1790009999.{reply + 1:06d}"
    button = apply_button(posted) or {"type": "button", "action_id": APPLY_ACTION, "value": "1"}
    if value is not None:
        button["value"] = value
    payload = {
        "type": "block_actions",
        "api_app_id": APP,
        "team": {"id": WORKSPACE},
        "user": {"id": user, "team_id": WORKSPACE},
        "channel": {"id": CHANNEL},
        "container": {"type": "message", "message_ts": ts, "channel_id": CHANNEL, "is_ephemeral": False},
        "message": {"type": "message", "ts": ts, "thread_ts": ROOT, "text": posted["text"]},
        "actions": [{**button, "action_ts": "1790000300.000100"}],
    }
    events = store._db.execute("SELECT count(*) FROM slack_events").fetchone()[0]
    assert store.enqueue_event(f"click:{events}", "interactive", json.dumps(payload))


def last_event(store: Store) -> tuple[str, str | None]:
    row = store._db.execute("SELECT status, outcome FROM slack_events ORDER BY id DESC LIMIT 1").fetchone()
    return row[0], row[1]


def told(worker: Worker) -> list[tuple[str, str]]:
    return [(note["user"], note["text"]) for note in slack(worker).notes]


def test_a_reply_whose_plan_waits_carries_an_apply_button_an_approver_clicks(store: Store, clock: Clock) -> None:
    clicking = Clicking(store)
    worker = replied(store, clock, clicking)
    [reply] = slack(worker).posted
    button = apply_button(reply)
    assert button is not None
    assert (button["action_id"], button["value"], button["style"]) == (APPLY_ACTION, "1", "primary")
    assert "confirm" in button  # a stray tap writes nothing
    assert store.request(KEY).state is S.PLANNED

    click(store, worker)
    worker.drain()
    assert last_event(store) == (EventStatus.DONE, "approved")
    assert store.request(KEY).state is S.APPROVED
    plan = store.plan(POST, 1)
    assert plan.approved_by == LUIS
    assert told(worker) == [(LUIS, APPLY_FEEDBACK["approved"])]

    click(store, worker)  # a second click, or Slack's retry
    worker.drain()
    assert last_event(store) == (EventStatus.DROPPED, "already_approved")
    assert told(worker)[-1] == (LUIS, APPLY_FEEDBACK["already_approved"])
    assert store.requests_to_work() == [KEY]


def test_with_writes_off_no_reply_has_an_apply_button_and_no_click_approves(store: Store, clock: Clock) -> None:
    worker = replied(store, clock, None)
    [reply] = slack(worker).posted
    assert apply_button(reply) is None
    click(store, worker)  # a forged click
    worker.drain()
    assert last_event(store) == (EventStatus.DROPPED, "writes_off")
    assert told(worker) == [(LUIS, APPLY_FEEDBACK["writes_off"])]
    assert store.request(KEY).state is S.PLANNED


def test_only_an_approvers_click_counts(store: Store, clock: Clock) -> None:
    clicking = Clicking(store)
    worker = replied(store, clock, clicking)
    click(store, worker, SAM)  # the poster is not an approver
    worker.drain()
    assert last_event(store) == (EventStatus.DROPPED, "not_authorized")  # by the ingest checks
    assert clicking.calls == []
    assert store.request(KEY).state is S.PLANNED
    judgement = approve_by_click(store, OWNER, POST, 1, SAM, ON, at=Decimal("1790000300"), expiry_hours=24)
    assert judgement is not None
    assert (judgement.verdict, judgement.why) == (Verdict.BLOCKED, "only an approver may apply a movement")


@pytest.mark.parametrize("value", ["2", "0", "01", "abc", "1.0", "\uff11"])  # the last: a full-width 1
def test_a_button_value_that_is_not_the_replys_revision_approves_nothing(
    store: Store, clock: Clock, value: str
) -> None:
    clicking = Clicking(store)
    worker = replied(store, clock, clicking)
    click(store, worker, value=value)
    worker.drain()
    assert last_event(store) == (EventStatus.DROPPED, "unknown_button")
    assert told(worker) == [(LUIS, CLICK_FEEDBACK["unknown_button"])]
    assert clicking.calls == []
    assert store.request(KEY).state is S.PLANNED


def test_a_click_on_a_message_no_dry_run_posted_approves_nothing(store: Store, clock: Clock) -> None:
    clicking = Clicking(store)
    worker = replied(store, clock, clicking)
    slack(worker).posted.append(dict(slack(worker).posted[0]))  # the same blocks, on another message
    click(store, worker, reply=1)
    worker.drain()
    assert last_event(store) == (EventStatus.DROPPED, "unknown_button")
    assert clicking.calls == []
    assert store.request(KEY).state is S.PLANNED


def test_a_click_on_a_reply_a_newer_one_replaced_approves_nothing(store: Store, clock: Clock) -> None:
    clicking = Clicking(store)
    worker = replied(store, clock, clicking)
    other = "returned 3 Disc 40 from truck 1 to main stock"
    enqueue(store, edit("1790000040.000100", BACK, other))
    clock.at("1790000100.000000")
    worker.step()
    assert len(slack(worker).posted) == 2  # the revised reply, with its own button
    assert apply_button(slack(worker).posted[1]) == {**(apply_button(slack(worker).posted[0]) or {}), "value": "2"}
    click(store, worker, reply=0)
    worker.drain()
    assert last_event(store) == (EventStatus.DROPPED, "other_message")
    assert told(worker) == [(LUIS, CLICK_FEEDBACK["other_message"])]
    assert clicking.calls == []
    assert store.request(RequestKey(POST, 2, 1)).state is S.PLANNED

    click(store, worker, reply=1)
    worker.drain()
    assert last_event(store) == (EventStatus.DONE, "approved")
    assert store.request(RequestKey(POST, 2, 1)).state is S.APPROVED


def test_a_click_on_the_reply_that_stands_approves_the_plan_an_edit_made_again(store: Store, clock: Clock) -> None:
    """An edit that reads the same posts nothing; the plan it makes again is the one the standing reply shows."""
    clicking = Clicking(store)
    worker = replied(store, clock, clicking)
    enqueue(store, edit("1790000040.000100", BACK, BACK + " "))
    clock.at("1790000100.000000")
    worker.step()
    assert len(slack(worker).posted) == 1
    plan = store.current_plan(POST)
    assert plan is not None
    assert plan.revision == 2
    click(store, worker)
    worker.drain()
    assert last_event(store) == (EventStatus.DONE, "approved")
    assert clicking.calls == [(POST, 2, LUIS)]
    assert store.request(RequestKey(POST, 2, 1)).state is S.APPROVED


def test_a_reply_a_retraction_replaced_has_no_plan_to_approve(store: Store, clock: Clock) -> None:
    clicking = Clicking(store)
    worker = replied(store, clock, clicking)
    store.stale_plan(OWNER, POST)  # the plan is taken back before a newer reply is recorded
    click(store, worker)
    worker.drain()
    assert last_event(store) == (EventStatus.DROPPED, "no_plan")
    assert told(worker) == [(LUIS, APPLY_FEEDBACK["no_plan"])]
    assert clicking.calls == []


def test_a_click_after_the_plan_expired_approves_nothing(store: Store, clock: Clock) -> None:
    clicking = Clicking(store)
    worker = replied(store, clock, clicking)
    clock.at("1790086401.000000")  # 24 hours and a second after the post
    click(store, worker)
    worker.drain()
    assert last_event(store) == (EventStatus.DROPPED, "apply_refused")
    assert told(worker) == [(LUIS, "Not applied: the plan expired.")]
    assert store.request(KEY).state is S.PLANNED


def test_a_plan_that_moved_on_in_between_tells_the_clicker_to_try_again(store: Store, clock: Clock) -> None:
    clicking = Clicking(store)
    worker = replied(store, clock, clicking)
    clicking.fail = [StateConflict("moved")]
    click(store, worker)
    worker.drain()
    assert last_event(store) == (EventStatus.DROPPED, "post_changed")
    assert told(worker) == [(LUIS, APPLY_FEEDBACK["post_changed"])]
    assert store.request(KEY).state is S.PLANNED


def test_a_click_that_fails_is_never_tried_again_later(store: Store, clock: Clock) -> None:
    """An approval long after the click would surprise the clicker: the click fails, and they are told to retry."""
    clicking = Clicking(store)
    worker = replied(store, clock, clicking)
    clicking.fail = [OSError("network down")]
    click(store, worker)
    worker.drain()
    status, _ = last_event(store)
    assert status == EventStatus.FAILED
    assert told(worker) == [(LUIS, APPLY_FEEDBACK["failed"])]
    clock.at("1790000600.000000")
    worker.step()
    assert clicking.calls == [(POST, 1, LUIS)]  # once
    assert store.request(KEY).state is S.PLANNED


def test_with_apply_refuses_a_revision_below_one() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        with_apply({"text": "Dry run", "blocks": []}, 0)


def test_the_run_loops_click_approver_reads_the_version_and_vehicles_for_each_click(store: Store, clock: Clock) -> None:
    replied(store, clock, None)
    reads: list[str] = []
    beats: list[int] = []

    def catalog() -> Catalog:
        reads.append("catalog")
        return Catalog(parts=(), locations=(), vehicles={SAM: TRUCK})

    def check(_: Any) -> VersionCheck:
        reads.append("version")
        return check_version(ServerInfo("9.9.9", 10**6))  # an upgrade nobody tested

    approve = click_approver(settings(), catalog, store, OWNER, beat=lambda: beats.append(1), check=check)
    judgement = approve(POST, 1, LUIS, Decimal("1790000300"))
    assert judgement is not None
    assert judgement.verdict is Verdict.BLOCKED
    assert "is not tested for writes" in judgement.why
    assert reads == ["catalog", "version"]
    assert beats == [1, 1]  # after each read
    assert store.request(KEY).state is S.PLANNED

    approve = click_approver(settings(), catalog, store, OWNER, beat=lambda: None, check=lambda _: TESTED)
    assert approve(POST, 1, LUIS, Decimal("1790086401")) == Judgement(Verdict.BLOCKED, "the plan expired")
    judgement = approve(POST, 1, LUIS, Decimal("1790000300"))
    assert judgement is not None
    assert judgement.allowed
    assert store.request(KEY).state is S.APPROVED
    assert store.plan(POST, 1).approved_by == LUIS


def test_a_reply_is_found_only_in_its_own_channel_and_only_when_one_dry_run_posted_it(store: Store) -> None:
    reply = "1790000100.000100"
    other = PostKey(WORKSPACE, CHANNEL, "1790000500.000100")  # past the sibling window
    elsewhere = PostKey(WORKSPACE, "C0ACME002", ROOT)
    for post in (POST, elsewhere):
        store.place_message(OWNER, post, user_id=SAM, thread_ts=None, window_seconds=5)
        store.finish_dry_run(OWNER, post, DryRunOutcome.REPLIED, reply, result={"movements": []})
    assert store.dry_run_with_reply(WORKSPACE, CHANNEL, reply) == (POST, 1)
    assert store.dry_run_with_reply(WORKSPACE, "C0ACME002", reply) == (elsewhere, 1)
    assert store.dry_run_with_reply(WORKSPACE, "C0ACME003", reply) is None
    assert store.dry_run_with_reply("T0OTHER01", CHANNEL, reply) is None
    store.place_message(OWNER, other, user_id=SAM, thread_ts=None, window_seconds=5)
    store.finish_dry_run(OWNER, other, DryRunOutcome.REPLIED, reply, result={"movements": []})
    assert store.dry_run_with_reply(WORKSPACE, CHANNEL, reply) is None  # never guessed between two


def undone(store: Store) -> None:
    """Revision 1 was written and then undone (its states set as the apply protocol leaves them)."""
    with store._db:
        store._db.execute("UPDATE requests SET state = 'undone'")


def test_a_click_after_an_undo_never_writes_the_movement_again(store: Store, clock: Clock) -> None:
    clicking = Clicking(store)
    worker = replied(store, clock, clicking)
    undone(store)
    click(store, worker)
    worker.drain()
    assert last_event(store) == (EventStatus.DROPPED, "undone")
    assert told(worker) == [(LUIS, APPLY_FEEDBACK["undone"])]

    enqueue(store, edit("1790000040.000100", BACK, BACK + " "))  # an edit that changes nothing makes the plan again
    clock.at("1790000100.000000")
    worker.step()
    plan = store.current_plan(POST)
    assert plan is not None
    assert plan.revision == 2
    click(store, worker)
    worker.drain()
    assert last_event(store) == (EventStatus.DROPPED, "apply_refused")
    assert told(worker)[-1] == (LUIS, "Not applied: this movement was undone; post it again to move the stock again.")
    assert store.request(RequestKey(POST, 2, 1)).state is S.PLANNED


def test_a_post_the_store_blocks_says_so_and_not_to_try_again(store: Store, clock: Clock) -> None:
    clicking = Clicking(store)
    worker = replied(store, clock, clicking)
    clicking.fail = [WriteBlocked("an earlier revision of this post left a stock change behind")]
    click(store, worker)
    worker.drain()
    assert last_event(store) == (EventStatus.DROPPED, "blocked")
    assert told(worker) == [(LUIS, APPLY_FEEDBACK["blocked"])]
