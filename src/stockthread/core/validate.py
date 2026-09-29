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
from decimal import Decimal, InvalidOperation

from stockthread.core.model import (
    CandidateMovement,
    MovementType,
    PostFlag,
    ReasonCode,
    ReasonSource,
    ReviewReason,
    reason_for_flag,
)
from stockthread.core.signals import (
    CONTAINER_WORDS,
    CONTAINERS,
    CORRECTION,
    HEDGE,
    LATER,
    MENTION,
    NEGATION,
    NUMBER_WORDS,
    PLAN_OR_CONDITION,
    QUESTION_OR_REQUEST,
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
_FROM = r"(?<!\w)(from|off|out of)"
_TO = r"((?<!\w)(to|into|onto|on|in|at|for|up)|→|->)"
"""Whole words only: "from main shop" must not read as "…in shop"."""


def _rule(code: ReasonCode, detail: str = "") -> ReviewReason:
    return ReviewReason(code, ReasonSource.RULE, detail)


def validate(
    movement: CandidateMovement,
    text: str,
    *,
    max_quantity: int,
    post_flags: frozenset[PostFlag] = frozenset(),
) -> tuple[ReviewReason, ...]:
    """Every review reason for one candidate. `text` is the post's text by people, all messages joined."""
    reasons = {reason_for_flag(flag) for flag in movement.flags}
    reasons |= {reason_for_flag(flag) for flag in post_flags if flag is not PostFlag.CONTAINS_SENSITIVE}
    text = _folded(text)
    reasons |= _from_fields(movement, text)
    reasons |= _from_text(movement, text)
    reasons |= _grounding(movement, text)
    reasons |= _places(movement, text)
    reasons |= _contradictions(movement)
    reasons |= {
        _rule(ReasonCode.QUANTITY_ABOVE_LIMIT, f"line {n}: above policy.max_quantity")
        for n, item in enumerate(movement.line_items, start=1)
        if item.quantity is not None and item.quantity > max_quantity
    }
    return tuple(sorted(reasons, key=lambda reason: (reason.code.value, reason.source.value, reason.detail)))


def _from_fields(movement: CandidateMovement, text: str) -> set[ReviewReason]:
    """The flags that follow from the fields (eval/README.md, "Flags"), and an unclear type."""
    kind = movement.movement_type
    reasons = set()
    if movement.source_text is None and kind not in (MovementType.RECEIVE, MovementType.COUNT):
        reasons.add(_rule(ReasonCode.IMPLIED_SOURCE))
    if movement.destination_text is None and kind is not MovementType.CONSUME:
        reasons.add(_rule(ReasonCode.IMPLIED_DESTINATION))
    if not movement.line_items or any(not (item.item_text or "").strip() for item in movement.line_items):
        reasons.add(_rule(ReasonCode.ITEMS_MISSING))
    if any(item.quantity is None for item in movement.line_items):
        reasons.add(_rule(ReasonCode.QUANTITY_MISSING, "no number"))
    if any(_unit(item.unit) in CONTAINERS or item.pack_size is not None for item in movement.line_items):
        reasons.add(_rule(ReasonCode.PACK_UNIT_AMBIGUOUS))
    for n, item in enumerate(movement.line_items, start=1):
        if item.serials and (item.quantity is None or len(item.serials) != item.quantity):
            reasons.add(_rule(ReasonCode.SERIAL_UNCONFIRMED, f"line {n}: serial count differs from the quantity"))
        if any(not _has_words(serial, text) for serial in item.serials):
            reasons.add(_rule(ReasonCode.SERIAL_UNCONFIRMED, f"line {n}: serial not in the text"))
    if not text.strip():
        reasons.add(_rule(ReasonCode.PHOTO_ONLY, "no text"))
    if kind is MovementType.UNCLEAR:
        reasons.add(_rule(ReasonCode.MOVEMENT_UNCLEAR, "type unclear"))
    return reasons


def _unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    unit = unit.casefold().strip()
    return unit[:-2] if unit.endswith("xes") else unit[:-1] if unit.endswith("s") else unit


def _from_text(movement: CandidateMovement, text: str) -> set[ReviewReason]:
    """Reasons the text gives, whatever the extractor made of it."""
    reasons = set()
    if SUSPICIOUS.search(text):
        reasons.add(_rule(ReasonCode.SUSPICIOUS_INSTRUCTIONS))
    if CONTAINER_WORDS.search(text):
        reasons.add(_rule(ReasonCode.PACK_UNIT_AMBIGUOUS, "container word in the text"))
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


def _folded(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def _occurrences(phrase: str, text: str) -> list[re.Match[str]]:
    """Where a phrase occurs in the folded text as whole words."""
    phrase = _folded(phrase)
    if len(re.findall(r"\w", phrase)) < 2:
        return []  # "a", "-": too short to be grounded by anything
    return list(re.finditer(rf"(?<!\w){re.escape(phrase)}(?!\w)", text))


def _has_words(phrase: str, text: str) -> bool:
    return bool(_occurrences(phrase, text))


def _stated(before: str) -> set[Decimal]:
    """The quantities the words right before an item can state."""
    before = _SEPARATOR.split(before)[-1]
    words = [word.strip("(),.!") for word in before.split()[-_QUANTITY_WINDOW:]]
    while words and not all(_quantity_word(word) for word in words):
        words.pop(0)  # "took 2 ONTs 3": only the words right before the item, not the last item's quantity
    found = set()
    for index, word in enumerate(words):
        if _AMBIGUOUS_NUMBER.match(word):
            continue
        if _compound(words, index):
            continue  # "2 dozen", "twenty five": neither word alone states the quantity
        if number := _NUMBER_TOKEN.match(word):
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


def _is_number(word: str) -> bool:
    return bool(_NUMBER_TOKEN.match(word)) or word in NUMBER_WORDS or bool(_FRACTION.match(word))


def _quantity_word(word: str) -> bool:
    return (
        _is_number(word)
        or word in _SCALES
        or word in _WORD_QUANTITIES
        or word in _ONE
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
    """An article right before the item ("the radio"), or before its container ("a 12-piece box of", "box of 100")."""
    words = [word.strip("(),.!") for word in words]
    container = False
    while words:
        last = words[-1]
        if CONTAINER_WORDS.fullmatch(last):
            container = True
        elif not (last == "of" or re.fullmatch(r"\d+-\w+", last) or (last.isdigit() and words[-2:-1] == ["of"])):
            break
        words.pop()
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
