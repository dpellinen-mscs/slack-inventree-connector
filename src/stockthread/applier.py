"""The worker's step of the apply protocol (design section 7): approval of a post's plan by the policy, and the next
step of each request one is due for. Both act on the views rebuilt from the dry-run record the plan was made from
(ADR 0016), and judge the gate with what is read now: the settings, the server's version check, the vehicles, the
answers and the plan's own state.

The worker runs `approve_by_policy` through `Worker.approve` once, right after the reply that made a plan is
recorded (`Worker.approve_due`), and `work_request` through `Worker.apply` (`Worker.work_due`); `stockthread run`
sets both only when writes are on and the server is tested (`run.inventree_steps`, `run.policy_approver`). A plan
the policy does not approve waits for an approver's Apply click (`approve_by_click`, through `Worker._handle_apply`
and `run.click_approver`); only then is it written.

Known gap: the dry-run record does not say which lines a person named the stock items for, so every line is held to
the rule a pick without a person follows (`precondition.check` with no `chosen`). A person's pick that rule would
not make goes back to review at the write instead of being written; it is never written wrongly.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

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
from stockthread.core.grouping import seconds
from stockthread.core.marker import AttemptKind, Marker
from stockthread.core.model import MovementType, PostKey, RequestKey
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
    default_sources: Mapping[MovementType, int] = field(default_factory=dict)
    """Movement type -> its default source now (`Catalog.default_sources` of the current catalog). A plan's source
    taken from a default counts for the policy only while it is still that type's default."""


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
        default_sources=now.default_sources,
        clicked_by=clicked_by,
    )


def approve_by_policy(store: Store, owner: str, post: PostKey, now: GateNow) -> Judgement | None:
    """Approve the post's current plan when the policy may approve it with no click (ADR 0004), and say why or why
    not. None when there is nothing to approve: no current plan, or one already approved or further on. Raises
    NotRebuilt when the plan's record cannot be acted on, and what `Store.approve_plan` raises when the plan moved on
    in between.

    Only a plan its first dry run made is ever approved by the policy (ADR 0019): one made after an edit, a deletion,
    a joined message or an answer waits for Apply even when the gate would allow it, so an edit after an undo never
    writes the movement again unasked, and neither does an edit that turned chatter into a movement. The plan row's
    revision is not enough: a post's first plan row may come from a later dry run."""
    plan = store.current_plan(post)
    if plan is None or any(row.state is not RequestState.PLANNED for row in plan.requests):
        return None
    judgement = judge_plan(plan_views(store, plan), now)
    if judgement.allowed and plan.revision > 1:
        return Judgement(
            Verdict.WAITS, f"plan revision {plan.revision}: only a post's first plan applies with no click"
        )
    if judgement.allowed and (len(store.dry_run_history(post)) > 1 or store.post_has_revision(post)):
        return Judgement(
            Verdict.WAITS, "the post was edited or read again: only its first dry run applies with no click"
        )
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
    expired: Callable[[PostKey], bool],
    settle: timedelta,
    send_by: float | None = None,
) -> Applied:
    """The next step of the apply protocol for request `key` (`Store.requests_to_work`): the write of an `approved`
    request (`apply.apply_request`, judged again under the writer lock with `gate_now()`, the plan and the answers
    read then, with the approver whose click approved the plan, `PlanRow.approved_by`), the read-back of an `applied`
    one (`apply.readback_request`), the recovery of an `unknown` one (`apply.recover_request`), or that of an
    `undo_unknown` one (`apply.recover_undo`). `send_by`: as `apply.apply_request`'s.

    `expired(post)` (`plan_expired`, now): a plan that expired before its first write began goes back to review,
    however it was approved, and is never written; so does one that expires while it is judged again under the lock,
    or during the reads before the write (asked again right before the attempt is begun).
    A plan whose first write began is finished: half a movement in InvenTree is worse than a late one, and each
    write still checks the stock first.

    Raises NotRebuilt, and changes nothing, when the plan's record cannot be acted on; ValueError for a request in any
    other state; and what the step raises."""
    rebuilt = plan_views(store, store.plan(key.post, key.revision))
    view = rebuilt.views[key.sequence - 1]
    state = store.request(key).state
    if state is RequestState.APPROVED:
        if not _plan_began(rebuilt.plan) and expired(key.post):
            store.transition(key, expected=RequestState.APPROVED, target=RequestState.PLANNED, actor=Actor.APPLIER)
            return Applied(RequestState.PLANNED, "back to review: the plan expired before it was written")

        def rejudge(marker: Marker) -> Judgement:
            fresh = plan_views(store, store.plan(key.post, key.revision))
            if not _plan_began(fresh.plan) and expired(key.post):
                return Judgement(Verdict.BLOCKED, "the plan expired")
            clicked_by = fresh.plan.approved_by
            judgement = judge_plan(fresh, gate_now(), clicked_by=clicked_by, marker=marker)
            if clicked_by is not None and judgement.allowed and judgement.approved_by != clicked_by:
                # The clicker is no longer an approver: the policy never approved this plan (ADR 0019).
                return Judgement(Verdict.WAITS, "the approver who clicked Apply is no longer an approver")
            return judgement

        def too_late() -> str | None:
            # Asked again after the reads: they may cross the expiry (review #141).
            if not _plan_began(store.plan(key.post, key.revision)) and expired(key.post):
                return "the plan expired before it was written"
            return None

        return await apply_request(
            key, view, rejudge, store=store, reader=reader, writer=writer, lock=lock, lock_owner=lock_owner,
            send_by=send_by, too_late=too_late,
        )  # fmt: skip
    if state is RequestState.APPLIED:
        return await readback_request(key, view, store=store, reader=reader, lock=lock)
    if state is RequestState.UNKNOWN:
        return await recover_request(key, view, store=store, reader=reader, lock=lock, settle=settle)
    if state is RequestState.UNDO_UNKNOWN:
        return await recover_undo(key, view, store=store, reader=reader, lock=lock, settle=settle)
    raise ValueError(f"request {key} is {state}: no step of the apply protocol is due")


def plan_expired(store: Store, post: PostKey, at: Decimal, expiry_hours: float) -> bool:
    """Whether the post's plan expired at `at` (Slack time): `expiry_hours` after the time `Store.plan_expires_from`
    gives, as the worker counts it. A post with no such time counts as expired."""
    expires_from = store.plan_expires_from(post)
    return expires_from is None or at - seconds(expires_from) > Decimal(str(expiry_hours)) * 3600


def _plan_began(plan: PlanRow) -> bool:
    """Whether a write of the plan began: any request of it is past `approved`, other than one a person rejected."""
    return any(
        row.state not in (RequestState.PLANNED, RequestState.APPROVED, RequestState.REJECTED) for row in plan.requests
    )


def approve_by_click(
    store: Store,
    owner: str,
    post: PostKey,
    revision: int,
    user: str,
    now: GateNow,
    *,
    at: Decimal,
    expiry_hours: float,
) -> Judgement | None:
    """Approve plan `revision` of the post for `user`, who clicked its Apply button, when the gate allows it with
    that click (`gate.judge`, `clicked_by`), and say why or why not. A click never approves by the policy: a user who
    is not an approver is refused even when the policy would approve the plan with no click. Blocked when the
    revision is not the post's current plan, when an undo of an earlier revision of the post was asked for, or when
    its plan expired at `at`: `expiry_hours` after the time
    `Store.plan_expires_from` gives, as the worker counts it. None when the plan is approved already or further on.
    Raises NotRebuilt when the plan's record cannot be acted on, and what `Store.approve_plan` raises (StateConflict,
    WriteBlocked) when the plan moved on in between or the post may not be approved now."""
    if user not in now.policy.approvers:
        return Judgement(Verdict.BLOCKED, "only an approver may apply a movement")
    plan = store.current_plan(post)
    if plan is None or plan.revision != revision:
        return Judgement(Verdict.BLOCKED, f"plan revision {revision} is no longer current")
    if any(row.state is not RequestState.PLANNED for row in plan.requests):
        return None
    if store.undone_before(post, revision):
        # An edit that changed nothing makes the plan again after its undo: the receipt above says it was undone.
        return Judgement(Verdict.BLOCKED, "this movement was undone; post it again to move the stock again")
    if plan_expired(store, post, at, expiry_hours):
        return Judgement(Verdict.BLOCKED, "the plan expired")
    judgement = judge_plan(plan_views(store, plan), now, clicked_by=user)
    if judgement.allowed:
        if judgement.approved_by != user:
            raise AssertionError("a click approved by the policy")  # the gate returns the approver it was given
        store.approve_plan(owner, post, revision, actor=Actor.HUMAN, approver=user)
    return judgement


def undo_refusal(
    store: Store, key: RequestKey, *, user: str, poster: str, approvers: Collection[str], max_age: timedelta
) -> str | None:
    """Why the Slack user `user` may not ask for an undo of request `key` now, or None (design section 4, "Clicks at
    ingest"; section 7, "Undo"). The poster of the post (`PlanViews.poster`) or an approver may ask, the approvers as
    the settings say now; then what the store says (`Store.undo_refusal`; `max_age` is `undo.max_age_hours`)."""
    if user != poster and user not in approvers:
        return "only the poster or an approver may undo this movement"
    return store.undo_refusal(key, max_age=max_age)
