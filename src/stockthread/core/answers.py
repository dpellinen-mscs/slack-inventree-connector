# SPDX-License-Identifier: MIT
"""Answers: what the answers given in a post's thread change about one reading of it (design section 4, "Acting on
an answer").

An answer is a fact about the reading its question was asked about. The worker replays every answer given about
the reading that stands, oldest first, onto that same reading read again; this module says what each one does:

- A part or location picked from a list, or a loose match confirmed with "yes", becomes a match by the `answer`
  rule. "None of these" or "no" makes it unmatched, so the next question asks for it by name.
- A movement type picked replaces an unclear one. The caller validates the movement again, so a type that
  contradicts the places raises its own question.
- "Yes" to a doubt about the whole reading, or to a question for approvers, clears the one reason it was asked
  for: its code and detail, for that movement and line only (the line the question named, or none). Any other
  reason (a serial, a quantity, the same doubt about another line, another doubt with the same code, an unclear
  type) is still asked about.
- A stock item picked from a list, or "yes" to a split, names the stock items a line acts on; "None of these" names
  none. "Read the stock again" ends the line's earlier stock answers. The caller reads the stock again on every
  revision and picks from the named items only (`resolve.resolve_lines`).
- "No" to any yes-or-no question about the reading ends the plan: nothing of this reading will be applied. So does
  "Drop it".
- An answer by thread reply (`Typed`, read by `core/typed_answers.py`) gives the line its quantity or serials, or
  the matcher's result for the typed name of its part or place, and settles every reason of that line with the
  code its question was asked for. A quantity answer settles every doubt about that line's quantity; one to a
  pack-unit question also makes the line count pieces.

An answer to a question for approvers counts only while its author is an approver, and one by thread reply only
while its author is the poster or an approver; one that does not fit its
question (a record that was not offered, a movement or line the reading does not have) changes nothing. Neither
ever authorizes a write: the write gate checks the plan again.

Pure: answers in, effects out.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
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
from stockthread.core.typed_answers import TypedAnswer


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


_QUANTITY_DOUBTS = (
    ReasonCode.QUANTITY_MISSING,
    ReasonCode.QUANTITY_VAGUE,
    ReasonCode.QUANTITY_FROM_PHOTO,
    ReasonCode.QUANTITY_NOT_IN_TEXT,
)
"""What a typed quantity settles on its line, whichever of them was asked about. A pack-unit doubt is settled only
by an answer to a pack-unit question: a quantity typed for "how many?" says nothing about what one of them is."""


@dataclass(frozen=True)
class Typed:
    """One answer by thread reply: the id of its question, the question, what the message reads as now, the typed
    text (shown beside the post's own words), who wrote it, and who wrote the post when the question was asked."""

    item: int
    question: Question
    answer: TypedAnswer
    text: str
    by: str
    poster: str | None
    """None for a question from before the poster was stored: only an approver's answer counts."""


@dataclass
class Effects:
    """What the answers replayed onto one reading change. Keys are movement indexes (and line or side)."""

    types: dict[int, MovementType] = field(default_factory=dict)
    parts: dict[tuple[int, int], Match] = field(default_factory=dict)
    places: dict[tuple[int, Side], Match] = field(default_factory=dict)
    stock: dict[tuple[int, int], tuple[int, ...]] = field(default_factory=dict)
    """(movement, line): the stock items a person named for the line, oldest answer first and the newest kept;
    empty for "None of these". A line without an entry picks by the rule."""
    quantities: dict[tuple[int, int], Decimal] = field(default_factory=dict)
    """(movement, line): the quantity typed in the thread."""
    pieces: set[tuple[int, int]] = field(default_factory=set)
    """(movement, line): lines a quantity answer to a pack-unit question set to count pieces."""
    serials: dict[tuple[int, int], tuple[str, ...]] = field(default_factory=dict)
    """(movement, line): the serials typed in the thread. Typed by a person, they count as confirmed."""
    named: dict[tuple[int, int | Side], str] = field(default_factory=dict)
    """(movement, line or side): the name typed in the thread for a part or a place, whose match is in `parts` or
    `places`."""
    retyped: set[int] = field(default_factory=set)
    """The movements an answer by thread reply changed: validation runs on them again."""
    settled: set[tuple[int, int | None, ReasonCode]] = field(default_factory=set)
    """(movement, line or None, code): every reason with that code on that line is settled by an answer by thread
    reply, whatever its detail: the value it doubted was replaced by what a person typed."""
    cleared: set[tuple[int, int | None, ReasonCode, str]] = field(default_factory=set)
    """(movement, line or None, code, detail): the reasons a "yes" cleared (`questions.reason_line` gives a reason's
    line)."""
    rejected: int | None = None
    """The id of the question whose "no" ended the plan, or None."""
    applied: list[int] = field(default_factory=list)
    """The ids of the questions whose answers were applied, in order."""
    ignored: list[int] = field(default_factory=list)
    """The ids of the questions whose answers changed nothing: not from an approver now, or not fitting."""


def replay(given: Sequence[Given | Typed], shape: Sequence[int], approvers: Collection[str]) -> Effects:
    """The effects of `given`, oldest first, on a reading whose movement n has `shape[n]` line items."""
    effects = Effects()
    for answer in given:
        if effects.rejected is not None:
            effects.ignored.append(answer.item)  # the plan has ended: nothing after it is applied
        elif isinstance(answer, Typed):
            (effects.applied if _typed(answer, shape, approvers, effects) else effects.ignored).append(answer.item)
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
    if ask in (Ask.CHOOSE_STOCK, Ask.CONFIRM_STOCK, Ask.READ_STOCK_AGAIN):
        return _stock(answer, effects)
    if ask is Ask.CONFIRM:
        if choice.kind == "no":
            effects.rejected = answer.item  # whatever it doubted, the person says the reading is wrong
            return True
        if choice.kind == "yes" and clears_on_yes(question):
            effects.cleared.add((movement, question.line, question.reason, question.detail))
            return True
    return False  # a question answered by a thread reply has no buttons; a yes to an old stock doubt clears nothing


def _typed(answer: Typed, shape: Sequence[int], approvers: Collection[str], effects: Effects) -> bool:
    question, typed = answer.question, answer.answer
    poster = answer.poster is not None and answer.by == answer.poster and not question.approver_only
    if not (poster or answer.by in approvers) or typed.ask is not question.ask:
        return False
    movement = question.movement
    if movement >= len(shape):
        return False
    if question.ask is Ask.NAME_LOCATION:
        if question.side is None or typed.match is None:
            return False
        effects.places[(movement, question.side)] = typed.match
        effects.named[(movement, question.side)] = answer.text
    else:
        if question.line is None or question.line >= shape[movement] or question.side is not None:
            return False
        line = (movement, question.line)
        if question.ask is Ask.NAME_PART and typed.match is not None:
            effects.parts[line] = typed.match
            effects.named[line] = answer.text
        elif question.ask is Ask.QUANTITY and typed.quantity is not None:
            effects.quantities[line] = typed.quantity
            if question.reason is ReasonCode.PACK_UNIT_AMBIGUOUS:
                effects.pieces.add(line)
        elif question.ask is Ask.SERIALS and typed.serials:
            effects.serials[line] = typed.serials
        else:
            return False
    effects.retyped.add(movement)
    # Some reasons are about the post's text and would come back when the movement is validated again: the same
    # doubt raised by rule and by the model, or a typed quantity that the post's text does not state.
    effects.settled.add((movement, question.line, question.reason))
    if question.ask is Ask.QUANTITY:
        effects.settled.update((movement, question.line, code) for code in _QUANTITY_DOUBTS)
    return True


def _stock(answer: Given, effects: Effects) -> bool:
    question, choice = answer.question, answer.choice
    if question.line is None or question.side is not None:
        return False
    line = (question.movement, question.line)
    if choice.kind == "no" and question.ask is not Ask.CHOOSE_STOCK:
        effects.rejected = answer.item  # "no" to the split, or "Drop it"
        return True
    if question.ask is Ask.CHOOSE_STOCK:
        if choice.kind == "record" and choice.record is not None and choice.record in question.candidates:
            effects.stock[line] = (choice.record,)
            return True
        if choice.kind == "none":
            effects.stock[line] = ()
            return True
    elif question.ask is Ask.CONFIRM_STOCK:
        if choice.kind == "yes" and question.candidates:
            effects.stock[line] = question.candidates
            return True
    elif choice.kind == "again":
        effects.stock.pop(line, None)  # the line's earlier stock answers end; later ones count again
        return True
    return False


def clears_on_yes(question: Question) -> bool:
    """Whether "yes" clears the question's reason: a doubt about the whole reading, or a question for approvers.
    Other confirm questions (a stock doubt asked before the stock question was built) are asked again until
    something else settles them."""
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
