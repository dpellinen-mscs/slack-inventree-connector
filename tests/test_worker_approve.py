"""Approval by the policy in the run loop (ADR 0004, 0019): `Worker.approve_due` lets the policy judge each plan a
reply made in this process, once, and `run.policy_approver` judges it with the settings, the server's version read
again and the catalog's vehicles."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from stockthread.applier import GateNow, approve_by_policy
from stockthread.config import Settings, parse_settings
from stockthread.core.matching import Catalog, LocationRecord, PartRecord
from stockthread.core.model import MovementType, PostKey, RequestKey
from stockthread.core.requests import RequestState
from stockthread.core.resolve import STATUS_OK, StockItem
from stockthread.gate import Judgement, Policy, Verdict
from stockthread.inventree import DEPLETING_MARKER_FOUND_FROM, InvenTreeError, ServerInfo, VersionCheck, check_version
from stockthread.run import policy_approver
from stockthread.store import LockLost, Store
from stockthread.worker import APPROVE_FOR_SECONDS, WORK_RETRY_SECONDS, Worker

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
TAKE = "took 2 Disc 40 from main stock to truck 1"
BACK = "returned 2 Disc 40 from truck 1 to main stock"
CATALOG = Catalog(
    parts=(PartRecord(1, "DISC-40", "Disc 40"),),
    locations=(LocationRecord(10, "Main stock"), LocationRecord(TRUCK, "Vehicles/Truck 1")),
)
ENV = {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic", "INVENTREE_TOKEN": "inv-synthetic"}


def settings(*, writes: bool = False) -> Settings:
    data = {
        "slack": {"workspace_id": WORKSPACE, "app_id": APP, "channels": [CHANNEL], "approvers": [LUIS]},
        "inventree": {"url": "http://inventree.local:8000"},
        "writes": {"enabled": writes},
    }
    return parse_settings(data, ENV)


class FakeSlack:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posted.append(kwargs)
        return {"ok": True, "ts": f"1790009999.{len(self.posted):06d}"}


class Clock:
    def __init__(self) -> None:
        self.now = datetime.fromtimestamp(1790000001, UTC)

    def __call__(self) -> datetime:
        return self.now

    def at(self, ts: str) -> None:
        self.now = datetime.fromtimestamp(float(Decimal(ts)), UTC)


def stock(pairs: Sequence[tuple[int, int]]) -> dict[tuple[int, int], list[StockItem]]:
    """Five pieces of Disc 40 in stock item 57 at main stock, and five in stock item 58 on the truck."""
    items = {(1, 10): 57, (1, TRUCK): 58}
    return {
        pair: [StockItem(items[pair], Decimal(5), STATUS_OK, Decimal(0))] if pair in items else [] for pair in pairs
    }


def message(ts: str, text: str, *, user: str = SAM) -> dict[str, Any]:
    event = {"type": "message", "channel": CHANNEL, "user": user, "text": text, "ts": ts}
    return {"type": "event_callback", "team_id": WORKSPACE, "api_app_id": APP, "event_id": f"Ev{ts}", "event": event}


def enqueue(store: Store, payload: dict[str, Any]) -> None:
    assert store.enqueue_event(f"{payload['event_id']}:event", "event", json.dumps(payload))


class Approving:
    """`approve_by_policy` with `now`, as `run.policy_approver` calls it; raises the errors in `fail` first."""

    def __init__(self, store: Store, now: GateNow = ON) -> None:
        self.store = store
        self.now = now
        self.calls: list[PostKey] = []
        self.fail: list[Exception] = []
        self.judged: list[Judgement | None] = []

    def __call__(self, post: PostKey) -> Judgement | None:
        self.calls.append(post)
        if self.fail:
            raise self.fail.pop(0)
        judgement = approve_by_policy(self.store, OWNER, post, self.now)
        self.judged.append(judgement)
        return judgement


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store.open(tmp_path / "stockthread.db")
    opened.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    yield opened
    opened.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


def replied(store: Store, clock: Clock, approve: Approving | None, text: str = TAKE) -> Worker:
    """The worker after the post's dry-run reply, which made a plan, in the step that posted it."""
    worker = Worker(settings(), store, OWNER, FakeSlack(), lambda: CATALOG, clock, stock=stock, approve=approve)  # type: ignore[arg-type]
    enqueue(store, message(ROOT, text))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    plan = store.current_plan(POST)
    assert plan is not None, "the reply made no plan"
    return worker


def test_a_clear_take_out_is_approved_by_the_policy_once_right_after_its_reply(store: Store, clock: Clock) -> None:
    approve = Approving(store)
    worker = replied(store, clock, approve)
    assert approve.calls == [POST]  # in the step that posted the reply
    [judgement] = approve.judged
    assert judgement is not None
    assert (judgement.verdict, judgement.why) == (Verdict.ALLOWED, "approved by the policy")
    assert store.request(KEY).state is S.APPROVED
    assert store.requests_to_work() == [KEY]
    clock.at("1790000100.000000")
    worker.step()
    assert approve.calls == [POST]  # never judged again


def test_a_return_is_judged_once_and_waits_for_apply(store: Store, clock: Clock) -> None:
    approve = Approving(store)
    worker = replied(store, clock, approve, BACK)
    [judgement] = approve.judged
    assert judgement is not None
    assert judgement.verdict is Verdict.WAITS
    assert store.request(KEY).state is S.PLANNED
    approve.now = GateNow(
        Policy(True, frozenset(MovementType), frozenset({LUIS}), TESTED), {SAM: TRUCK}
    )  # the settings change: the plan is not judged again
    clock.at("1790000100.000000")
    worker.step()
    assert approve.calls == [POST]
    assert store.request(KEY).state is S.PLANNED


def test_with_writes_off_no_plan_is_judged(store: Store, clock: Clock) -> None:
    worker = replied(store, clock, None)
    assert worker.approve_due() == 0
    assert store.request(KEY).state is S.PLANNED
    approve = Approving(store)
    worker.approve = approve  # turned on later in the same process: the earlier plan still waits
    assert worker.approve_due() == 0
    assert approve.calls == []


def test_an_outage_puts_the_judgement_off_with_backoff(
    store: Store, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    approve = Approving(store)
    approve.fail = [InvenTreeError("down"), OSError("down")]
    with caplog.at_level(logging.WARNING, logger="stockthread.worker"):
        worker = replied(store, clock, approve)
    assert approve.calls == [POST]
    assert store.request(KEY).state is S.PLANNED
    assert "tried again later" in caplog.text
    made = Decimal("1790000030")
    clock.at(str(made + WORK_RETRY_SECONDS - 1))
    assert worker.approve_due() == 0
    assert len(approve.calls) == 1  # held back
    clock.at(str(made + WORK_RETRY_SECONDS))
    assert worker.approve_due() == 0
    assert len(approve.calls) == 2  # failed again: the wait doubles
    clock.at(str(made + 3 * WORK_RETRY_SECONDS - 1))
    assert worker.approve_due() == 0
    assert len(approve.calls) == 2
    clock.at(str(made + 3 * WORK_RETRY_SECONDS))
    assert worker.approve_due() == 1
    assert store.request(KEY).state is S.APPROVED


def test_a_plan_the_policy_could_not_judge_in_time_waits_for_apply(
    store: Store, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    approve = Approving(store)
    approve.fail = [InvenTreeError("down")]
    worker = replied(store, clock, approve)
    clock.at(str(Decimal("1790000030") + APPROVE_FOR_SECONDS))
    with caplog.at_level(logging.WARNING, logger="stockthread.worker"):
        assert worker.approve_due() == 0
    assert approve.calls == [POST]  # not judged once the reply is no longer fresh
    assert "could not judge its plan in time; it waits for Apply" in caplog.text
    assert worker.approve_due() == 0
    assert approve.calls == [POST]
    assert store.request(KEY).state is S.PLANNED


def test_a_judgement_that_fails_otherwise_is_an_error_and_the_plan_waits(
    store: Store, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    approve = Approving(store)
    approve.fail = [ValueError("a bug")]
    with caplog.at_level(logging.ERROR, logger="stockthread.worker"):
        worker = replied(store, clock, approve)
    assert "the policy's judgement failed (ValueError: a bug); the plan waits for Apply" in caplog.text
    clock.at("1790000100.000000")
    worker.step()
    assert approve.calls == [POST]
    assert store.request(KEY).state is S.PLANNED


def test_no_plan_is_judged_while_an_event_waits(store: Store, clock: Clock) -> None:
    approve = Approving(store)
    approve.fail = [InvenTreeError("down")]
    worker = replied(store, clock, approve)
    clock.at(str(Decimal("1790000030") + WORK_RETRY_SECONDS))
    enqueue(store, message("1790000031.000100", "lunch?", user=LUIS))  # may be an edit that takes the plan back
    assert worker.approve_due() == 0
    assert approve.calls == [POST]
    worker.step()  # drained first, then judged
    assert approve.calls == [POST, POST]
    assert store.request(KEY).state is S.APPROVED


def test_a_plan_made_again_by_an_edit_that_reads_the_same_waits_for_apply(store: Store, clock: Clock) -> None:
    approve = Approving(store)
    approve.fail = [InvenTreeError("down")]
    worker = replied(store, clock, approve)
    edited = message("1790000040.000100", "")
    edited["event"]["subtype"] = "message_changed"
    base = {"type": "message", "user": SAM, "ts": ROOT}
    edited["event"]["message"] = {**base, "text": TAKE + " ", "edited": {"user": SAM, "ts": "1790000040.000100"}}
    edited["event"]["previous_message"] = {**base, "text": TAKE}
    enqueue(store, edited)
    clock.at("1790000100.000000")
    worker.step()
    plan = store.current_plan(POST)
    assert plan is not None
    assert plan.revision == 2  # the edit staled the first plan before it was judged, and made it again
    assert approve.calls == [POST]  # the second dry run's plan is not queued
    last = approve_by_policy(store, OWNER, POST, ON)  # nor approved if it were judged
    assert last is not None
    assert (last.verdict, last.why) == (
        Verdict.WAITS,
        "plan revision 2: only a post's first plan applies with no click",
    )
    assert store.request(RequestKey(POST, 2, 1)).state is S.PLANNED


def edit(at: str, before: str, after: str) -> dict[str, Any]:
    edited = message(at, "")
    edited["event"]["subtype"] = "message_changed"
    base = {"type": "message", "user": SAM, "ts": ROOT}
    edited["event"]["message"] = {**base, "text": after, "edited": {"user": SAM, "ts": at}}
    edited["event"]["previous_message"] = {**base, "text": before}
    return edited


def test_a_first_plan_an_edit_made_waits_for_apply(store: Store, clock: Clock) -> None:
    """The post's first plan row, made by its second dry run: chatter edited into a take-out needs a click."""
    approve = Approving(store)
    worker = Worker(settings(), store, OWNER, FakeSlack(), lambda: CATALOG, clock, stock=stock, approve=approve)  # type: ignore[arg-type]
    enqueue(store, message(ROOT, "took some stuff"))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    assert store.current_plan(POST) is None
    enqueue(store, edit("1790000040.000100", "took some stuff", TAKE))
    clock.at("1790000100.000000")
    worker.step()
    plan = store.current_plan(POST)
    assert plan is not None
    assert plan.revision == 1  # the first plan row, from the post's second dry run
    assert approve.calls == []  # not queued: only the first dry run's plan is judged
    judgement = approve_by_policy(store, OWNER, POST, ON)  # nor approved if it were judged
    assert judgement is not None
    assert (judgement.verdict, judgement.why) == (
        Verdict.WAITS,
        "the post was edited or read again: only its first dry run applies with no click",
    )
    assert store.request(KEY).state is S.PLANNED


def test_a_second_dry_run_alone_keeps_the_policy_from_approving(
    store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A message joined to a post makes a second dry run with no edit noted (the joined text below is read as an
    edit, then the edit is hidden): the dry runs alone hold the first plan row back."""
    worker = Worker(settings(), store, OWNER, FakeSlack(), lambda: CATALOG, clock, stock=stock)  # type: ignore[arg-type]
    enqueue(store, message(ROOT, "took some stuff"))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    enqueue(store, edit("1790000040.000100", "took some stuff", TAKE))
    clock.at("1790000100.000000")
    worker.step()
    assert len(store.dry_run_history(POST)) == 2
    monkeypatch.setattr(store, "post_has_revision", lambda _: False)
    judgement = approve_by_policy(store, OWNER, POST, ON)
    assert judgement is not None
    assert judgement.verdict is Verdict.WAITS
    assert store.request(KEY).state is S.PLANNED


def test_a_lost_lock_stops_the_judgement(store: Store, clock: Clock) -> None:
    approve = Approving(store)
    approve.fail = [InvenTreeError("down")]
    worker = replied(store, clock, approve)
    approve.fail = [LockLost()]
    clock.at(str(Decimal("1790000030") + WORK_RETRY_SECONDS))
    with pytest.raises(LockLost):
        worker.approve_due()


def test_an_outage_puts_off_every_judgement_left_in_the_step(store: Store, clock: Clock) -> None:
    approve = Approving(store)
    approve.fail = [InvenTreeError("down")]
    worker = Worker(settings(), store, OWNER, FakeSlack(), lambda: CATALOG, clock, stock=stock, approve=approve)  # type: ignore[arg-type]
    other = PostKey(WORKSPACE, CHANNEL, "1790000000.000200")
    enqueue(store, message(ROOT, TAKE))
    enqueue(store, message(other.ts, TAKE, user=LUIS))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    assert store.current_plan(POST) is not None
    assert store.current_plan(other) is not None
    assert approve.calls == [POST]  # the first failure stops the other judgement in this step
    clock.at(str(Decimal("1790000030") + WORK_RETRY_SECONDS))
    assert worker.approve_due() == 1
    assert approve.calls == [POST, POST, other]


def test_a_reply_that_takes_the_plan_back_drops_its_judgement(store: Store, clock: Clock) -> None:
    approve = Approving(store)
    approve.fail = [InvenTreeError("down")]
    worker = replied(store, clock, approve)
    enqueue(store, edit("1790000040.000100", TAKE, "lunch?"))
    clock.at(str(Decimal("1790000030") + WORK_RETRY_SECONDS))
    worker.step()
    assert store.current_plan(POST) is None
    clock.at(str(Decimal("1790000030") + 4 * WORK_RETRY_SECONDS))
    worker.step()
    assert approve.calls == [POST]  # the edit's reply dropped the earlier plan's judgement


# --- `run.policy_approver` ---


def test_the_run_loops_approver_reads_the_version_and_vehicles_for_each_judgement(store: Store, clock: Clock) -> None:
    replied(store, clock, None)
    reads: list[str] = []
    beats: list[int] = []

    def catalog() -> Catalog:
        reads.append("catalog")
        return Catalog(parts=(), locations=(), vehicles={SAM: TRUCK})

    def check(_: Any) -> VersionCheck:
        reads.append("version")
        return check_version(ServerInfo("9.9.9", 10**6))  # an upgrade nobody tested

    on = settings(writes=True)
    approve = policy_approver(on, catalog, store, OWNER, beat=lambda: beats.append(1), check=check)
    judgement = approve(POST)
    assert judgement is not None
    assert judgement.verdict is Verdict.BLOCKED
    assert "is not tested for writes" in judgement.why
    assert reads == ["catalog", "version"]
    assert beats == [1, 1]  # after each read
    assert store.request(KEY).state is S.PLANNED

    approve = policy_approver(on, catalog, store, OWNER, beat=lambda: None, check=lambda _: TESTED)
    judgement = approve(POST)
    assert judgement is not None
    assert judgement.allowed
    assert store.request(KEY).state is S.APPROVED


UNSOURCED = "2 Disc 40 on truck 1"
DEFAULTED = replace(CATALOG, default_sources={MovementType.TAKE_TO_VEHICLE: 10})
"""Main stock is the default source of a take to a vehicle (`policy.default_source`, `catalog.assign_default_sources`)."""


def test_a_take_out_that_states_no_source_takes_the_default_and_applies_by_the_policy(
    store: Store, clock: Clock
) -> None:
    approve = Approving(store, GateNow(ON.policy, ON.vehicles, dict(DEFAULTED.default_sources)))
    slack = FakeSlack()
    worker = Worker(settings(), store, OWNER, slack, lambda: DEFAULTED, clock, stock=stock, approve=approve)  # type: ignore[arg-type]
    enqueue(store, message(ROOT, UNSOURCED))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    [reply] = slack.posted
    shown = json.dumps(reply["blocks"])
    assert "*Main stock* (not stated: the default source)" in shown
    assert "No source location was stated" not in shown
    [judgement] = approve.judged
    assert judgement is not None
    assert (judgement.verdict, judgement.why) == (Verdict.ALLOWED, "approved by the policy")
    [request] = judgement.requests
    assert [(item["pk"], item["quantity"]) for item in request.body["items"]] == [(57, "2")]  # at main stock
    assert store.request(KEY).state is S.APPROVED


def test_without_a_default_the_same_post_asks_for_its_source(store: Store, clock: Clock) -> None:
    approve = Approving(store)
    slack = FakeSlack()
    worker = Worker(settings(), store, OWNER, slack, lambda: CATALOG, clock, stock=stock, approve=approve)  # type: ignore[arg-type]
    enqueue(store, message(ROOT, UNSOURCED))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    [reply] = slack.posted
    assert "No source location was stated" in json.dumps(reply["blocks"])
    assert store.current_plan(POST) is None
    assert approve.calls == []
