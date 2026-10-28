# SPDX-License-Identifier: MIT
"""Validate: review reasons derived by rule from the post text and the candidate (design section 7).

Every reason an extractor can raise is also derived here by rule, where the text or the fields
allow it, so an extractor that leaves a flag out cannot remove a review reason. Extractor flags
still add reasons. Nothing here reads the catalog or live stock: matching and resolve add their
own reasons later.

A reason's detail names the check, never the message text, so reasons are safe to log.
"""

from __future__ import annotations

import contextlib
import re
import unicodedata
from collections import Counter
from collections.abc import Collection, Sequence
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from stockthread.core.model import (
    CandidateMovement,
    MessageInput,
    MovementType,
    PostFlag,
    ReasonCode,
    ReasonSource,
    ReviewReason,
    reason_for_flag,
)
from stockthread.core.signals import (
    CONTAINER_WORDS,
    CORRECTION,
    HEDGE,
    LATER,
    LENGTH_UNITS,
    MENTION,
    NEGATION,
    NUMBER_WORDS,
    PLAN_OR_CONDITION,
    QUESTION_OR_REQUEST,
    SITE_WORDS,
    SUSPICIOUS,
    THIRD_PERSON,
    VAGUE,
    VEHICLE_WORDS,
)

_NUMBER_TOKEN = re.compile(r"^(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(x|-\w+)?$", re.IGNORECASE)
"""A number token: "2", "2x", "12-piece", "1,050". Not "van3" or "d40"."""
_SCALES = frozenset(
    {"dozen", "score", "gross", "hundred", "hundreds", "thousand", "thousands", "k", "million", "millions", "grand"}
)
_FRACTION = re.compile(r"^\d*[/\u2044]\d*$")
_AMBIGUOUS_NUMBER = re.compile(r"^\d{1,3}\.\d{3}$")
""""2.000": two, or two thousand written the European way. It states neither."""
_FILLER = frozenset(
    {"of", "x", "about", "around", "approx", "approximately", "roughly", "~", "half", "each", "ea", "pc", "pcs"}
    | {"piece", "pieces", "unit", "units", "ft", "feet", "foot", "m", "meter", "meters", "metre", "metres"}
)
"""Words that may sit between a quantity and its item: "5 boxes of", "300 ft of", "2 of the 5"."""
_EACH = frozenset({"each", "ea", "pc", "piece", "unit", "x"})
"""Unit words that mean a count, in the singular: the same as no unit."""
_WHOLE = frozenset({"both", "all"})
"""Words that count without a number. They state nothing: "all the ONTs" is not one ONT."""
_HALVES = (["half"], ["half", "a"], ["half", "an"], ["half", "dozen"], ["half", "a", "dozen"], ["a", "half", "dozen"])
"""The only shapes in which "half" is read: a half (0.5), or half a dozen (6)."""
_ONE = frozenset({"a", "an", "the", "one", "single", "this", "that"})
_WORD_QUANTITIES = {"dozen": Decimal(12), "pair": Decimal(2), "couple": Decimal(2), "half": Decimal("0.5")}
_QUANTITY_WINDOW = 4
"""How many words before an item may hold its quantity: "5 boxes of", "a 12-piece box of", "2 of the 5"."""
_SEPARATOR = re.compile(r"(?<!\d),|,(?!\d)|[;:\n+&]|\band\b|→|->")
"""Where one item's words end. A comma between digits is not one: "1,050" is a number, "1,5" states nothing."""
_TYPE_VERBS = {
    MovementType.COUNT: re.compile(r"\b(count(ed)?|stocktake|inventory)\b"),
    MovementType.RECEIVE: re.compile(r"\b(receiv\w*|rcvd|arrived|delivery|delivered|came in)\b"),
    MovementType.CONSUME: re.compile(
        r"\b(install\w*|used|use|replaced|swapped|consumed|ran|broke|broken|lost|damaged|toast|dead)\b"
    ),
    MovementType.RETURN: re.compile(r"\b(return\w*|brought back|back to|put back)\b|\bswapped\b"),
    MovementType.TRANSFER: re.compile(r"\b(moved?|transferr?\w*)\b"),
}
"""A movement type that changes stock in a way a take cannot needs one of its words in the text."""
_TAKE_OUTS = (MovementType.TAKE_TO_VEHICLE, MovementType.TAKE_TO_SITE)
_FROM = r"(?<!\w)(from|frm|off|out\s+of|out-of|out the|outta|outa)"
_ARROW = r"(?<![-\u2013\u2014=])[-\u2013\u2014=]+(>|&gt;)|[→⇒⟶➔➜➝➞➡⇨]|(?<=\w) (>|&gt;) (?=\w)"
"""An arrow, typed or a symbol: "->", "=>", a dash then ">", "→", "⇒", "⟶", or a lone ">" between two words. Slack
sends a typed ">" as "&gt;"."""
_TO = rf"((?<!\w)(to|into|onto|on|in|at|for|up)|{_ARROW})"
"""Whole words only: "from main shop" must not read as "…in shop"."""
_NAMES_A_SOURCE = re.compile(
    rf"(?<!\bdrop )(?<!\bdropped ){_FROM}[\s\u200b-\u200d\u2060\ufeff:\"'\u201c\u2018(\[<#*_~`…-]+\w"
    rf"|{_ARROW}|(?<!\w)(source|src)\b"
    r"|(?<!\w)(at|@) ?(the )?(shelf|shelves|bin|rack|warehouse|shop|stockroom|storeroom|storage|cage|receiving|yard"
    r"|depot)\b",
    re.IGNORECASE,
)
"""Wording that names a source: "from shelf B", "off the van", "out of stock", "from 3rd floor cage", "from: shelf B",
"truck 1 → truck 2", "source: shelf B", "grabbed 2 at shelf B". "from 3 boxes" counts too, as "from 3 boxes on
shelf B" names shelf B. "dropped off" names a destination. Any arrow counts, so "2 wall mounts → my truck" is asked
for its source once. "at" counts only before a storage word: "at the Smith job" is where stock goes, and "at truck 1"
may be either. A miss lets a default source settle a source the text names (T19), so this errs towards asking."""


def _rule(code: ReasonCode, detail: str = "") -> ReviewReason:
    return ReviewReason(code, ReasonSource.RULE, detail)


DETAIL_SOURCE_LEFT_OUT = 'the text has "from", "off", "out of" or an arrow, but no source was read'
"""The detail of the implied source a text that names one raises: a default source never settles it (ADR 0020)."""
DETAIL_PLACE_NAMED = "the text names a stock location that is neither the destination nor the default source"
"""The detail of the implied source a text that names another stock location with no "from" word raises ("pulled
2 ONTs shelf B to van 3"): a default source never settles it either (the worker adds it, as it needs the catalog)."""
DETAIL_OTHER_AUTHOR = "a message in the post is by someone else"
"""ACTOR_NOT_POSTER for a post whose messages are not all the poster's: its "my truck" is read as the poster's."""
DETAIL_MIXED = "the fields come from different clauses, and one of them names another place or number"
"""The detail of the reason a movement drawn from several clauses raises when the text names a rival place (#200)."""
DETAIL_NOT_A_COUNT = "a unit other than a count"
"""The detail of the reason a line in feet, metres or any unit but a count raises."""
DETAIL_UNIT_IN_ITEM = "the item starts with a unit word"
"""The detail of the reason an item text like "ft drop cable" raises: its unit was read as part of the item."""
DETAIL_PACK_SIZE = "a pack size"
"""The detail of the reason a line with a pack size ("a 12-piece box") raises."""
MEASURE_WORDS = (
    frozenset(LENGTH_UNITS)
    | {"yd", "yds", "yard", "yards", "inch", "inches"}
    | {
        "kg",
        "kgs",
        "lb",
        "lbs",
        "gal",
        "gallon",
        "gallons",
    }
)
"""Units of length, weight or volume: a quantity in one of them is not a count."""
_FEET_OR_INCHES = "['\"\N{RIGHT DOUBLE QUOTATION MARK}]"
MEASURE = re.compile(
    rf"(?<![\w.,])\d+(?:[.,]\d+)*(?:\s*(?:{'|'.join(sorted(MEASURE_WORDS))})\b"
    rf"|{_FEET_OR_INCHES}|\s+{_FEET_OR_INCHES}(?!\w))"
)
""""50 ft", "3m", "2.5 metres", "100'", '12"cable', "2 kg": a measure in the text, whatever the extractor made of it.
A quote mark after a space with a word right after it opens a quoted name: 'took 2 "Disc 40"'."""
TYPE_UNCLEAR = "type unclear"
"""The detail of the reason an unclear movement type raises (other MOVEMENT_UNCLEAR details doubt the text)."""


def validate(
    movement: CandidateMovement,
    text: str,
    *,
    max_quantity: int,
    post_flags: frozenset[PostFlag] = frozenset(),
    stocking: Sequence[str | None] = (),
    kinds: Collection[MovementType] = (),
) -> tuple[ReviewReason, ...]:
    """Every review reason for one candidate. `text` is the post's text by people, all messages joined.

    `stocking` holds, per line, the units of the line's part (InvenTree's `Part.units`), given only for a part
    matched so that it can authorize a write, else None. A line whose length is in its part's own unit
    (`in_stocking_unit`: "50 m" of a part stocked in m), stated by a number in the text (`_lone_number`), is
    a count of that unit and raises no pack-unit reason for its unit. `kinds` holds the type of every movement of
    the reading: a return among them accounts for a return word in the text. The caller judges the part; nothing here
    reads the catalog."""
    reasons = {reason_for_flag(flag) for flag in movement.flags}
    reasons |= {reason_for_flag(flag) for flag in post_flags if flag is not PostFlag.CONTAINS_SENSITIVE}
    lone = _lone_number(movement, text)
    text = _folded(text)
    stocked = frozenset(
        n
        for n, (item, units) in enumerate(zip(movement.line_items, stocking, strict=False), start=1)
        if lone and in_stocking_unit(item.unit, units)
    )
    # Only a line a measure in the text states exactly: "50 cm", "1,5 m" or no measure at all leaves it a question.
    own = frozenset(
        n
        for found in MEASURE.finditer(text)
        if not _in_a_name(movement, text, found)
        for n in _own_measure(movement, found, stocked)
    )
    reasons |= _from_fields(movement, text, own)
    reasons |= _from_text(movement, text, own, kinds)
    reasons |= _grounding(movement, text)
    reasons |= _places(movement, text)
    reasons |= _one_clause(movement, text)
    reasons |= _contradictions(movement)
    reasons |= {
        _rule(ReasonCode.QUANTITY_ABOVE_LIMIT, f"line {n}: above policy.max_quantity")
        for n, item in enumerate(movement.line_items, start=1)
        if item.quantity is not None and item.quantity > max_quantity
    }
    return tuple(sorted(reasons, key=lambda reason: (reason.code.value, reason.source.value, reason.detail)))


def photo_not_read(messages: Sequence[MessageInput]) -> bool:
    """A message of the post that is not deleted carries a file. No extractor reads files, so every movement of
    such a post has the reason PHOTO_NOT_READ."""
    return any(message.file_ids for message in messages if not message.deleted)


def _from_fields(movement: CandidateMovement, text: str, own: frozenset[int]) -> set[ReviewReason]:
    """The flags that follow from the fields (eval/README.md, "Flags"), and an unclear type. The lines in `own` are
    in their part's own unit."""
    kind = movement.movement_type
    reasons = set()
    if movement.source_text is None and kind not in (MovementType.RECEIVE, MovementType.COUNT):
        named = _NAMES_A_SOURCE.search(text) is not None
        reasons.add(_rule(ReasonCode.IMPLIED_SOURCE, DETAIL_SOURCE_LEFT_OUT if named else ""))
    if movement.destination_text is None and kind is not MovementType.CONSUME:
        reasons.add(_rule(ReasonCode.IMPLIED_DESTINATION))
    if not movement.line_items or any(not (item.item_text or "").strip() for item in movement.line_items):
        reasons.add(_rule(ReasonCode.ITEMS_MISSING))
    if any(item.quantity is None for item in movement.line_items):
        reasons.add(_rule(ReasonCode.QUANTITY_MISSING, "no number"))
    for n, item in enumerate(movement.line_items, start=1):
        if not counts_pieces(item.unit) and n not in own:
            reasons.add(_rule(ReasonCode.PACK_UNIT_AMBIGUOUS, f"line {n}: {DETAIL_NOT_A_COUNT}"))
        if item.pack_size is not None:
            reasons.add(_rule(ReasonCode.PACK_UNIT_AMBIGUOUS, f"line {n}: {DETAIL_PACK_SIZE}"))
        first = _folded(item.item_text or "").split(" ", 1)[0]
        if first in MEASURE_WORDS or CONTAINER_WORDS.fullmatch(first):
            reasons.add(_rule(ReasonCode.PACK_UNIT_AMBIGUOUS, f"line {n}: {DETAIL_UNIT_IN_ITEM}"))
        if item.serials and (item.quantity is None or len(item.serials) != item.quantity):
            reasons.add(_rule(ReasonCode.SERIAL_UNCONFIRMED, f"line {n}: serial count differs from the quantity"))
        if any(not _has_words(serial, text) for serial in item.serials):
            reasons.add(_rule(ReasonCode.SERIAL_UNCONFIRMED, f"line {n}: serial not in the text"))
    if not text.strip():
        reasons.add(_rule(ReasonCode.PHOTO_ONLY, "no text"))
    if kind is MovementType.UNCLEAR:
        reasons.add(_rule(ReasonCode.MOVEMENT_UNCLEAR, TYPE_UNCLEAR))
    return reasons


def counts_pieces(unit: str | None) -> bool:
    """The unit is a count: none, or "each", "pc", "x". A request carries the quantity alone, as a number of the
    part's stocking unit: "50 ft" may be 50 m or 50 pieces once written, unless `in_stocking_unit` holds."""
    unit = _unit(unit)
    return unit is None or unit in _EACH


def stocking_unit(units: str | None) -> str | None:
    """The length unit a part is stocked in, from InvenTree's `Part.units`: "m" for m, meter(s) or metre(s), "ft"
    for ft, foot or feet, else None. Only the same unit counts, never a conversion: cm, mm and inches are left out
    ("in" is also a preposition), and so is "M", which is not a metre."""
    if units is None or units.strip() == "M":
        return None
    return LENGTH_UNITS.get(units.strip().casefold())


def in_stocking_unit(unit: str | None, units: str | None) -> bool:
    """A line's unit is the length unit its part is stocked in (`stocking_unit` of the part's `units`): then the
    line's quantity is a number of the part's own unit, and a request may carry it bare."""
    stocked = stocking_unit(units)
    return stocked is not None and stocking_unit(unit) == stocked


def _unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    unit = unit.casefold().strip()
    return unit[:-2] if unit.endswith("xes") else unit[:-1] if unit.endswith("s") else unit


def _from_text(
    movement: CandidateMovement, text: str, own: frozenset[int], kinds: Collection[MovementType] = ()
) -> set[ReviewReason]:
    """Reasons the text gives, whatever the extractor made of it. The lines in `own` are in their part's own unit."""
    reasons = set()
    if SUSPICIOUS.search(text):
        reasons.add(_rule(ReasonCode.SUSPICIOUS_INSTRUCTIONS))
    for found in CONTAINER_WORDS.finditer(text):
        reasons |= _unit_reasons(movement, text, found, "container word in the text")
    for found in MEASURE.finditer(text):
        if not _in_a_name(movement, text, found):
            reasons |= _unit_reasons(movement, text, found, "measure in the text", _own_measure(movement, found, own))
    if VAGUE.search(text):
        reasons.add(_rule(ReasonCode.QUANTITY_VAGUE, "vague quantity word in the text"))
    if CORRECTION.search(text):
        reasons.add(_rule(ReasonCode.CORRECTED_IN_THREAD, "correction word in the text"))
    if THIRD_PERSON.search(text):
        reasons.add(_rule(ReasonCode.ACTOR_NOT_POSTER, "someone else is named as the actor"))
    elif MENTION.search(text):
        reasons.add(_rule(ReasonCode.ACTOR_NOT_POSTER, "a person is mentioned"))
    verbs = _TYPE_VERBS.get(movement.movement_type)
    if verbs is not None and not verbs.search(text):
        reasons.add(_rule(ReasonCode.MOVEMENT_UNCLEAR, f"no word in the text supports {movement.movement_type.value}"))
    returned = MovementType.RETURN in kinds
    if movement.movement_type in _TAKE_OUTS and not returned and _TYPE_VERBS[MovementType.RETURN].search(text):
        # A take-out and a return are both one transfer, but only take-outs may apply with no click (#199). With a
        # return in the reading the plan is not all take-outs, so the policy waits for Apply (gate.py), unless
        # `policy.auto_apply` has return too, and then the label does not matter.
        reasons.add(
            _rule(ReasonCode.MOVEMENT_UNCLEAR, f"a return word in the text, read as {movement.movement_type.value}")
        )
    for pattern, detail in (
        (QUESTION_OR_REQUEST, "question or request"),
        (PLAN_OR_CONDITION, "plan or condition"),
        (LATER, "a time ahead"),
        (NEGATION, "negation"),
        (HEDGE, "hedge"),
    ):
        if pattern.search(text):
            reasons.add(_rule(ReasonCode.MOVEMENT_UNCLEAR, detail))
    return reasons


def _lone_number(movement: CandidateMovement, text: str) -> bool:
    """Each number in the post's text is one line's quantity, one for one, and nothing in it may multiply one, so a
    measure in it can state a line's quantity in its part's own unit. Else "50 m" may be more than 50 m once
    written: "3 runs of 50 m", "2x 50 m", "50 m x2", "50 m for each of the 3 houses", "50 m, twice", the measure
    posted twice, a number word, or "50 M" (not a metre), which only the text before folding tells apart. A number
    inside a word ("cat6", "van2", a mention) or in a place's text word for word ("van 3") is part of a name; the
    place must still match a location that can authorize a write. Strict on purpose: what it leaves out is asked
    about, never written."""
    if re.search(r"\dM\b|\d\s+M\b", unicodedata.normalize("NFKC", text)):
        return False
    text = _folded(text)
    # Before a place's text is taken out, so a place cannot hide one ("from shop 2x").
    if re.search(r"\bx\s*\d|\d\s*x\b|\N{MULTIPLICATION SIGN}|\+\+", text) or any(
        word in NUMBER_WORDS or word in _MULTIPLIER_WORDS for word in re.findall(r"[a-z]+", text)
    ):
        return False
    for place in (movement.source_text, movement.destination_text):
        if place and (found := next(iter(_occurrences(place, text)), None)):
            text = f"{text[: found.start()]} {text[found.end() :]}"  # once: a second "van 3" may be another 50 m
    numbers = re.findall(r"(?<![^\W\d_])\d+(?:[.,]\d+)*", text)
    if any("," in number or _AMBIGUOUS_NUMBER.fullmatch(number) for number in numbers):
        return False
    return Counter(Decimal(number) for number in numbers) == Counter(item.quantity for item in movement.line_items)


_MULTIPLIER_WORDS = frozenset(
    {"twice", "thrice", "times", "each", "per", "every", "apiece", "double", "triple", "both", "pair", "pairs"}
    | {"couple", "dozen", "dozens", "half", "halves", "lengths", "runs", "cuts", "pieces", "again", "another"}
    | {"same", "ditto", "too", "also", "plus", "all", "extra", "more", "dbl"}
)
"""Words that may multiply a measure or repeat it. A list of places with none of them ("for unit A and unit B") is
not caught, as it is not for a count either; the reply shows the quantity it would write."""


def _own_measure(movement: CandidateMovement, found: re.Match[str], own: frozenset[int]) -> frozenset[int]:
    """The lines of `own` this measure states exactly: its number is the line's quantity, and its unit word the
    line's unit. A measure in another unit ("50 m (164 ft)"), in quote marks, or with a number whose separator could
    be read two ways ("1,5 m", "1.000 m", "1,050 m") states none."""
    stated = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]+)", found.group(0))
    if stated is None or _AMBIGUOUS_NUMBER.fullmatch(stated.group(1)):
        return frozenset()
    with contextlib.suppress(InvalidOperation):
        value = Decimal(stated.group(1).replace(",", ""))
        return frozenset(
            n
            for n in own
            if movement.line_items[n - 1].quantity == value
            and stocking_unit(stated.group(2)) == stocking_unit(movement.line_items[n - 1].unit)
        )
    return frozenset()


def _unit_reasons(
    movement: CandidateMovement, text: str, found: re.Match[str], detail: str, exact: frozenset[int] = frozenset()
) -> set[ReviewReason]:
    """The pack-unit reasons of a unit word in the text, each naming a line the unit may belong to, so that line's
    quantity question settles it; a wrong line settled would write a length as a count. The lines named:
    - the item right after it ("50 ft of drop cable", "1 box of breakers"), on the same line of text;
    - the item right before it, with only spaces or punctuation between ("drop cable 50 ft", "breakers, 2 boxes");
    - when no item is right after it: the item before it however far, and every line whose quantity is its number;
    - every line whose item is not in the text word for word: where its unit is cannot be told.
    A movement of a single line whose item is not found names no line, which fits it. A line in `exact` is left
    out: the measure states its quantity in its part's own unit (`_own_measure`)."""
    start, number = found.start(), re.match(_NUMBER, found.group(0))
    if number is None and (number := re.search(rf"{_NUMBER}(?=\s*$)", text[:start])):
        start = number.start()  # "1 box": the container's number is part of it
    after: dict[int, int] = {}
    before: dict[int, int] = {}
    named = set()
    for n, item in enumerate(movement.line_items, start=1):
        places = _occurrences(item.item_text or "", text)
        if not places:
            named.add(n)
        if starts := [m.start() for m in places if m.start() >= found.end()]:
            after[n] = min(starts)
        if ends := [m.end() for m in places if m.end() <= start]:
            before[n] = max(ends)
    right_after = False
    if after:
        nxt = min(after, key=lambda n: (after[n], n))
        named.add(nxt)
        right_after = bool(_RIGHT_AFTER.fullmatch(text[found.end() : after[nxt]]))
    if before:
        last = max(before, key=lambda n: (before[n], -n))
        if not right_after or _RIGHT_BEFORE.fullmatch(text[before[last] : start]):
            named.add(last)
    if not right_after and number:
        with contextlib.suppress(InvalidOperation):
            value = Decimal(number.group(0).replace(",", ""))
            named |= {n for n, item in enumerate(movement.line_items, start=1) if item.quantity == value}
    if len(movement.line_items) == 1 and not after and not before:
        return {_rule(ReasonCode.PACK_UNIT_AMBIGUOUS, detail)}
    return {_rule(ReasonCode.PACK_UNIT_AMBIGUOUS, f"line {n}: {detail}") for n in named - exact}


_NUMBER = r"\d+(?:[.,]\d+)*"
_RIGHT_AFTER = re.compile(r"[ \t]*(?:of[ \t]+(?:the[ \t]+)?)?")
"""What may stand between a unit and the item after it that it belongs to: "50 ft drop cable", "a box of the
breakers". Not a line break: "drop cable 50 ft\nONT 2" is a list whose unit ends the row before."""
_RIGHT_BEFORE = re.compile(r"[\s,:;()-]*")
"""What may stand between an item and a unit after it that may be its own: "drop cable 50 ft", "breakers, 2 boxes"."""


def _in_a_name(movement: CandidateMovement, text: str, found: re.Match[str]) -> bool:
    """A measure inside an item's text is part of its name: "Cat6 patch cables 3 ft". A double quote mark that closes
    a quoted name is not one: 'took 2 "Disc 40" to van 3'. It does when the nearest double quote mark before it opens
    a quote: a word right after it and no digit right before it (after '6"pipe and 10"' the nearest is an inch mark)."""
    if found.group(0)[-1] in '"\N{RIGHT DOUBLE QUOTATION MARK}':
        before = text[: found.start()]
        at = max(before.rfind(mark) for mark in '"\N{LEFT DOUBLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK}')
        if (
            at >= 0
            and before[at] != "\N{RIGHT DOUBLE QUOTATION MARK}"
            and re.match(r"\w", before[at + 1 :])
            and not before[at - 1 : at].isdigit()
        ):
            return True
    measure = re.sub(r"\s", "", found.group(0))
    return any(measure in re.sub(r"\s", "", _folded(item.item_text or "")) for item in movement.line_items)


def _folded(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def _occurrences(phrase: str, text: str) -> list[re.Match[str]]:
    """Where a phrase occurs in the folded text as whole words."""
    phrase = _folded(phrase)
    if len(re.findall(r"\w", phrase)) < 2:
        return []  # "a", "-": too short to be grounded by anything
    return list(re.finditer(rf"(?<!\w){re.escape(phrase)}(?!\w)", text))


def says(text: str, phrase: str) -> bool:
    """Whether `phrase` occurs in `text` as whole words, case and width folded as the validator reads a post."""
    return bool(_occurrences(phrase, _folded(text)))


def _has_words(phrase: str, text: str) -> bool:
    return bool(_occurrences(phrase, text))


def _quantity_words(before: str) -> list[str]:
    """The words right before an item that can belong to its quantity."""
    before = _SEPARATOR.split(before)[-1]
    words = [_bare(word) for word in before.split()[-_QUANTITY_WINDOW:]]
    while words and not all(_quantity_word(word) for word in words):
        words.pop(0)  # "took 2 ONTs 3": only the words right before the item, not the last item's quantity
    return words


def _bare(word: str) -> str:
    """A word without the punctuation around it. A leading dot stays: ".5" is not 5."""
    return word.lstrip("(").rstrip("(),.!")


def _stated(before: str) -> set[Decimal]:
    """The quantities the words right before an item can state. An expression is read as a whole, and one
    this does not know how to read ("one and a half", "half of the 10", "both") states nothing."""
    if re.search(r"(?<!\w)and (a )?half(?!\w)", " ".join(before.split()[-4:])):
        return set()  # "2 and a half dozen": the separator would leave "a half dozen"
    tokens = [_bare(word) for word in _SEPARATOR.split(before)[-1].split()]
    for index in range(len(tokens) - 2, max(len(tokens) - 7, 0), -1):
        part = index - 2 if tokens[index - 1] == "out" and index >= 2 else index - 1  # "2 out of 5"
        if tokens[index] in ("of", "from") and _counts(tokens[part]) and any(map(_is_number, tokens[index + 1 :])):
            # "2 of the 5", "2 of my 5", "2 from the 5": what moved is the part, and the total is not stated.
            prefix = tokens[: part + 1]
            return set() if "half" in _quantity_words(" ".join(prefix)) else _stated(" ".join(prefix))
    words = _quantity_words(before)
    if "half" in words and words not in _HALVES:
        return set()
    found = set()
    for index, word in enumerate(words):
        if _AMBIGUOUS_NUMBER.match(word):
            continue
        if _compound(words, index):
            continue  # "2 dozen", "twenty five": neither word alone states the quantity
        if word == "half" and "dozen" in words:
            continue  # "half a dozen" is 6, never 0.5
        if (number := _NUMBER_TOKEN.match(word)) and not (number.group(2) or "").startswith("-"):  # "12-ft": a size
            with contextlib.suppress(InvalidOperation):
                found.add(Decimal(number.group(1).replace(",", "")))
        if word in NUMBER_WORDS:
            found.add(Decimal(NUMBER_WORDS[word]))
        if word in _WORD_QUANTITIES:
            halved = word == "dozen" and "half" in words[max(0, index - 2) : index]  # "half a dozen"
            found.add(_WORD_QUANTITIES[word] / 2 if halved else _WORD_QUANTITIES[word])
    if _one_container(words):
        found.add(Decimal(1))
    return found


def _counts(word: str) -> bool:
    """A word that says how many, in any form: "2", "2nd", "two", "half", "both", "couple"."""
    return (
        any(character.isdigit() for character in word)
        or word in NUMBER_WORDS
        or word in _WORD_QUANTITIES
        or word in _SCALES
        or word in _WHOLE
    )


def _stated_units(before: str) -> set[str]:
    """The units the words right before an item state, in the form a line item carries: "ft", "m", "box"."""
    found = set()
    for word in _quantity_words(before):
        if word in LENGTH_UNITS:
            found.add(LENGTH_UNITS[word])
        elif CONTAINER_WORDS.fullmatch(word):
            found.add(_unit(word) or word)
    return found


def _same_unit(unit: str | None, stated: set[str]) -> bool:
    """The candidate's unit is the one the text states: none for none, and neither swapped nor left out."""
    unit = _unit(unit)
    if unit is None or unit in _EACH:
        return not stated
    return stated == {LENGTH_UNITS.get(unit, unit)}  # "5 ft of 2 m": two units state neither


def _is_number(word: str) -> bool:
    return bool(_NUMBER_TOKEN.match(word)) or word in NUMBER_WORDS or bool(_FRACTION.match(word))


def _quantity_word(word: str) -> bool:
    return (
        _is_number(word)
        or word in _SCALES
        or word in _WORD_QUANTITIES
        or word in _ONE
        or word in _WHOLE
        or word in _FILLER
        or bool(CONTAINER_WORDS.fullmatch(word))
        or bool(_AMBIGUOUS_NUMBER.match(word))
    )


def _compound(words: list[str], index: int) -> bool:
    """A number that is part of a longer one: next to a scale word or a fraction, a number word next to another,
    or either side of "x"."""
    word, near = words[index], words[max(0, index - 1) : index] + words[index + 1 : index + 2]
    if word in _SCALES:
        return any(_is_number(other) for other in near)
    if _is_number(word):
        times = any(
            0 <= index + 2 * step < len(words) and words[index + step] == "x" and _is_number(words[index + 2 * step])
            for step in (-1, 1)
        )  # "2 x 3"
        return times or any(
            other in _SCALES or _FRACTION.match(other) or (_is_number(other) and NUMBER_WORDS.keys() & {word, other})
            for other in near
        )
    return False


def _one_container(words: list[str]) -> bool:
    """An article right before the item ("the radio"), or before its container ("a 12-piece box of", "box of 100").
    Not after a number: "2 of the ONTs" states 2."""
    words = [word.strip("(),.!") for word in words]
    container = False
    while words:
        last = words[-1]
        if CONTAINER_WORDS.fullmatch(last):
            container = True
        elif not (last == "of" or re.fullmatch(r"\d+-\w+", last) or (last.isdigit() and words[-2:-1] == ["of"])):
            break
        words.pop()
    if any(_counts(word) for word in words[:-1]):
        return False  # also "both of the", "half the", "all the": not one
    return (bool(words) and words[-1] in _ONE) or (container and not words)


def _grounding(movement: CandidateMovement, text: str) -> set[ReviewReason]:
    """Each item is in the text as whole words, and its quantity is stated right before it."""
    places = [_folded(place) for place in (movement.source_text, movement.destination_text) if place]
    reasons = set()
    used: set[int] = set()
    for n, item in enumerate(movement.line_items, start=1):
        if not item.item_text:
            continue  # items_missing covers it
        found = [m for m in _occurrences(item.item_text, text) if m.start() not in used]
        if not found or any(_folded(item.item_text) in place for place in places):
            reasons.add(_rule(ReasonCode.ITEM_NOT_IN_TEXT, f"line {n}"))
        first = _folded(item.item_text).split()[0]
        grounded = [m for m in found if item.quantity is None or item.quantity in _stated(text[: m.start()])]
        if grounded:
            used.add(grounded[0].start())  # one mention grounds one line: "2 ONTs" is not two lines of 2
        if found and not any(_same_unit(item.unit, _stated_units(text[: m.start()])) for m in grounded[:1] or found):
            reasons.add(_rule(ReasonCode.QUANTITY_NOT_IN_TEXT, f"line {n}: the unit is not the one in the text"))
        if item.quantity is not None and (
            first in _SCALES
            or first in NUMBER_WORDS
            or _FRACTION.match(first)
            or not grounded
            or (first[:1].isdigit() and any(_after_times(text[: m.start()]) for m in found))
        ):
            reasons.add(_rule(ReasonCode.QUANTITY_NOT_IN_TEXT, f"line {n}"))
    return reasons


def _after_times(before: str) -> bool:
    """ "2 x " right before an item that opens with a number: "2 x 3 ONTs" is not 2 of "3 ONTs"."""
    return bool(re.search(r"(?<!\w)(\d+\s*x|x)\s*$", before))


def _places(movement: CandidateMovement, text: str) -> set[ReviewReason]:
    """Each location is in the text, on the side the movement puts it: "from X", "to X", or "X:" for a heading.

    A place that opens the post without a colon ("van 3 got 2 breakers") is not on either side.
    """
    reasons = set()
    for side, place, pattern in (
        ("source", movement.source_text, rf"{_FROM}\s+(the\s+)?$"),
        ("destination", movement.destination_text, rf"{_TO}\s+(the\s+)?$"),
    ):
        if place is None:
            continue
        found = _occurrences(place, text)
        if not found:
            reasons.add(_rule(ReasonCode.LOCATION_NOT_IN_TEXT, side))
            continue
        after_heading = side == "destination" and any(re.match(r"\s*(count\s*)?:", text[m.end() :]) for m in found)
        if not after_heading and not any(re.search(pattern, text[: m.start()].rstrip() + " ") for m in found):
            reasons.add(_rule(ReasonCode.MOVEMENT_UNCLEAR, f"the text does not put the {side} on that side"))
    return reasons


_CLAUSE_END = re.compile(r"\n|[.!?;](?=\s|$|[^\W\d])|(?<!\d),|,(?!\d)|[&+]|\b(and|then|plus|also)\b")
"""Where a clause ends: a message or line, a sentence, a comma that is not in a number, "and", "&", "plus"."""
_NAMES_A_PLACE = {
    "source": re.compile(rf"{_FROM}\s+\w"),
    "destination": re.compile(rf"(?<!\w)(to|into|onto)\s+\w|({_ARROW})\s*\w"),
}
"""Words that open a place. Not "on", "in", "at", "for" or "up": "picked up 2", "in the back", "for the Smith job"."""
_EMOJI = re.compile(r":[\w+-]+:")
"""A Slack emoji code, such as ":truck:"."""
_PURPOSE = re.compile(r"\bfor\s+(\S+\s+){0,3}$", re.IGNORECASE)
"""The words before a job that says why, not where: "for the Smith job", "for the Oak St site"."""
_A_NUMBER = re.compile(rf"(?<!\w)(\d+|{'|'.join(NUMBER_WORDS)})(?![^\W\d])", re.IGNORECASE)


def _one_clause(movement: CandidateMovement, text: str) -> set[ReviewReason]:
    """A movement whose fields no single clause holds all of may mix two movements: "took 2 ONTs from shelf A to
    van 3" and "took 4 ONTs from shelf B to van 4" read as 4 ONTs from shelf A to van 3 (#200). It is doubted when a
    clause among those it draws on, from the first to the last, has a rival: a place on a side whose place the
    movement takes from another clause, or a number that is neither a line's quantity nor in a field's words
    ("van 3 took 4 ONTs too"), or a vehicle, job or site that is not one of its places ("van 4 got the drops", "the
    Smith job got the drops"; not "for the Smith job", which says why). An item counts
    where its quantity is stated. A list under a heading ("Van 3 from shelf A:" then a line per item) has no rival
    and passes, as does another movement after the last clause."""
    span = _span(movement, text)
    if span is None:
        return set()
    starts, lines, places, sides, first, last = span

    def clauses(found: list[re.Match[str]]) -> set[int]:
        return _clauses(starts, found)

    # A line's own words run from where its quantity may start, never before its clause, to the end of its item.
    own = [
        (
            max(m.start() - len(_SEPARATOR.split(text[: m.start()])[-1]), max(s for s in starts if s <= m.start())),
            m.end(),
        )
        for found in lines
        for m in found
    ]
    names = [
        (m.start(), m.end())
        for item in movement.line_items
        if item.item_text
        for m in _occurrences(item.item_text, text)
    ]
    names += [(m.start(), m.end()) for found in places.values() for m in found]
    own += names
    numbers = [m for m in _A_NUMBER.finditer(text) if not any(start <= m.start() < end for start, end in own)]
    names += [(m.start(), m.end()) for m in _EMOJI.finditer(text)]  # ":truck:" is no place
    # A vehicle needs no "to" or number to be a place ("van 3 got the drops"); one of this movement's places is not.
    places_named = [
        m for m in VEHICLE_WORDS.finditer(text) if not any(start <= m.start() < end for start, end in names)
    ]
    # So does a job or a site ("the Smith job got the drops"), unless it says why ("for the Smith job").
    places_named += [
        m
        for m in SITE_WORDS.finditer(text)
        if not any(start <= m.start() < end for start, end in names)
        and not _PURPOSE.search(text[max(s for s in starts if s <= m.start()) : m.start()])
    ]
    rival = (
        any(
            first <= n <= last and n not in mine
            for side, mine in sides.items()
            for n in clauses(list(_NAMES_A_PLACE[side].finditer(text)))
        )
        or any(first <= n <= last for n in clauses(numbers))
        or any(first <= n <= last and n not in set().union(*sides.values()) for n in clauses(places_named))
    )
    return {_rule(ReasonCode.MOVEMENT_UNCLEAR, DETAIL_MIXED)} if rival else set()


class _Span(NamedTuple):
    """The clauses a movement draws on, when no single one holds all of its fields."""

    starts: list[int]
    lines: list[list[re.Match[str]]]
    places: dict[str, list[re.Match[str]]]
    sides: dict[str, set[int]]
    first: int
    last: int


def _clauses(starts: list[int], found: list[re.Match[str]]) -> set[int]:
    return {sum(start <= m.start() for start in starts) - 1 for m in found}


def _span(movement: CandidateMovement, text: str) -> _Span | None:
    """Where the movement's fields are in the folded text, and the first and last clause they are in; None when
    one clause holds them all, or none of them is in the text."""
    starts = [0] + [m.end() for m in _CLAUSE_END.finditer(text)]
    lines = [
        [
            m
            for m in _occurrences(item.item_text, text)
            if item.quantity is None or item.quantity in _stated(text[: m.start()])
        ]
        for item in movement.line_items
        if item.item_text
    ]
    places = {
        side: _occurrences(place, text)
        for side, place in (("source", movement.source_text), ("destination", movement.destination_text))
        if place
    }
    sides = {side: _clauses(starts, found) for side, found in places.items()}
    fields = [found for found in (*(_clauses(starts, found) for found in lines), *sides.values()) if found]
    if not fields or set.intersection(*fields):
        return None  # a field not in the text is doubted
    return _Span(starts, lines, places, sides, min(set.union(*fields)), max(set.union(*fields)))


def clauses_drawn_on(movement: CandidateMovement, text: str) -> tuple[str, ...]:
    """The clauses, from the first to the last, of a movement whose fields no single clause holds all of (as
    `_one_clause`), with the movement's own items and places, and anything after "for", blanked out by commas; none
    when one clause holds them all. The caller looks in them for a rival place only the catalog knows: "took 2 ONTs
    to van 3, Riverside Apartments got 4 drops" read as 2 ONTs and 4 drops to van 3 (T6)."""
    text = _folded(text)
    span = _span(movement, text)
    if span is None:
        return ()
    own = [m for item in movement.line_items if item.item_text for m in _occurrences(item.item_text, text)]
    own += [m for found in span.places.values() for m in found]
    blanked = list(text)
    for m in own:
        blanked[m.start() : m.end()] = "," * (m.end() - m.start())
    ends = [*span.starts[1:], len(text)]
    drawn_on = ("".join(blanked[span.starts[n] : ends[n]]) for n in range(span.first, span.last + 1))
    # A place after "for" says why, not where, as in `_one_clause`: "to van 3 for Riverside Apartments".
    return tuple(re.sub(r"(?<!\w)for(?!\w).*", lambda m: "," * len(m.group(0)), clause) for clause in drawn_on)


def _contradictions(movement: CandidateMovement) -> set[ReviewReason]:
    kind = movement.movement_type
    source, destination = movement.source_text, movement.destination_text
    onto_vehicle = destination is not None and VEHICLE_WORDS.search(destination) is not None
    found = []
    if kind in (MovementType.RETURN, MovementType.TRANSFER) and onto_vehicle:
        found.append(f"a {kind.value} whose destination is a vehicle")
    if kind is MovementType.TAKE_TO_VEHICLE and destination is not None and not onto_vehicle:
        found.append("take_to_vehicle whose destination names no vehicle")
    if kind is MovementType.RECEIVE and source is not None:
        found.append("a receive has no source location")
    if kind is not MovementType.COUNT and any(item.quantity == 0 for item in movement.line_items):
        found.append("zero quantity outside a count")
    if source is not None and destination is not None and _squashed(source) == _squashed(destination):
        found.append("source and destination are the same")
    return {_rule(ReasonCode.CONTRADICTION, detail) for detail in found}


def _squashed(place: str) -> str:
    """Only letters and digits: "Van 3" and "van3" are the same place."""
    return re.sub(r"\W|_", "", _folded(place))
