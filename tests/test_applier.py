"""The worker's step of the apply protocol (`applier`): approval by the policy and the next step of each request, on
views rebuilt from the dry-run record (ADR 0016), against the fake InvenTree of test_apply_request.py."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
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
    clicked_by: str | None = None,
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
        clicked_by=clicked_by,
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
    back = view(MovementType.RETURN, stock=(picked((ITEM, "2")),))
    reply(store, record(back))
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
async def test_an_approval_by_click_writes_only_with_the_approver_named(store: Store) -> None:
    back = view(MovementType.RETURN, stock=(picked((ITEM, "2")),))
    reply(store, record(back))
    store.approve_plan(OWNER, POST, 1, actor=Actor.HUMAN)
    fake = FakeInvenTree()
    waits = await step(store, fake)  # the clicker is not known: the return waits, and is not written
    assert (waits.state, fake.sent) == (S.APPROVED, [])
    assert waits.why == "not written: movement 1: return waits for Apply"
    assert (await step(store, fake, clicked_by=SAM)).state is S.APPROVED  # Sam is no approver
    assert fake.sent == []
    assert (await step(store, fake, clicked_by=LUIS)).state is S.VERIFIED
    assert str(fake.sent[0].body["notes"]).endswith(LUIS)


@pytest.mark.anyio
async def test_a_plan_without_its_record_is_never_written(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, "1700000101.000100", result=record(TAKE))
    store.create_plan(POST, "0" * 64, (Operation.TRANSFER,))  # made from no record the post holds
    with pytest.raises(NotRebuilt, match="no dry-run record"):
        approve_by_policy(store, OWNER, POST, ON)
    assert store.request(KEY).state is S.PLANNED
    store.approve_plan(OWNER, POST, 1, actor=Actor.HUMAN)
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
