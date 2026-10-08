"""The worker's step of the apply protocol (`applier`): approval by the policy and the next step of each request, on
views rebuilt from the dry-run record (ADR 0016), against the fake InvenTree of test_apply_request.py."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
import pytest
from fake_inventree import ITEM, FakeInvenTree
from views import DESTINATION, picked, view

from stockthread.applier import GateNow, approve_by_policy, judge_plan, plan_views, work_request
from stockthread.apply import Applied
from stockthread.core.marker import AttemptKind
from stockthread.core.model import Extraction, MovementType, Operation, PostKey, RequestKey
from stockthread.core.requests import Actor, IllegalTransition, RequestState
from stockthread.gate import Policy, Verdict
from stockthread.inventree import DEPLETING_MARKER_FOUND_FROM, InvenTreeError, ServerInfo, VersionCheck
from stockthread.plan_record import NotRebuilt, plan_of
from stockthread.slack.replies import MovementView
from stockthread.store import (
    Answered,
    DryRunOutcome,
    LockLost,
    NewPlan,
    NewQuestion,
    StateConflict,
    Store,
    WriteBlocked,
)
from stockthread.worker import _record
from stockthread.writer import Outcome

S = RequestState
OWNER = "worker-1"
SAM, LUIS = "U0SAM0001", "U0LUIS001"
POST = PostKey("T0ACME001", "C0ACME001", "1700000000.000100")
KEY = RequestKey(POST, 1, 1)
TAKE = view(stock=(picked((ITEM, "2")),))
"""Sam takes all 2 of item 57 from location 10 to his truck, location 11, every match exact: a clear take-out."""
BACK = view(MovementType.RETURN, stock=(picked((ITEM, "2")),))
"""Sam returns the same 2 items: a return always waits for an approver's Apply click."""
TESTED = VersionCheck(ServerInfo("1.5.6", DEPLETING_MARKER_FOUND_FROM), True, "tested")
ON = GateNow(
    Policy(True, frozenset({MovementType.TAKE_TO_VEHICLE, MovementType.TAKE_TO_SITE}), frozenset({LUIS}), TESTED),
    {SAM: DESTINATION},
)
OFF = replace(ON, policy=replace(ON.policy, writes_enabled=False))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def record(*views: MovementView, **extra: Any) -> dict[str, Any]:
    extraction = Extraction("rules", tuple(v.movement for v in views))
    stored = _record(extraction, views, late=False, poster=SAM, messages=(), max_quantity=500)
    return {**json.loads(json.dumps(stored)), **extra}


def reply(store: Store, result: dict[str, Any], revision: int = 1) -> None:
    """A reply showing `result`, with the plan it makes."""
    made = plan_of(result)
    assert made is not None
    if revision > 1:
        joined = PostKey(POST.workspace_id, POST.channel_id, f"17000000{revision:02d}.000100")
        store.place_message(OWNER, joined, user_id=SAM, thread_ts=POST.ts, window_seconds=5)
    store.finish_dry_run(
        OWNER,
        POST,
        DryRunOutcome.REPLIED,
        f"17000001{revision:02d}.000100",
        revision=revision,
        result=result,
        plan=NewPlan(*made),
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store.open(tmp_path / "stockthread.db")
    opened.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    opened.place_message(OWNER, POST, user_id=SAM, thread_ts=None, window_seconds=5)
    yield opened
    opened.close()


async def step(
    store: Store,
    fake: FakeInvenTree,
    key: RequestKey = KEY,
    now: GateNow = ON,
    *,
    settle: timedelta = timedelta(seconds=120),
) -> Applied:
    lock = fake.lock = anyio.Lock()
    return await work_request(
        key,
        store=store,
        reader=fake,
        writer=fake,
        lock=lock,
        lock_owner=OWNER,
        gate_now=lambda: now,
        settle=settle,
    )


@pytest.mark.anyio
async def test_a_clear_take_out_is_approved_by_the_policy_and_written_once(store: Store) -> None:
    reply(store, record(TAKE))
    judgement = approve_by_policy(store, OWNER, POST, ON)
    assert judgement is not None
    assert judgement.verdict is Verdict.ALLOWED
    assert store.request(KEY).state is S.APPROVED
    assert ("policy", "request_state", "planned", "approved") in [row[1:] for row in store.audit(POST)]
    assert store.requests_to_work() == [KEY]
    fake = FakeInvenTree()
    assert await step(store, fake) == Applied(S.VERIFIED, "read back: as planned")
    assert len(fake.sent) == 1
    assert fake.items[ITEM][1] == DESTINATION
    assert str(fake.sent[0].body["notes"]).endswith(SAM)  # the policy approved it: the notes name the poster
    assert store.requests_to_work() == []
    assert approve_by_policy(store, OWNER, POST, ON) is None  # nothing left to approve


def test_writes_off_approve_nothing(store: Store) -> None:
    reply(store, record(TAKE))
    judgement = approve_by_policy(store, OWNER, POST, OFF)
    assert judgement is not None
    assert (judgement.verdict, judgement.why) == (Verdict.BLOCKED, "stock writes are off")
    assert store.request(KEY).state is S.PLANNED
    assert store.requests_to_work() == []


def test_a_return_waits_for_apply(store: Store) -> None:
    reply(store, record(BACK))
    judgement = approve_by_policy(store, OWNER, POST, ON)
    assert judgement is not None
    assert judgement.verdict is Verdict.WAITS
    assert store.request(KEY).state is S.PLANNED


def test_an_answered_post_waits_for_apply(store: Store) -> None:
    reply(store, record(TAKE, answers=[{"question": 1, "answer": "yes"}]))
    judgement = approve_by_policy(store, OWNER, POST, ON)
    assert judgement is not None
    assert (judgement.verdict, judgement.why) == (Verdict.WAITS, "a person answered a question about it")
    assert store.request(KEY).state is S.PLANNED


def test_a_post_with_an_answered_question_waits_even_if_the_record_lists_no_answer(store: Store) -> None:
    yes = "a" * 22
    question = NewQuestion(
        {"ask": "confirm", "reason": "late_ingest", "movement": 0},
        False,
        [(yes, {"kind": "yes"}), ("b" * 22, {"kind": "no"})],
        poster=SAM,
        expires_from=POST.ts,
    )
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, "1700000101.000100", question=question)
    button = store.button(yes)
    assert button is not None
    assert store.answer_question(OWNER, button, SAM) is Answered.ANSWERED
    reply(store, record(TAKE), revision=2)
    judgement = approve_by_policy(store, OWNER, POST, ON)
    assert judgement is not None
    assert judgement.verdict is Verdict.WAITS
    assert store.request(RequestKey(POST, 1, 1)).state is S.PLANNED


def test_no_current_plan_approves_nothing(store: Store) -> None:
    assert approve_by_policy(store, OWNER, POST, ON) is None
    reply(store, record(TAKE))
    store.stale_plan(OWNER, POST)
    assert approve_by_policy(store, OWNER, POST, ON) is None
    assert store.request(KEY).state is S.CANCELLED


@pytest.mark.anyio
async def test_writes_turned_off_after_approval_write_nothing(store: Store) -> None:
    reply(store, record(TAKE))
    approve_by_policy(store, OWNER, POST, ON)
    fake = FakeInvenTree()
    fake.fail_reads = True  # none may happen
    assert await step(store, fake, now=OFF) == Applied(S.APPROVED, "not written: stock writes are off")
    assert fake.sent == []
    assert store.markers(KEY) == []


@pytest.mark.anyio
async def test_writes_turned_off_while_the_write_waits_for_the_lock_write_nothing(store: Store) -> None:
    reply(store, record(TAKE))
    approve_by_policy(store, OWNER, POST, ON)
    fake = FakeInvenTree()
    lock = fake.lock = anyio.Lock()
    now = [ON]
    done: list[Applied] = []

    async def write() -> None:
        done.append(
            await work_request(
                KEY,
                store=store,
                reader=fake,
                writer=fake,
                lock=lock,
                lock_owner=OWNER,
                gate_now=lambda: now[0],
                settle=timedelta(seconds=120),
            )
        )

    async with anyio.create_task_group() as tasks, lock:  # another write is in flight
        tasks.start_soon(write)
        await anyio.wait_all_tasks_blocked()
        now[0] = OFF
    assert done == [Applied(S.APPROVED, "not written: stock writes are off")]
    assert fake.sent == []


@pytest.mark.anyio
async def test_a_plan_approved_by_a_click_is_written_with_the_approver_named(store: Store) -> None:
    reply(store, record(BACK))
    store.approve_plan(OWNER, POST, 1, actor=Actor.HUMAN, approver=LUIS)
    assert store.plan(POST, 1).approved_by == LUIS
    fake = FakeInvenTree()
    assert (await step(store, fake)).state is S.VERIFIED
    assert str(fake.sent[0].body["notes"]).endswith(LUIS)


@pytest.mark.anyio
async def test_a_click_counts_only_while_its_clicker_is_an_approver(store: Store) -> None:
    reply(store, record(BACK))
    store.approve_plan(OWNER, POST, 1, actor=Actor.HUMAN, approver=LUIS)
    fake = FakeInvenTree()
    removed = replace(ON, policy=replace(ON.policy, approvers=frozenset()))
    waits = await step(store, fake, now=removed)
    assert waits == Applied(S.APPROVED, "not written: movement 1: return waits for Apply")
    assert fake.sent == []


@pytest.mark.anyio
async def test_a_request_approved_with_no_approver_stored_waits_like_the_policy(store: Store) -> None:
    reply(store, record(BACK))
    store.transition(KEY, expected=S.PLANNED, target=S.APPROVED, actor=Actor.HUMAN)  # one request, no clicker
    assert store.plan(POST, 1).approved_by is None
    fake = FakeInvenTree()
    assert (await step(store, fake)).why == "not written: movement 1: return waits for Apply"
    assert fake.sent == []


@pytest.mark.anyio
async def test_a_request_sent_back_to_review_loses_its_plans_clicker(store: Store) -> None:
    reply(store, record(BACK))
    store.approve_plan(OWNER, POST, 1, actor=Actor.HUMAN, approver=LUIS)
    fake = FakeInvenTree(held="1")  # the stock changed since the plan
    assert (await step(store, fake)).state is S.PLANNED
    assert store.plan(POST, 1).approved_by is None
    store.transition(KEY, expected=S.PLANNED, target=S.APPROVED, actor=Actor.HUMAN)
    fake.items[ITEM][2] = Decimal(2)
    assert (await step(store, fake)).why == "not written: movement 1: return waits for Apply"
    assert fake.sent == []


def test_a_persons_approval_names_the_approver_and_only_a_persons_does(store: Store) -> None:
    reply(store, record(TAKE))
    with pytest.raises(ValueError, match="names the approver"):
        store.approve_plan(OWNER, POST, 1, actor=Actor.HUMAN)
    with pytest.raises(ValueError, match="names the approver"):
        store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY, approver=LUIS)
    assert store.request(KEY).state is S.PLANNED
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    assert store.plan(POST, 1).approved_by is None


@pytest.mark.anyio
async def test_a_plan_without_its_record_is_never_written(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, "1700000101.000100", result=record(TAKE))
    store.create_plan(POST, "0" * 64, (Operation.TRANSFER,))  # made from no record the post holds
    with pytest.raises(NotRebuilt, match="no dry-run record"):
        approve_by_policy(store, OWNER, POST, ON)
    assert store.request(KEY).state is S.PLANNED
    store.approve_plan(OWNER, POST, 1, actor=Actor.HUMAN, approver=LUIS)
    fake = FakeInvenTree()
    with pytest.raises(NotRebuilt, match="no dry-run record"):
        await step(store, fake)
    assert fake.sent == []
    assert store.request(KEY).state is S.APPROVED


def test_a_plan_with_more_requests_than_its_record_has_movements_is_refused(store: Store) -> None:
    result = record(TAKE)
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, "1700000101.000100", result=result)
    made = plan_of(result)
    assert made is not None
    store.create_plan(POST, made[0], (Operation.TRANSFER, Operation.TRANSFER))
    plan = store.current_plan(POST)
    assert plan is not None
    with pytest.raises(NotRebuilt, match="one request per movement"):
        plan_views(store, plan)


def test_a_stale_plan_is_blocked_whatever_the_gate_says(store: Store) -> None:
    reply(store, record(TAKE))
    plan = store.current_plan(POST)
    assert plan is not None
    rebuilt = plan_views(store, plan)
    assert judge_plan(rebuilt, ON).allowed
    stale = replace(rebuilt, plan=replace(plan, current=False))
    assert judge_plan(stale, ON, clicked_by=LUIS).why == "plan revision 1 is no longer current"


@pytest.mark.anyio
async def test_an_approved_request_whose_plan_went_stale_is_not_worked(store: Store) -> None:
    reply(store, record(TAKE))
    approve_by_policy(store, OWNER, POST, ON)
    reply(store, record(TAKE), revision=2)  # a message joined, and the post was read again
    assert store.request(KEY).state is S.CANCELLED
    assert store.requests_to_work() == []
    with pytest.raises(ValueError, match="no step of the apply protocol is due"):
        await step(store, FakeInvenTree())


@pytest.mark.anyio
async def test_a_read_back_left_undone_is_finished_by_the_next_step(store: Store) -> None:
    reply(store, record(TAKE))
    approve_by_policy(store, OWNER, POST, ON)
    fake = FakeInvenTree()
    fake.fail_reads_after_write = True
    with pytest.raises(InvenTreeError):
        await step(store, fake)
    assert store.requests_to_work() == [KEY]
    assert store.request(KEY).state is S.APPLIED
    fake.fail_reads_after_write = False
    assert await step(store, fake) == Applied(S.VERIFIED, "read back: as planned")
    assert len(fake.sent) == 1


@pytest.mark.anyio
async def test_an_unknown_outcome_is_recovered_by_the_next_step_once_settled(store: Store) -> None:
    reply(store, record(TAKE))
    approve_by_policy(store, OWNER, POST, ON)
    fake = FakeInvenTree()
    fake.answer, fake.lands = Outcome.UNKNOWN, True  # it ran, but the client timed out
    assert (await step(store, fake)).state is S.UNKNOWN
    assert store.requests_to_work() == [KEY]
    settling = await step(store, fake)
    assert settling.state is S.UNKNOWN
    recovered = await step(store, fake, settle=timedelta(0))
    assert recovered == Applied(S.VERIFIED, "marker found; read back: as planned")
    assert len(fake.sent) == 1


def test_a_plan_is_approved_whole_or_not_at_all(store: Store) -> None:
    two = view(lines=(("Disc 40", "2"), ("U7 Lite", "1")), stock=(picked((ITEM, "2")), picked((58, "1"))))
    reply(store, record(two, TAKE))
    second = RequestKey(POST, 1, 2)
    store.transition(second, expected=S.PLANNED, target=S.APPROVED, actor=Actor.HUMAN)
    with pytest.raises(StateConflict, match="not planned"):
        store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    assert [row.state for row in store.plan(POST, 1).requests] == [S.PLANNED, S.APPROVED]


def test_approval_is_refused_whole_after_an_earlier_revision_wrote(store: Store) -> None:
    reply(store, record(TAKE))
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    marker = store.next_marker(KEY, AttemptKind.APPLY)
    store.begin_attempt(
        KEY,
        marker.kind,
        expected=S.APPROVED,
        actor=Actor.APPLIER,
        snapshot={},
        request_body={"notes": str(marker)},
        lock_owner=OWNER,
    )
    store.transition(KEY, expected=S.APPLYING, target=S.APPLIED, actor=Actor.APPLIER)
    reply(store, record(TAKE, TAKE), revision=2)
    with pytest.raises(WriteBlocked, match="earlier revision"):
        store.approve_plan(OWNER, POST, 2, actor=Actor.POLICY)
    assert [row.state for row in store.plan(POST, 2).requests] == [S.PLANNED, S.PLANNED]


def test_only_the_lock_holder_approves(store: Store) -> None:
    reply(store, record(TAKE))
    with pytest.raises(LockLost):
        store.approve_plan("worker-2", POST, 1, actor=Actor.POLICY)
    assert store.request(KEY).state is S.PLANNED


def test_only_a_person_or_the_policy_approves(store: Store) -> None:
    reply(store, record(TAKE))
    with pytest.raises(IllegalTransition, match="made by"):
        store.approve_plan(OWNER, POST, 1, actor=Actor.APPLIER)
    assert store.request(KEY).state is S.PLANNED


def write(store: Store, key: RequestKey, outcome: RequestState) -> None:
    """Send request `key`, already approved, to `outcome` the way the apply protocol does, with no InvenTree."""
    marker = store.next_marker(key, AttemptKind.APPLY)
    store.begin_attempt(
        key,
        marker.kind,
        expected=S.APPROVED,
        actor=Actor.APPLIER,
        snapshot={},
        request_body={"notes": str(marker)},
        lock_owner=OWNER,
    )
    if outcome is S.FAILED:
        store.transition(key, expected=S.APPLYING, target=S.FAILED, actor=Actor.APPLIER)
        return
    store.transition(key, expected=S.APPLYING, target=S.APPLIED, actor=Actor.APPLIER)
    if outcome is not S.APPLIED:
        store.transition(key, expected=S.APPLIED, target=outcome, actor=Actor.APPLIER)


TWO = view(lines=(("Disc 40", "2"), ("U7 Lite", "1")), stock=(picked((ITEM, "2")), picked((58, "1"))))
SECOND = RequestKey(POST, 1, 2)


def states(store: Store, revision: int = 1) -> list[RequestState]:
    return [row.state for row in store.plan(POST, revision).requests]


@pytest.mark.anyio
async def test_a_plan_is_due_its_receipt_once_its_write_is_read_back_and_only_once(store: Store) -> None:
    reply(store, record(TAKE))
    assert store.receipts_due() == []
    approve_by_policy(store, OWNER, POST, ON)
    assert store.receipts_due() == []
    assert await step(store, FakeInvenTree()) == Applied(S.VERIFIED, "read back: as planned")
    assert store.receipts_due() == [(POST, 1)]
    assert store.plan(POST, 1).receipt_ts is None
    store.receipt_posted(OWNER, POST, 1, "1700000200.000100", [S.VERIFIED])
    assert store.plan(POST, 1).receipt_ts == "1700000200.000100"
    assert store.request(KEY).receipt_state is S.VERIFIED
    assert store.receipts_due() == []
    assert ("applier", "receipt_posted", None, None) in [row[1:] for row in store.audit(POST)]
    with pytest.raises(StateConflict, match="not due"):
        store.receipt_posted(OWNER, POST, 1, "1700000201.000100", [S.VERIFIED])
    assert store.plan(POST, 1).receipt_ts == "1700000200.000100"


@pytest.mark.parametrize("outcome", [S.VERIFIED, S.VERIFY_FAILED, S.FAILED])
def test_every_concluded_write_makes_its_plan_due(store: Store, outcome: RequestState) -> None:
    reply(store, record(TAKE))
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    write(store, KEY, outcome)
    assert store.receipts_due() == [(POST, 1)]


def test_a_check_that_found_a_problem_before_anything_was_sent_makes_its_plan_due(store: Store) -> None:
    reply(store, record(TAKE))
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    store.transition(KEY, expected=S.APPROVED, target=S.VERIFY_FAILED, actor=Actor.APPLIER)
    assert store.receipts_due() == [(POST, 1)]


@pytest.mark.parametrize("outcome", [S.APPROVED, S.APPLYING, S.APPLIED, S.UNKNOWN])
def test_a_plan_waits_for_its_receipt_while_any_write_is_due_or_in_flight(store: Store, outcome: RequestState) -> None:
    reply(store, record(TAKE, TWO))
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    write(store, KEY, S.VERIFIED)
    if outcome is not S.APPROVED:
        marker = store.next_marker(SECOND, AttemptKind.APPLY)
        store.begin_attempt(
            SECOND,
            marker.kind,
            expected=S.APPROVED,
            actor=Actor.APPLIER,
            snapshot={},
            request_body={"notes": str(marker)},
            lock_owner=OWNER,
        )
        if outcome is not S.APPLYING:
            store.transition(SECOND, expected=S.APPLYING, target=outcome, actor=Actor.APPLIER)
    assert states(store) == [S.VERIFIED, outcome]
    assert store.receipts_due() == []
    with pytest.raises(StateConflict, match="not due"):
        store.receipt_posted(OWNER, POST, 1, "1700000200.000100", [S.VERIFIED, outcome])
    assert store.plan(POST, 1).receipt_ts is None


def test_a_write_whose_sibling_went_back_to_review_is_reported_at_once(store: Store) -> None:
    reply(store, record(TAKE, TWO))
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    write(store, KEY, S.VERIFIED)
    store.transition(SECOND, expected=S.APPROVED, target=S.PLANNED, actor=Actor.APPLIER)  # stock changed
    assert store.receipts_due() == [(POST, 1)]
    store.receipt_posted(OWNER, POST, 1, "1700000200.000100", [S.VERIFIED, S.PLANNED])
    assert store.request(SECOND).receipt_state is None
    assert store.receipts_due() == []


@pytest.mark.parametrize("outcome", [S.VERIFIED, S.VERIFY_FAILED, S.FAILED])
def test_a_write_tried_again_after_its_receipt_is_reported_again(store: Store, outcome: RequestState) -> None:
    reply(store, record(TAKE))
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    write(store, KEY, S.FAILED)
    store.receipt_posted(OWNER, POST, 1, "1700000200.000100", [S.FAILED])
    store.transition(KEY, expected=S.FAILED, target=S.APPROVED, actor=Actor.HUMAN)
    assert store.request(KEY).receipt_state is None  # the try the receipt reported is over
    assert store.receipts_due() == []  # the new try is due: the receipt waits for it
    write(store, KEY, outcome)  # refused again included: the same state is a new outcome
    assert store.receipts_due() == [(POST, 1)]
    store.receipt_posted(OWNER, POST, 1, "1700000300.000100", [outcome])
    assert store.plan(POST, 1).receipt_ts == "1700000300.000100"
    assert store.receipts_due() == []


def test_a_receipt_of_states_that_moved_on_is_not_recorded(store: Store) -> None:
    reply(store, record(TAKE))
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    write(store, KEY, S.VERIFY_FAILED)
    with pytest.raises(StateConflict, match="no longer in the states"):
        store.receipt_posted(OWNER, POST, 1, "1700000200.000100", [S.FAILED])
    with pytest.raises(StateConflict, match="no longer in the states"):
        store.receipt_posted(OWNER, POST, 1, "1700000200.000100", [S.VERIFY_FAILED, S.VERIFY_FAILED])
    assert store.plan(POST, 1).receipt_ts is None
    assert store.receipts_due() == [(POST, 1)]


def test_a_plan_never_written_is_not_due_a_receipt(store: Store) -> None:
    reply(store, record(TAKE))
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    reply(store, record(TAKE, TAKE), revision=2)  # the approved request is cancelled with its stale plan
    assert store.request(KEY).state is S.CANCELLED
    assert store.receipts_due() == []


def test_only_the_lock_holder_records_a_receipt(store: Store) -> None:
    reply(store, record(TAKE))
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    write(store, KEY, S.VERIFIED)
    with pytest.raises(LockLost):
        store.receipt_posted("worker-2", POST, 1, "1700000200.000100", [S.VERIFIED])
    with pytest.raises(ValueError, match="not empty"):
        store.receipt_posted(OWNER, POST, 1, "", [S.VERIFIED])
    with pytest.raises(KeyError):
        store.receipt_posted(OWNER, POST, 2, "1700000200.000100", [S.VERIFIED])
    assert store.receipts_due() == [(POST, 1)]


def test_writes_concluded_before_receipts_existed_are_not_reported(store: Store, tmp_path: Path) -> None:
    reply(store, record(TAKE))
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    write(store, KEY, S.VERIFIED)
    store.close()
    db = sqlite3.connect(tmp_path / "stockthread.db", isolation_level=None)  # back to schema 18, as 0018 left it
    db.execute("ALTER TABLE plans DROP COLUMN receipt_ts")
    db.execute("ALTER TABLE requests DROP COLUMN receipt_state")
    db.execute("PRAGMA user_version = 18")
    db.close()
    reopened = Store.open(tmp_path / "stockthread.db")
    assert reopened.schema_version == 20
    assert reopened.request(KEY).receipt_state is S.VERIFIED
    assert reopened.receipts_due() == []
    reopened.close()


def undo(store: Store, key: RequestKey, expected: RequestState, outcome: RequestState | None) -> None:
    """Start an undo of request `key` from `expected` the way the undo protocol does, with no InvenTree, and conclude
    it in `outcome` (None: leave it `undoing`)."""
    marker = store.next_marker(key, AttemptKind.UNDO)
    store.begin_attempt(
        key,
        marker.kind,
        expected=expected,
        actor=Actor.HUMAN,
        snapshot={},
        request_body={"notes": str(marker)},
        lock_owner=OWNER,
    )
    if outcome is not None:
        store.transition(key, expected=S.UNDOING, target=outcome, actor=Actor.APPLIER)


@pytest.mark.parametrize("outcome", [S.UNDONE, S.UNDO_FAILED, S.VERIFY_FAILED])
@pytest.mark.parametrize("applied", [S.VERIFIED, S.VERIFY_FAILED])
def test_an_undo_is_reported_by_a_new_receipt_once_it_concluded(
    store: Store, applied: RequestState, outcome: RequestState
) -> None:
    reply(store, record(TAKE))
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    write(store, KEY, applied)
    store.receipt_posted(OWNER, POST, 1, "1700000200.000100", [applied])
    undo(store, KEY, applied, None)
    assert store.request(KEY).receipt_state is None  # the undo is a new try: its outcome is reported, whatever it is
    assert store.receipts_due() == []  # not while it is in flight
    store.transition(KEY, expected=S.UNDOING, target=outcome, actor=Actor.APPLIER)
    assert store.receipts_due() == [(POST, 1)]  # verify_failed after verify_failed included
    store.receipt_posted(OWNER, POST, 1, "1700000300.000100", [outcome])
    assert store.plan(POST, 1).receipt_ts == "1700000300.000100"  # the old receipt's Undo button is out of date
    assert store.request(KEY).receipt_state is outcome
    assert store.receipts_due() == []


def test_an_undo_tried_again_is_reported_again(store: Store) -> None:
    reply(store, record(TAKE))
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    write(store, KEY, S.VERIFIED)
    store.receipt_posted(OWNER, POST, 1, "1700000200.000100", [S.VERIFIED])
    undo(store, KEY, S.VERIFIED, S.UNDO_FAILED)
    store.receipt_posted(OWNER, POST, 1, "1700000300.000100", [S.UNDO_FAILED])
    undo(store, KEY, S.UNDO_FAILED, S.UNDO_FAILED)  # refused again: the same state is a new outcome
    assert store.receipts_due() == [(POST, 1)]
    store.receipt_posted(OWNER, POST, 1, "1700000400.000100", [S.UNDO_FAILED])
    assert store.receipts_due() == []


def test_an_undo_of_one_movement_reports_the_whole_plan(store: Store) -> None:
    reply(store, record(TAKE, TWO))
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    write(store, KEY, S.VERIFIED)
    write(store, SECOND, S.VERIFIED)
    store.receipt_posted(OWNER, POST, 1, "1700000200.000100", [S.VERIFIED, S.VERIFIED])
    undo(store, SECOND, S.VERIFIED, S.UNDONE)
    assert store.receipts_due() == [(POST, 1)]
    with pytest.raises(StateConflict, match="no longer in the states"):
        store.receipt_posted(OWNER, POST, 1, "1700000300.000100", [S.VERIFIED, S.VERIFIED])
    store.receipt_posted(OWNER, POST, 1, "1700000300.000100", [S.VERIFIED, S.UNDONE])
    assert [store.request(key).receipt_state for key in (KEY, SECOND)] == [S.VERIFIED, S.UNDONE]
    assert store.receipts_due() == []


def downgrade_to_19(path: Path, reported: dict[int, str | None]) -> None:
    """Put the database back to schema 19, as 0019 left it: `receipt_state` takes the 3 apply outcomes only."""
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("ALTER TABLE requests DROP COLUMN receipt_state")
    db.execute(
        "ALTER TABLE requests ADD COLUMN receipt_state TEXT CHECK ("
        " receipt_state IS NULL OR receipt_state IN ('verified', 'verify_failed', 'failed'))"
    )
    for request_id, state in reported.items():
        db.execute("UPDATE requests SET receipt_state = ? WHERE id = ?", (state, request_id))
    db.execute("PRAGMA user_version = 19")
    db.close()


def test_undos_concluded_before_receipts_reported_them_are_not_reported(store: Store, tmp_path: Path) -> None:
    reply(store, record(TAKE, TWO))
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    write(store, KEY, S.VERIFIED)
    write(store, SECOND, S.VERIFIED)
    undo(store, SECOND, S.VERIFIED, S.UNDONE)
    first, second = store.request(KEY).id, store.request(SECOND).id
    store.close()
    downgrade_to_19(tmp_path / "stockthread.db", {first: "verified", second: "verified"})  # schema 19 kept these
    reopened = Store.open(tmp_path / "stockthread.db")
    assert reopened.schema_version == 20
    assert [reopened.request(key).receipt_state for key in (KEY, SECOND)] == [S.VERIFIED, S.UNDONE]
    assert reopened.receipts_due() == []
    reopened.close()


def test_a_reported_state_not_yet_reported_survives_the_migration_and_its_check(store: Store, tmp_path: Path) -> None:
    reply(store, record(TAKE, TWO))
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    write(store, KEY, S.VERIFIED)
    write(store, SECOND, S.FAILED)
    first, second = store.request(KEY).id, store.request(SECOND).id
    store.close()
    downgrade_to_19(tmp_path / "stockthread.db", {first: "verified", second: None})
    reopened = Store.open(tmp_path / "stockthread.db")
    assert [reopened.request(key).receipt_state for key in (KEY, SECOND)] == [S.VERIFIED, None]
    assert reopened.receipts_due() == [(POST, 1)]  # the unreported write is still due its receipt
    reopened.close()
    db = sqlite3.connect(tmp_path / "stockthread.db", isolation_level=None)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        db.execute("UPDATE requests SET receipt_state = 'planned' WHERE id = ?", (first,))
    db.execute("UPDATE requests SET receipt_state = 'undone' WHERE id = ?", (first,))
    db.close()


@pytest.mark.parametrize(("outcome", "found"), [(S.UNDONE, True), (S.VERIFY_FAILED, True), (S.UNDO_FAILED, False)])
def test_an_undo_a_crash_left_unknown_is_reported_once_recovery_concluded_it(
    store: Store, outcome: RequestState, found: bool
) -> None:
    reply(store, record(TAKE))
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    write(store, KEY, S.VERIFY_FAILED)
    store.receipt_posted(OWNER, POST, 1, "1700000200.000100", [S.VERIFY_FAILED])
    undo(store, KEY, S.VERIFY_FAILED, None)
    assert store.conclude_crashed_writes(OWNER) == [KEY]
    assert store.request(KEY).state is S.UNDO_UNKNOWN
    assert store.receipts_due() == []
    store.transition(KEY, expected=S.UNDO_UNKNOWN, target=outcome, actor=Actor.RECOVERY, marker_found=found)
    assert store.receipts_due() == [(POST, 1)]
    store.receipt_posted(OWNER, POST, 1, "1700000300.000100", [outcome])
    assert store.receipts_due() == []


def test_one_undo_of_a_plan_at_a_time_each_reported(store: Store) -> None:
    reply(store, record(TAKE, TWO))
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    write(store, KEY, S.VERIFIED)
    write(store, SECOND, S.VERIFIED)
    store.receipt_posted(OWNER, POST, 1, "1700000200.000100", [S.VERIFIED, S.VERIFIED])
    undo(store, KEY, S.VERIFIED, None)
    with pytest.raises(WriteBlocked, match="in flight"):  # so no receipt can report one undo while another runs
        undo(store, SECOND, S.VERIFIED, None)
    assert store.receipts_due() == []
    store.transition(KEY, expected=S.UNDOING, target=S.UNDONE, actor=Actor.APPLIER)
    store.receipt_posted(OWNER, POST, 1, "1700000300.000100", [S.UNDONE, S.VERIFIED])
    undo(store, SECOND, S.VERIFIED, S.UNDO_FAILED)
    assert store.receipts_due() == [(POST, 1)]
    store.receipt_posted(OWNER, POST, 1, "1700000400.000100", [S.UNDONE, S.UNDO_FAILED])
    assert store.receipts_due() == []


def test_an_undo_in_flight_at_the_migration_is_reported_once_it_concluded(store: Store, tmp_path: Path) -> None:
    reply(store, record(TAKE))
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    write(store, KEY, S.VERIFY_FAILED)
    undo(store, KEY, S.VERIFY_FAILED, None)
    request_id = store.request(KEY).id
    store.close()
    downgrade_to_19(tmp_path / "stockthread.db", {request_id: "verify_failed"})  # 19 kept it when the undo started
    reopened = Store.open(tmp_path / "stockthread.db")
    assert reopened.request(KEY).receipt_state is None
    reopened.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    reopened.transition(KEY, expected=S.UNDOING, target=S.VERIFY_FAILED, actor=Actor.APPLIER)
    assert reopened.receipts_due() == [(POST, 1)]
    reopened.close()
