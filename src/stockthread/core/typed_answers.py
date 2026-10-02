"""Answers by thread reply: whether a message reads cleanly as the answer to a question (design section 4,
"Answers by thread reply").

Four questions have no buttons: which part, which place, how many and which serials. A message in the thread
answers one of them only when the whole message reads cleanly as that answer; anything else is a thread message
and is read with the post, so a correction is never lost to a parser that took it for an answer. The rules are
narrow on purpose: the whole message, one value, no conjunctions.

This module judges only the text against one question. Who may answer, the message's place in the thread and
replay belong to the worker.

Pure: no I/O. The matchers run on the catalog they are given.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from stockthread.core.matching import Catalog, match_location, match_part
from stockthread.core.model import CandidateMovement, LineItem, Match, MatchRule, MatchStatus, MovementType
from stockthread.core.questions import MAX_CHOICES, Ask, Question
from stockthread.core.signals import CONTAINER_WORDS, LENGTH_UNITS, NUMBER_WORDS

_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?")
_AMBIGUOUS_NUMBER = re.compile(r"\d{1,3}\.\d{3}")  # "1.000": one, or a thousand?
_PIECES = frozenset({"pc", "pcs", "piece", "pieces", "each", "ea", "unit", "units", "x"})
_SEPARATORS = re.compile(r"[\s,]+")
_SERIAL = re.compile(r"(?=[^\d]*\d)[A-Za-z0-9_/.:-]{4,40}")
_MAC = re.compile(r"[0-9A-Fa-f]{12}|[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}|[0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5}")
_NAME_RULES = frozenset({MatchRule.EXACT, MatchRule.NORMALIZED, MatchRule.ALIAS})


@dataclass(frozen=True)
class TypedAnswer:
    """What a message that reads cleanly says. One field is set, by the question's ask."""

    ask: Ask
    quantity: Decimal | None = None
    match: Match | None = None
    """For `name_part` and `name_location`: a match, or a short list of candidates."""
    serials: tuple[str, ...] = ()


def takes_typed_answer(question: Question) -> bool:
    """Only a question that names one line (or, for a place, one side) takes a typed answer. "How many of each
    item?" or "Which item was moved?" on a reading with no lines does not: replies to it are read with the post."""
    if question.ask is Ask.NAME_LOCATION:
        return question.side is not None
    return question.ask in (Ask.NAME_PART, Ask.QUANTITY, Ask.SERIALS) and question.line is not None


def read_typed_answer(
    text: str, question: Question, movement: CandidateMovement, catalog: Catalog, poster: str
) -> TypedAnswer | None:
    """The answer `text` gives to `question` about `movement`, or None when it does not read cleanly as one.
    `poster` is the Slack user id of the post's author: the matchers use it for "my truck" and personal aliases."""
    if not takes_typed_answer(question):
        return None
    if question.ask is Ask.NAME_LOCATION:
        found = read_name(match_location(text, catalog, poster)) if text.strip() else None
        return None if found is None else TypedAnswer(question.ask, match=found)
    if question.line is None or not 0 <= question.line < len(movement.line_items):
        return None
    line = movement.line_items[question.line]
    if question.ask is Ask.NAME_PART:
        found = read_name(match_part(text, catalog, poster)) if text.strip() else None
        return None if found is None else TypedAnswer(question.ask, match=found)
    if question.ask is Ask.QUANTITY:
        quantity = read_quantity(text, line.unit, zero=movement.movement_type is MovementType.COUNT)
        return None if quantity is None else TypedAnswer(question.ask, quantity=quantity)
    serials = read_serials(text, line, catalog, poster)
    return None if serials is None else TypedAnswer(question.ask, serials=serials)


def read_quantity(text: str, unit: str | None, *, zero: bool = False) -> Decimal | None:
    """The whole message is one number, as digits or a number word, above zero (zero only when `zero`, for a
    count). A piece word may follow ("3 pcs"), or the line's own unit when it is not a container ("40 ft").
    "2 boxes", "3 or 4", "a few" and "3, and 2 to truck 2" do not read cleanly: v1.0 never converts packs."""
    words = text.strip().lower().split()
    if not 1 <= len(words) <= 2:
        return None
    number, *rest = words
    if number in NUMBER_WORDS:
        quantity = Decimal(NUMBER_WORDS[number])
    elif _NUMBER.fullmatch(number) and not _AMBIGUOUS_NUMBER.fullmatch(number):
        quantity = Decimal(number.replace(",", ""))
    else:
        return None
    if rest and not (rest[0] in _PIECES or _own_unit(rest[0], unit)):
        return None
    if quantity < 0 or (quantity == 0 and not zero):
        return None
    return quantity


def read_name(match: Match) -> Match | None:
    """A typed name reads cleanly when the matcher, run on the whole message, finds one record (a loose match is
    confirmed by the next question) or a short list of candidates (at most `MAX_CHOICES`)."""
    if match.status is MatchStatus.MATCHED:
        return match
    if match.status is MatchStatus.AMBIGUOUS and len(match.candidates) <= MAX_CHOICES:
        return match
    return None


def read_serials(text: str, line: LineItem, catalog: Catalog, poster: str) -> tuple[str, ...] | None:
    """Only serial-shaped tokens, separated by spaces, commas or new lines, as many as the line's quantity, no
    token twice. A token is serial-shaped when it is 4 to 40 letters, digits and `-_/.:` with at least one digit,
    or a MAC address, and is not a part or location name ("U7-Lite"). So "ok", "thanks", "done" and "3" are not."""
    quantity = line.quantity
    if quantity is None or quantity <= 0 or quantity != quantity.to_integral_value():
        return None
    tokens = tuple(token for token in _SEPARATORS.split(text.strip()) if token)
    if len(tokens) != quantity or len({token.casefold() for token in tokens}) != len(tokens):
        return None
    if not all(_serial_shaped(token, catalog, poster) for token in tokens):
        return None
    return tokens


def _serial_shaped(token: str, catalog: Catalog, poster: str) -> bool:
    if not (_SERIAL.fullmatch(token) or _MAC.fullmatch(token)):
        return False
    return not any(
        found.status is MatchStatus.MATCHED and found.rule in _NAME_RULES for found in _names(token, catalog, poster)
    )


def _names(token: str, catalog: Catalog, poster: str) -> tuple[Match, Match]:
    return match_part(token, catalog, poster), match_location(token, catalog, poster)


def _own_unit(word: str, unit: str | None) -> bool:
    if unit is None or CONTAINER_WORDS.fullmatch(unit):
        return False
    return LENGTH_UNITS.get(word, word) == LENGTH_UNITS.get(unit.lower(), unit.lower())
