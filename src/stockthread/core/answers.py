"""Answers: what the answers given in a post's thread change about one reading of it (design section 4, "Acting on
an answer").

An answer is a fact about the reading its question was asked about. The worker replays every answer given about
the reading that stands, oldest first, onto that same reading read again; this module says what each one does:

- A part or location picked from a list, or a loose match confirmed with "yes", becomes a match by the `answer`
  rule. "None of these" or "no" makes it unmatched, so the next question asks for it by name.
- A movement type picked replaces an unclear one. The caller validates the movement again, so a type that
  contradicts the places raises its own question.
- "Yes" to a doubt about the whole reading, or to a question for approvers, clears the reason code it was asked
  for, for that movement only. Any other reason (a serial, a quantity) is still asked about.
- "No" to either ends the plan: nothing of this reading will be applied.

An answer to a question for approvers counts only while its author is an approver; one that does not fit its
question (a record that was not offered, a movement or line the reading does not have) changes nothing. Neither
ever authorizes a write: the write gate checks the plan again.

Pure: answers in, effects out.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from stockthread.core.model import Match, MatchRule, MatchStatus, MovementType, ReasonCode
from stockthread.core.questions import (
    Ask,
    Choice,
    Question,
    Side,
    Tier,
    choice_from_json,
    question_from_json,
    tier_of,
)


@dataclass(frozen=True)
class Given:
    """One answer as stored: the id of its question, the question, the button chosen, and who chose it."""

    item: int
    question: Question
    choice: Choice
    by: str

    @classmethod
    def stored(cls, item: int, question: Mapping[str, Any], answer: Mapping[str, Any], by: str) -> Given:
        """From the stored question and answer. Raises ValueError for anything the store does not hold."""
        return cls(item, question_from_json(question), choice_from_json(answer), by)


@dataclass
class Effects:
    """What the answers replayed onto one reading change. Keys are movement indexes (and line or side)."""

    types: dict[int, MovementType] = field(default_factory=dict)
    parts: dict[tuple[int, int], Match] = field(default_factory=dict)
    places: dict[tuple[int, Side], Match] = field(default_factory=dict)
    cleared: set[tuple[int, ReasonCode]] = field(default_factory=set)
    rejected: int | None = None
    """The id of the question whose "no" ended the plan, or None."""
    applied: list[int] = field(default_factory=list)
    """The ids of the questions whose answers were applied, in order."""
    ignored: list[int] = field(default_factory=list)
    """The ids of the questions whose answers changed nothing: not from an approver now, or not fitting."""


def replay(given: Sequence[Given], shape: Sequence[int], approvers: Collection[str]) -> Effects:
    """The effects of `given`, oldest first, on a reading whose movement n has `shape[n]` line items."""
    effects = Effects()
    for answer in given:
        if effects.rejected is not None:
            effects.ignored.append(answer.item)  # the plan has ended: nothing after it is applied
        elif _apply(answer, shape, approvers, effects):
            effects.applied.append(answer.item)
        else:
            effects.ignored.append(answer.item)
    return effects


def _apply(answer: Given, shape: Sequence[int], approvers: Collection[str], effects: Effects) -> bool:
    question, choice = answer.question, answer.choice
    if question.approver_only and answer.by not in approvers:
        return False
    movement = question.movement
    if movement >= len(shape) or (question.line is not None and question.line >= shape[movement]):
        return False
    ask = question.ask
    if ask is Ask.MOVEMENT_TYPE:
        if choice.kind != "type" or choice.movement_type is None:
            return False
        effects.types[movement] = choice.movement_type
        return True
    if ask in (Ask.CHOOSE_PART, Ask.CONFIRM_PART, Ask.CHOOSE_LOCATION, Ask.CONFIRM_LOCATION):
        match = _match(ask, question.candidates, choice)
        if match is None:
            return False
        if ask in (Ask.CHOOSE_PART, Ask.CONFIRM_PART):
            if question.line is None or question.side is not None:
                return False
            effects.parts[(movement, question.line)] = match
        else:
            if question.side is None:
                return False
            effects.places[(movement, question.side)] = match
        return True
    if ask is Ask.CONFIRM and clears_on_yes(question):
        if choice.kind == "yes":
            effects.cleared.add((movement, question.reason))
            return True
        if choice.kind == "no":
            effects.rejected = answer.item
            return True
    return False  # a question answered by a thread reply has no buttons; any other confirm clears nothing yet


def clears_on_yes(question: Question) -> bool:
    """Whether "yes" clears the question's reason: a doubt about the whole reading, or a question for approvers.
    Other confirm questions (stock, until source selection asks its own) are asked again until something else
    settles them."""
    return question.ask is Ask.CONFIRM and (question.approver_only or tier_of(question) is Tier.READING)


def _match(ask: Ask, candidates: tuple[int, ...], choice: Choice) -> Match | None:
    if ask in (Ask.CHOOSE_PART, Ask.CHOOSE_LOCATION):
        if choice.kind == "record" and choice.record is not None and choice.record in candidates:
            return Match(MatchStatus.MATCHED, MatchRule.ANSWER, choice.record)
        return Match(MatchStatus.UNMATCHED) if choice.kind == "none" else None
    if len(candidates) != 1:
        return None
    if choice.kind == "yes":
        return Match(MatchStatus.MATCHED, MatchRule.ANSWER, candidates[0])
    return Match(MatchStatus.UNMATCHED) if choice.kind == "no" else None
