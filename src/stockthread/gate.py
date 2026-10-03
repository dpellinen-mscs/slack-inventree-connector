"""The write gate: switch, policy and validation, each able to block alone (design section 6, ADR 0004).

`judge` is evaluated twice: when a plan is approved (by a click on Apply, or by the policy), and again right before
the write, because the switch, the approver list and the stock can change in between. Nothing here writes. A write
is sent only from the requests of a verdict that `allowed`, and only the requests that verdict carries.

Validation is `plan_requests`: a plan it refuses is blocked whole. The policy then says who may approve it:

- An approver's Apply click approves any decided plan, counted only if the clicker is an approver when judged.
- The policy approves it with no click when every movement's type is in `policy.auto_apply` and nothing about the
  plan came from a person or from a loose rule: no answer was applied to it, every part and place was
  found by the exact, normalized or alias rule, and a vehicle is the poster's mapped vehicle or was found by its
  exact location name (ADR 0004; a learned alias for a vehicle still waits). A model flag is a review reason and
  blocks before this.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from stockthread.config import Settings
from stockthread.core.marker import Marker
from stockthread.core.model import Match, MatchRule, MovementType, ReasonSource
from stockthread.core.write_request import WriteRequest
from stockthread.plan import NotDecided, plan_requests
from stockthread.slack.replies import MovementView

_AUTOMATIC_RULES = frozenset({MatchRule.EXACT, MatchRule.NORMALIZED, MatchRule.ALIAS})


class Verdict(StrEnum):
    BLOCKED = "blocked"
    """No write, whoever clicks: writes are off, the plan was rejected, or it is not decided."""
    WAITS = "waits"
    """Decided, but waits for an approver's Apply click."""
    ALLOWED = "allowed"
    """May be written: approved by an approver who still is one, or by the policy."""


@dataclass(frozen=True)
class Judgement:
    verdict: Verdict
    why: str
    """One line for the trail and the log: what blocked it, why it waits, or who approved it."""
    approved_by: str | None = None
    """The approver's Slack user id; None when the policy approved it, or nothing did."""
    requests: Sequence[WriteRequest] = field(default=())
    """The requests to send, one per movement, only when `allowed`."""

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOWED


@dataclass(frozen=True)
class Policy:
    """What the gate reads from the settings, read again for every judgement."""

    writes_enabled: bool
    auto_apply: Collection[MovementType]
    approvers: Collection[str]

    @classmethod
    def of(cls, settings: Settings) -> Policy:
        auto_apply = frozenset(MovementType(value) for value in settings.policy.auto_apply)
        return cls(settings.writes.enabled, auto_apply, frozenset(settings.slack.approvers))


def judge(
    views: Sequence[MovementView],
    markers: Sequence[Marker],
    policy: Policy,
    *,
    poster: str,
    link: str | None = None,
    answered: bool,
    rejected: bool,
    poster_vehicle: int | None,
    clicked_by: str | None = None,
) -> Judgement:
    """Judge one plan of a post.

    `answered`: any answer was applied to the plan, by button or typed reply, including a stock choice (it is
    then never automatic). This flag is the only safeguard for typed and stock answers: a typed name keeps the
    matcher's own rule, and a stock choice changes no match. `poster_vehicle`: the location of the vehicle
    mapped to the poster, if any. `rejected`: a "no" ended
    the plan. `clicked_by`: who clicked Apply on it, if anyone. The requests' notes name the approver, or the poster
    when the policy approved it.
    """
    if not policy.writes_enabled:
        return Judgement(Verdict.BLOCKED, "stock writes are off")
    if rejected:
        return Judgement(Verdict.BLOCKED, "the plan was rejected in the thread")
    approver = clicked_by if clicked_by is not None and clicked_by in policy.approvers else None
    try:
        requests = plan_requests(views, markers, user=approver or poster, link=link)
    except NotDecided as error:
        return Judgement(Verdict.BLOCKED, f"not decided: {error}")
    if approver is not None:
        return Judgement(Verdict.ALLOWED, "approved by an approver", approver, requests)
    if why := _not_automatic(views, policy, answered=answered, poster_vehicle=poster_vehicle):
        return Judgement(Verdict.WAITS, why)
    return Judgement(Verdict.ALLOWED, "approved by the policy", None, requests)


def _not_automatic(
    views: Sequence[MovementView], policy: Policy, *, answered: bool, poster_vehicle: int | None
) -> str | None:
    """Why the policy may not approve this decided plan without a click, or None."""
    if answered:
        return "a person answered a question about it"
    for number, view in enumerate(views, start=1):
        movement = view.movement
        if movement.movement_type not in policy.auto_apply:
            return f"movement {number}: {movement.movement_type.value} waits for Apply"
        if any(reason.source is ReasonSource.MODEL for reason in view.reasons):
            return f"movement {number}: the model raised a flag"
        if not all(_by(match, _AUTOMATIC_RULES) for match in view.items):
            return f"movement {number}: a part was not found by an exact, normalized or alias rule"
        for match in (view.source, view.destination):
            if match is not None and not _by(match, _AUTOMATIC_RULES):
                return f"movement {number}: a place was not found by an exact, normalized or alias rule"
        if movement.movement_type is MovementType.TAKE_TO_VEHICLE and not _vehicle(view.destination, poster_vehicle):
            return f"movement {number}: the vehicle is not the poster's mapped vehicle or found by its exact name"
    return None


def _by(match: Match | None, rules: frozenset[MatchRule]) -> bool:
    return match is not None and match.can_authorize_a_write and match.rule in rules


def _vehicle(match: Match | None, poster_vehicle: int | None) -> bool:
    """The poster's mapped vehicle, or a location found by its exact name (ADR 0004)."""
    if match is None or not match.can_authorize_a_write:
        return False
    return match.rule is MatchRule.EXACT or (poster_vehicle is not None and match.record == poster_vehicle)
