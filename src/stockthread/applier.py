"""The worker's step of the apply protocol (design section 7): approval of a post's plan by the policy, and the next
step of each request one is due for. Both act on the views rebuilt from the dry-run record the plan was made from
(ADR 0016), and judge the gate with what is read now: the settings, the server's version check, the vehicles, the
answers and the plan's own state.

The worker runs `work_request` through `Worker.apply` (`Worker.work_due`). Nothing approves a plan in the run loop
yet: a write the policy approves applies without a click, and ADR 0004 ships that only with its receipt and Undo, so
until then the run loop keeps writes off whatever the settings say.

Known gap: the dry-run record does not say which lines a person named the stock items for, so every line is held to
the rule a pick without a person follows (`precondition.check` with no `chosen`). A person's pick that rule would
not make goes back to review at the write instead of being written; it is never written wrongly.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import timedelta

import anyio

from stockthread.apply import (
    Applied,
    ApplyReader,
    Sender,
    apply_request,
    readback_request,
    recover_request,
    recover_undo,
)
from stockthread.core.marker import AttemptKind, Marker
from stockthread.core.model import PostKey, RequestKey
from stockthread.core.requests import Actor, RequestState
from stockthread.gate import Judgement, Policy, Verdict, judge
from stockthread.plan_record import NotRebuilt, plan_hash, views_of
from stockthread.slack.replies import MovementView
from stockthread.store import PlanRow, Store


@dataclass(frozen=True)
class GateNow:
    """What the gate reads besides the views, read again for every judgement (ADR 0016)."""

    policy: Policy
    vehicles: Mapping[str, int]
    """Slack user id -> the stock location of the person's vehicle (`Catalog.vehicles` of the current catalog)."""


@dataclass(frozen=True)
class PlanViews:
    """A plan revision with the views rebuilt from its dry-run record, and what the gate reads from the post."""

    plan: PlanRow
    views: list[MovementView]
    poster: str
    answered: bool
    """Any question of the post was answered, or the record applied an answer: the plan then waits for Apply."""
    rejected: bool


def plan_views(store: Store, plan: PlanRow) -> PlanViews:
    """`plan` with its views, rebuilt from the newest dry-run record of the post the plan was made from (every record
    whose movements hash to the plan's `input_hash` rebuilds the same views). NotRebuilt when no record is, or the
    plan does not hold one request per movement."""
    for record in reversed(store.dry_run_history(plan.post)):
        result = record.result
        if result is None or plan_hash(result) != plan.input_hash:
            continue
        views = views_of(result, plan.input_hash)
        if len(views) != len(plan.requests):
            raise NotRebuilt("the plan does not hold one request per movement of its record")
        poster = result.get("poster")
        if not isinstance(poster, str):
            raise NotRebuilt("the record names no poster")
        answered = bool(result.get("answers")) or store.post_answers_through(plan.post) > 0
        return PlanViews(plan, views, poster, answered, result.get("rejected") is not None)
    raise NotRebuilt("no dry-run record of the post is the one the plan was made from")


def judge_plan(
    rebuilt: PlanViews, now: GateNow, *, clicked_by: str | None = None, marker: Marker | None = None
) -> Judgement:
    """`gate.judge` of the plan. Each request carries its first apply marker, except the one `marker` is for (the
    attempt about to be made). A plan that is no longer current is blocked whatever the gate would say."""
    plan = rebuilt.plan
    if not plan.current:
        return Judgement(Verdict.BLOCKED, f"plan revision {plan.revision} is no longer current")
    markers = [Marker(row.marker_id, AttemptKind.APPLY, 1) for row in plan.requests]
    if marker is not None:
        sequence = next((row.key.sequence for row in plan.requests if row.marker_id == marker.marker_id), None)
        if sequence is None or marker.kind is not AttemptKind.APPLY:
            raise ValueError("the marker is not the apply marker of a request of this plan")
        markers[sequence - 1] = marker
    return judge(
        rebuilt.views,
        markers,
        now.policy,
        poster=rebuilt.poster,
        answered=rebuilt.answered,
        rejected=rebuilt.rejected,
        poster_vehicle=now.vehicles.get(rebuilt.poster),
        vehicles=frozenset(now.vehicles.values()),
        clicked_by=clicked_by,
    )


def approve_by_policy(store: Store, owner: str, post: PostKey, now: GateNow) -> Judgement | None:
    """Approve the post's current plan when the policy may approve it with no click (ADR 0004), and say why or why
    not. None when there is nothing to approve: no current plan, or one already approved or further on. Raises
    NotRebuilt when the plan's record cannot be acted on, and what `Store.approve_plan` raises when the plan moved on
    in between."""
    plan = store.current_plan(post)
    if plan is None or any(row.state is not RequestState.PLANNED for row in plan.requests):
        return None
    judgement = judge_plan(plan_views(store, plan), now)
    if judgement.allowed:
        store.approve_plan(owner, post, plan.revision, actor=Actor.POLICY)
    return judgement


async def work_request(
    key: RequestKey,
    *,
    store: Store,
    reader: ApplyReader,
    writer: Sender,
    lock: anyio.Lock,
    lock_owner: str,
    gate_now: Callable[[], GateNow],
    settle: timedelta,
) -> Applied:
    """The next step of the apply protocol for request `key` (`Store.requests_to_work`): the write of an `approved`
    request (`apply.apply_request`, judged again under the writer lock with `gate_now()`, the plan and the answers
    read then, with the approver whose click approved the plan, `PlanRow.approved_by`), the read-back of an `applied`
    one (`apply.readback_request`), the recovery of an `unknown` one (`apply.recover_request`), or that of an
    `undo_unknown` one (`apply.recover_undo`).

    Raises NotRebuilt, and changes nothing, when the plan's record cannot be acted on; ValueError for a request in any
    other state; and what the step raises."""
    rebuilt = plan_views(store, store.plan(key.post, key.revision))
    view = rebuilt.views[key.sequence - 1]
    state = store.request(key).state
    if state is RequestState.APPROVED:

        def rejudge(marker: Marker) -> Judgement:
            fresh = plan_views(store, store.plan(key.post, key.revision))
            return judge_plan(fresh, gate_now(), clicked_by=fresh.plan.approved_by, marker=marker)

        return await apply_request(
            key, view, rejudge, store=store, reader=reader, writer=writer, lock=lock, lock_owner=lock_owner
        )
    if state is RequestState.APPLIED:
        return await readback_request(key, view, store=store, reader=reader, lock=lock)
    if state is RequestState.UNKNOWN:
        return await recover_request(key, view, store=store, reader=reader, lock=lock, settle=settle)
    if state is RequestState.UNDO_UNKNOWN:
        return await recover_undo(key, view, store=store, reader=reader, lock=lock, settle=settle)
    raise ValueError(f"request {key} is {state}: no step of the apply protocol is due")


def undo_refusal(
    store: Store, key: RequestKey, *, user: str, poster: str, approvers: Collection[str], max_age: timedelta
) -> str | None:
    """Why the Slack user `user` may not ask for an undo of request `key` now, or None (design section 4, "Clicks at
    ingest"; section 7, "Undo"). The poster of the post (`PlanViews.poster`) or an approver may ask, the approvers as
    the settings say now; then what the store says (`Store.undo_refusal`; `max_age` is `undo.max_age_hours`)."""
    if user != poster and user not in approvers:
        return "only the poster or an approver may undo this movement"
    return store.undo_refusal(key, max_age=max_age)
