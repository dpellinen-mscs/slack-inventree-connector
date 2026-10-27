# SPDX-License-Identifier: MIT
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
from stockthread.core.requests import Actor, RequestState
from stockthread.core.resolve import STATUS_OK, StockItem
from stockthread.gate import Judgement, Policy, Verdict
from stockthread.inventree import DEPLETING_MARKER_FOUND_FROM, ServerInfo, VersionCheck, check_version
from stockthread.run import click_approver
from stockthread.slack.replies import (
    APPLY_ACTION,
    APPLY_BLOCK,
    APPLY_LINE,
    CLICK_FEEDBACK,
    NO_APPLY_TEXT,
    apply_closed,
    with_apply,
)
from stockthread.store import DryRunOutcome, EventStatus, StateConflict, Store, WriteBlocked
from stockthread.worker import APPLY_FEEDBACK, TAKE_DOWN_BACKOFF_SECONDS, TAKE_DOWN_TRIES, Worker

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
        self.updates: list[dict[str, Any]] = []
        self.edited: dict[str, list[dict[str, Any]]] = {}
        """Blocks of a reply as it stands after an edit, by its ts."""
        self.read_failures = 0

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posted.append(kwargs)
        return {"ok": True, "ts": f"1790009999.{len(self.posted):06d}"}

    def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:
        self.notes.append(kwargs)
        return {"ok": True}

    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        """The thread's root, then the replies posted within the bounds, as Slack returns them (with its own fields)."""
        if self.read_failures:
            self.read_failures -= 1
            raise ConnectionError("synthetic outage")
        listed: list[dict[str, Any]] = [{"type": "message", "user": SAM, "text": "a post", "ts": kwargs["ts"]}]
        for n, reply in enumerate(self.posted, start=1):
            ts = f"1790009999.{n:06d}"
            if kwargs["oldest"] <= ts <= kwargs["latest"]:
                blocks = self.edited.get(ts) or [_as_slack_returns(block) for block in reply["blocks"]]
                listed.append({"type": "message", "bot_id": "B0STOCKBOT", "app_id": APP, "ts": ts,
                               "thread_ts": ROOT, "text": reply["text"], "blocks": blocks})  # fmt: skip
        return {"ok": True, "messages": listed[: kwargs["limit"]], "has_more": False}

    def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        self.edited[kwargs["ts"]] = list(kwargs["blocks"])
        return {"ok": True, "ts": kwargs["ts"]}


def _as_slack_returns(block: dict[str, Any]) -> dict[str, Any]:
    """Slack gives each block a block_id when it has none, and each text its `verbatim` flag."""
    returned = {"block_id": f"b{len(json.dumps(block)) % 997}", **block}
    if isinstance(block.get("text"), dict):
        returned["text"] = {**block["text"], "verbatim": False}
    return returned


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
        settings(), store, OWNER, FakeSlack(), lambda: CATALOG, clock, stock=stock, approve_click=clicking,
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


def test_a_click_on_a_reply_whose_newer_reply_lost_its_ts_approves_nothing(store: Store, clock: Clock) -> None:
    """A revised reply stored with no ts still made the current plan: the older reply's button must not approve it."""
    clicking = Clicking(store)
    worker = replied(store, clock, clicking)
    fake = slack(worker)
    post_message = fake.chat_postMessage

    def no_ts(**kwargs: Any) -> dict[str, Any]:
        post_message(**kwargs)
        return {"ok": True, "ts": None}

    fake.chat_postMessage = no_ts  # type: ignore[method-assign]
    enqueue(store, edit("1790000040.000100", BACK, "returned 3 Disc 40 from truck 1 to main stock"))
    clock.at("1790000100.000000")
    worker.step()
    assert len(fake.posted) == 2
    history = store.dry_run_history(POST)
    assert [(record.revision, record.reply_ts is None) for record in history] == [(1, False), (2, True)]
    plan = store.current_plan(POST)
    assert plan is not None
    assert plan.revision == 2
    fake.chat_postMessage = post_message  # type: ignore[method-assign]
    click(store, worker, reply=0)
    worker.drain()
    assert last_event(store) == (EventStatus.DROPPED, "other_message")
    assert told(worker) == [(LUIS, CLICK_FEEDBACK["other_message"])]
    assert clicking.calls == []
    assert store.request(RequestKey(POST, 2, 1)).state is S.PLANNED


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


ADAPTERS = 50
LONG_RETURN = (
    "returned "
    + ", ".join(f"1 Synthetic adapter {n:02d}" for n in range(1, ADAPTERS + 1))
    + " from truck 1 to main stock"
)


def test_a_reply_too_long_to_show_in_full_has_no_apply_button(store: Store, clock: Clock) -> None:
    """Review #139 finding 1: a return of 50 adapters does not fit the reply, which leaves out the last lines and both
    places. An approver approves only what they were shown (ADR 0016), so the reply says why it has no Apply button,
    and a click on it approves nothing."""
    parts = tuple(PartRecord(n, f"ADAPTER-{n:02d}", f"Synthetic adapter {n:02d}") for n in range(1, ADAPTERS + 1))
    catalog = Catalog(parts=parts, locations=CATALOG.locations)

    def on_the_truck(pairs: Sequence[tuple[int, int]]) -> dict[tuple[int, int], list[StockItem]]:
        return {
            (part, at): [StockItem(100 + part, Decimal(5), STATUS_OK, Decimal(0))] if at == TRUCK else []
            for part, at in pairs
        }

    clicking = Clicking(store)
    worker = Worker(
        settings(), store, OWNER, FakeSlack(), lambda: catalog, clock, stock=on_the_truck, approve_click=clicking,
    )  # fmt: skip
    enqueue(store, message(ROOT, LONG_RETURN))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    plan = store.current_plan(POST)
    assert plan is not None, "the reply made no plan"
    [reply] = slack(worker).posted
    shown = "\n".join(block["text"]["text"] for block in reply["blocks"] if block["type"] == "section")
    assert "Synthetic adapter 50" not in shown  # cut out of the reply
    assert apply_button(reply) is None
    assert shown.endswith(NO_APPLY_TEXT)
    click(store, worker)  # a forged click, as if the reply carried the button
    worker.drain()
    assert last_event(store) == (EventStatus.DROPPED, "not_shown")
    assert told(worker) == [(LUIS, APPLY_FEEDBACK["not_shown"])]
    assert clicking.calls == []
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


def test_a_click_approves_a_plan_its_posts_second_dry_run_made_after_an_answer(store: Store, clock: Clock) -> None:
    # Plan revisions are counted apart from dry runs: dry run 2 makes plan revision 1 here.
    clicking = Clicking(store)
    worker = Worker(
        settings(), store, OWNER, FakeSlack(), lambda: CATALOG, clock, stock=stock, approve_click=clicking,
    )  # fmt: skip
    enqueue(store, message(ROOT, "returned 2 disc from truck 1 to main stock"))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    assert store.current_plan(POST) is None  # "disc" is a whole-word match: asked first
    [asked] = slack(worker).posted
    [yes] = [e for block in asked["blocks"] if block["type"] == "actions" for e in block["elements"]
             if e["text"]["text"] == "Yes"]  # fmt: skip
    payload = {
        "type": "block_actions",
        "api_app_id": APP,
        "team": {"id": WORKSPACE},
        "user": {"id": SAM, "team_id": WORKSPACE},
        "channel": {"id": CHANNEL},
        "container": {"type": "message", "message_ts": "1790009999.000001", "channel_id": CHANNEL},
        "message": {"type": "message", "ts": "1790009999.000001", "thread_ts": ROOT, "text": asked["text"]},
        "actions": [{**yes, "action_ts": "1790000040.000100"}],
    }
    assert store.enqueue_event("click:yes", "interactive", json.dumps(payload))
    clock.at("1790000050.000000")
    worker.step()
    clock.at("1790000090.000000")
    worker.step()
    plan = store.current_plan(POST)
    assert plan is not None
    assert plan.revision == 1
    assert [record.revision for record in store.dry_run_history(POST)] == [1, 2]
    button = apply_button(slack(worker).posted[-1])
    assert button is not None
    assert (button["action_id"], button["value"]) == (APPLY_ACTION, "2")  # the dry run's revision, not the plan's
    click(store, worker, reply=len(slack(worker).posted) - 1)
    worker.step()
    assert last_event(store) == (EventStatus.DONE, "approved")
    assert clicking.calls == [(POST, 1, LUIS)]
    assert store.plan(POST, 1).approved_by == LUIS


PAST_THE_LOOK = "1790000033.000000"
"""Past TAKE_DOWN_SPACING_SECONDS after the reply's own step looked through the Apply buttons and found none due."""


def shown(store: Store) -> list[tuple[int, str, int]]:
    return store._db.execute("SELECT revision, buttons, buttons_tries FROM apply_buttons ORDER BY id").fetchall()


def closing(worker: Worker, reply: int = 0) -> str | None:
    """The line the reply posted `reply`-th ends with once edited, or None while it is not edited."""
    blocks = slack(worker).edited.get(f"1790009999.{reply + 1:06d}")
    if blocks is None:
        return None
    assert not [block for block in blocks if block.get("block_id") == APPLY_BLOCK]
    assert not [block for block in blocks if (block.get("text") or {}).get("text") == APPLY_LINE]
    last = blocks[-1]
    assert last["type"] == "context"
    return str(last["elements"][0]["text"])


def written(store: Store) -> None:
    """The plan's write began (its states set as the apply protocol leaves them)."""
    with store._db:
        store._db.execute("UPDATE requests SET state = 'verified'")


def policy_wrote(store: Store) -> None:
    """The policy approved the plan with no click, and its write began."""
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    written(store)


def test_the_apply_button_is_taken_down_once_the_write_a_click_approved_began(store: Store, clock: Clock) -> None:
    clicking = Clicking(store)
    worker = replied(store, clock, clicking)
    assert shown(store) == [(1, "shown", 0)]
    worker.step()
    assert slack(worker).updates == []  # the plan waits: the button stays
    click(store, worker)
    clock.at(PAST_THE_LOOK)
    worker.step()
    assert store.request(KEY).state is S.APPROVED
    assert slack(worker).updates == []  # approved, not yet written: the writer may send it back to review
    written(store)
    clock.at("1790000036.000000")
    worker.step()
    assert closing(worker) == f"This was approved by <@{LUIS}>. The receipt in this thread says what was written."
    [update] = slack(worker).updates
    kept = update["blocks"][:-1]
    assert kept == [_as_slack_returns(block) for block in slack(worker).posted[0]["blocks"][:-2]]  # the reply as it was
    assert shown(store) == [(1, "removed", 0)]
    assert "apply_removed" in [row[0] for row in store._db.execute("SELECT event FROM audit_log")]
    clock.at("1790000400.000000")
    worker.step()
    assert len(slack(worker).updates) == 1  # taken down once

    click(store, worker)  # a click Slack sent before the edit approves nothing more
    worker.drain()
    assert last_event(store) == (EventStatus.DROPPED, "already_approved")


def test_a_plan_the_policy_approved_takes_its_button_down_with_no_name(store: Store, clock: Clock) -> None:
    worker = replied(store, clock, Clicking(store))
    policy_wrote(store)
    written(store)
    clock.at(PAST_THE_LOOK)
    worker.step()
    assert closing(worker) == "This was approved with no click. The receipt in this thread says what was written."


def test_an_approved_plan_sent_back_to_review_keeps_its_button_for_a_click(store: Store, clock: Clock) -> None:
    clicking = Clicking(store)
    worker = replied(store, clock, clicking)
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    clock.at(PAST_THE_LOOK)
    worker.step()
    assert slack(worker).updates == []
    # Stock changed since the plan (`apply._back_to_review`): it waits for a click again.
    store.transition(KEY, expected=S.APPROVED, target=S.PLANNED, actor=Actor.APPLIER)
    clock.at("1790000036.000000")
    worker.step()
    assert slack(worker).updates == []
    click(store, worker)
    worker.drain()
    assert last_event(store) == (EventStatus.DONE, "approved")


@pytest.mark.parametrize("outcome", [DryRunOutcome.SILENT, DryRunOutcome.REPLIED])
def test_a_written_plan_says_approved_even_once_a_later_dry_run_ran(
    store: Store, clock: Clock, outcome: DryRunOutcome
) -> None:
    worker = replied(store, clock, Clicking(store))
    policy_wrote(store)
    written(store)
    later(store, outcome)
    clock.at(PAST_THE_LOOK)
    worker.step()
    assert closing(worker) == "This was approved with no click. The receipt in this thread says what was written."


def later(store: Store, outcome: DryRunOutcome) -> None:
    """A thread reply joins the post, and its revision 2 is recorded with `outcome` and no plan."""
    store.place_message(OWNER, PostKey(WORKSPACE, CHANNEL, "1790000020.000100"), user_id=SAM, thread_ts=ROOT,
                        window_seconds=5)  # fmt: skip
    posted = "1790000021.000100" if outcome is DryRunOutcome.REPLIED else None
    store.finish_dry_run(OWNER, POST, outcome, posted, revision=2)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (DryRunOutcome.SILENT, "This movement can no longer be applied from here."),
        (
            DryRunOutcome.REPLIED,
            "A newer reply in this thread replaced this one, so it can no longer be applied from here.",
        ),
    ],
)
def test_a_later_dry_run_closes_the_button_and_says_replaced_only_for_a_reply(
    store: Store, clock: Clock, outcome: DryRunOutcome, expected: str
) -> None:
    worker = replied(store, clock, Clicking(store))
    later(store, outcome)
    clock.at(PAST_THE_LOOK)
    worker.step()
    assert closing(worker) == expected


def test_a_reply_a_newer_one_replaced_loses_its_button_and_the_newer_keeps_its_own(store: Store, clock: Clock) -> None:
    worker = replied(store, clock, Clicking(store))
    enqueue(store, edit("1790000040.000100", BACK, "returned 3 Disc 40 from truck 1 to main stock"))
    clock.at("1790000100.000000")
    worker.step()
    clock.at("1790000103.000000")
    worker.step()
    assert closing(worker, 0) == (
        "A newer reply in this thread replaced this one, so it can no longer be applied from here."
    )
    assert closing(worker, 1) is None
    assert shown(store) == [(1, "removed", 0), (2, "shown", 0)]


def test_an_edit_that_reads_the_same_keeps_the_button_of_the_reply_that_stands(store: Store, clock: Clock) -> None:
    worker = replied(store, clock, Clicking(store))
    enqueue(store, edit("1790000040.000100", BACK, BACK + " "))
    clock.at("1790000100.000000")
    worker.step()
    clock.at("1790000103.000000")
    worker.step()
    assert len(slack(worker).posted) == 1
    assert slack(worker).updates == []
    assert shown(store) == [(1, "shown", 0)]


def edited_away_and_back(store: Store, worker: Worker, clock: Clock, first: str) -> int:
    """Plan revision 1's requests are left `first`; an edit then makes plan 2, and an edit back plan 3, whose movements,
    and so its digest, are plan 1's. Each edit posts a reply with its own button. Returns which post is plan 3's reply
    (a receipt of plan 1 may come first)."""
    with store._db:
        store._db.execute(
            "UPDATE requests SET state = ? WHERE plan_id = (SELECT id FROM plans WHERE revision = 1)", (first,)
        )
    other = "returned 3 Disc 40 from truck 1 to main stock"
    enqueue(store, edit("1790000040.000100", BACK, other))
    clock.at("1790000100.000000")
    worker.step()
    enqueue(store, edit("1790000140.000100", other, BACK))
    clock.at("1790000200.000000")
    worker.step()
    texts = [posted["text"] for posted in slack(worker).posted]
    assert texts[-1] == "Revised dry run: 1 movement, nothing changed."
    plan = store.current_plan(POST)
    assert plan is not None
    assert (plan.revision, plan.input_hash) == (3, store.plan(POST, 1).input_hash)
    clock.at("1790000203.000000")
    worker.step()
    clock.at("1790000206.000000")
    worker.step()
    return len(texts) - 1


def test_a_write_from_before_the_reply_does_not_close_the_button_of_the_plan_made_again(
    store: Store, clock: Clock
) -> None:
    """Plan 1's write was refused (nothing changed), so a click on the reply that makes the same plan again counts."""
    clicking = Clicking(store)
    worker = replied(store, clock, clicking)
    third = edited_away_and_back(store, worker, clock, "failed")
    assert third == 3  # after the "Not applied" receipt of plan 1
    assert [row[:2] for row in shown(store)] == [(1, "removed"), (2, "removed"), (3, "shown")]
    assert closing(worker, 0) == "This was approved with no click. The receipt in this thread says what was written."
    assert closing(worker, third) is None
    click(store, worker, reply=third)
    worker.drain()
    assert last_event(store) == (EventStatus.DONE, "approved")
    assert store.request(RequestKey(POST, 3, 1)).state is S.APPROVED


def test_a_write_from_before_the_reply_that_was_undone_closes_the_plan_made_again_with_no_approver(
    store: Store, clock: Clock
) -> None:
    worker = replied(store, clock, Clicking(store))
    third = edited_away_and_back(store, worker, clock, "undone")
    assert [row[:2] for row in shown(store)] == [(1, "removed"), (2, "removed"), (3, "removed")]
    assert closing(worker, third) == "This movement can no longer be applied from here."


def test_with_its_plan_taken_back_and_no_newer_reply_yet_the_button_waits(store: Store, clock: Clock) -> None:
    worker = replied(store, clock, Clicking(store))
    store.stale_plan(OWNER, POST)  # a revision is on its way, and may make the plan again
    clock.at("1790000100.000000")
    worker.step()
    assert slack(worker).updates == []
    assert shown(store) == [(1, "shown", 0)]


@pytest.mark.parametrize(("at", "expired"), [("1790086400.000100", False), ("1790086400.000101", True)])
def test_the_button_is_taken_down_when_its_plan_expires_as_a_click_would_be_refused(
    store: Store, clock: Clock, at: str, expired: bool
) -> None:
    worker = replied(store, clock, Clicking(store))
    clock.at(at)
    worker.step()
    expected = "This plan expired, so nothing from this post will be applied. To record it, post it again."
    assert closing(worker) == (expected if expired else None)


@pytest.mark.parametrize("state", ["rejected", "cancelled"])
def test_a_plan_closed_with_nothing_approved_says_it_can_no_longer_be_applied(
    store: Store, clock: Clock, state: str
) -> None:
    worker = replied(store, clock, Clicking(store))
    with store._db:
        store._db.execute("UPDATE requests SET state = ?", (state,))
    clock.at(PAST_THE_LOOK)
    worker.step()
    assert closing(worker) == "This movement can no longer be applied from here."


def test_a_button_that_cannot_be_taken_down_is_tried_with_backoff_then_left(store: Store, clock: Clock) -> None:
    worker = replied(store, clock, Clicking(store))
    policy_wrote(store)
    slack(worker).read_failures = TAKE_DOWN_TRIES
    clock.at(PAST_THE_LOOK)
    worker.step()
    assert shown(store) == [(1, "shown", 1)]
    worker.step()
    assert shown(store) == [(1, "shown", 1)]  # not before its backoff
    at = 1790000033
    for tries, wait in enumerate([TAKE_DOWN_BACKOFF_SECONDS, 2 * TAKE_DOWN_BACKOFF_SECONDS], start=2):
        at += wait
        clock.at(f"{at}.000000")
        worker.step()
        assert shown(store)[0][2] == tries
    assert shown(store) == [(1, "left", 3)]
    assert slack(worker).updates == []


def test_an_edit_that_went_through_unseen_counts_as_done_and_a_reply_with_no_button_is_left(
    store: Store, clock: Clock
) -> None:
    worker = replied(store, clock, Clicking(store))
    reply = slack(worker).posted[0]
    policy_wrote(store)
    done = apply_closed("approved")
    slack(worker).edited["1790009999.000001"] = [*reply["blocks"][:-2], done]
    clock.at(PAST_THE_LOOK)
    worker.step()
    assert shown(store) == [(1, "removed", 0)]
    assert slack(worker).updates == []


def test_a_reply_with_no_apply_button_to_take_out_is_left(store: Store, clock: Clock) -> None:
    worker = replied(store, clock, Clicking(store))
    reply = slack(worker).posted[0]
    policy_wrote(store)
    slack(worker).edited["1790009999.000001"] = reply["blocks"][:-2]  # someone else's edit, say
    clock.at(PAST_THE_LOOK)
    worker.step()
    assert shown(store) == [(1, "left", 0)]
    assert slack(worker).updates == []


def test_questions_and_apply_buttons_take_turns() -> None:
    worker = Worker.__new__(Worker)
    turns: list[str] = []
    worker._apply_turn = False
    item = type("Item", (), {"id": 2})()

    def due(now: Decimal) -> tuple[Any, str, None] | None:
        return (type("B", (), {"id": 1})(), "approved", None)

    def one(what: str, *args: Any) -> int:
        turns.append(what.split()[0])
        return 1

    class Quiet:
        def expire_questions(self, *args: Any) -> int:
            return 0

        def closed_with_buttons(self, limit: int) -> list[tuple[object, None]]:
            return [(item, None)]

        buttons_failed = buttons_taken_down = apply_failed = apply_taken_down = None

    worker.store = Quiet()  # type: ignore[assignment]
    worker.owner = OWNER
    worker.settings = settings()
    worker.clock = Clock()
    worker._quiet_until = worker._take_down_quiet_until = Decimal(0)
    worker.stopping = lambda: False
    worker._apply_due = due  # type: ignore[method-assign]
    worker._take_down_one = one  # type: ignore[method-assign, assignment]
    for _ in range(4):
        worker.take_down_buttons()
    assert turns == ["buttons", "Apply", "buttons", "Apply"]


def test_a_plan_made_again_after_an_undo_takes_its_button_down(store: Store, clock: Clock) -> None:
    """An edit that changes nothing makes the plan again after its undo; a click on it is refused, so the button goes.
    The plan of the reply was written (and undone, as its receipt says), so the line says it was approved."""
    worker = replied(store, clock, Clicking(store))
    undone(store)
    enqueue(store, edit("1790000040.000100", BACK, BACK + " "))
    clock.at("1790000100.000000")
    worker.step()
    plan = store.current_plan(POST)
    assert plan is not None
    assert (plan.revision, {row.state for row in plan.requests}) == (2, {S.PLANNED})
    assert closing(worker) == "This was approved with no click. The receipt in this thread says what was written."


def test_a_plan_whose_post_was_undone_before_takes_its_button_down(store: Store, clock: Clock) -> None:
    """A revised reply's own plan was never written, but an undo of the post's earlier plan closes it all the same."""
    worker = replied(store, clock, Clicking(store))
    undone(store)
    enqueue(store, edit("1790000040.000100", BACK, "returned 3 Disc 40 from truck 1 to main stock"))
    clock.at("1790000100.000000")
    worker.step()
    clock.at("1790000103.000000")
    worker.step()
    [revised] = [n for n, body in enumerate(slack(worker).posted) if (apply_button(body) or {}).get("value") == "2"]
    assert closing(worker, 0) == "This was approved with no click. The receipt in this thread says what was written."
    assert closing(worker, revised) == "This movement can no longer be applied from here."


def test_a_button_waits_while_a_reply_of_its_post_is_on_its_way(store: Store, clock: Clock) -> None:
    worker = replied(store, clock, Clicking(store))
    policy_wrote(store)
    worker._running[POST] = object()  # type: ignore[assignment]
    clock.at(PAST_THE_LOOK)
    worker.take_down_buttons()
    assert slack(worker).updates == []
    worker._running.clear()
    clock.at("1790000036.000000")
    worker.take_down_buttons()
    assert closing(worker) == "This was approved with no click. The receipt in this thread says what was written."
