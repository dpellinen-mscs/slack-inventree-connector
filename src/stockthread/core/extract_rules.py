"""The rule-based extractor (design section 9): no network, no key, and no guessing.

It reads one message of the common shapes, "took 2 Disc 40 to my truck" or "2 ONTs on truck 1",
and declines everything else: photos, threads, sibling messages, lists, several movements in one
post, a third person as the actor, and any wording it does not know. A declined post is not "no
movement"; the pipeline must not read it as one (`Extraction.declined`).

Some posts are clearly not movements: questions, requests, plans, conditions and negations. Those
give an extraction with no movements. Post flags (instructions to the connector, customer details)
are raised on every post, declined or not, and a sentence that holds instructions is never read.

Flags that follow from the fields (eval/README.md, "Flags") are computed here by rule.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from stockthread.core.model import CandidateMovement, Extraction, Flag, LineItem, MessageInput, MovementType, PostFlag

EXTRACTOR = "rules-0.1"

_SUSPICIOUS = re.compile(
    r"\b(ignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|rules)|system\s*:|without review"
    r"|writes? (are|is) (now )?(on|enabled)|mark (this|it) as (confirmed|approved)|you are now|disregard)",
    re.IGNORECASE,
)
_SENSITIVE = re.compile(
    r"\b(gate code|door code|lock ?box|alarm code|pass(word|code)|pin code|customer (cell|phone|number)"
    r"|phone|cell)\b|\b\d{3}[-.]\d{4}\b",
    re.IGNORECASE,
)
_NOT_A_MOVEMENT = re.compile(
    r"\?|\b(can|could|would) (some|any)one\b|\b(can|could) you\b|\bneeds?\b|\bplease\b|\bshould\b|\bset \w+ aside\b"
    r"|\b(going to|gonna|i'll)\b|^if\b|, if\b"
    r"|\b(i|we|he|she|they|you|someone|who) will\b|\bwill (be|take|grab|bring|need|get|pick|drop|return|load|use)\b"
    r"|\b(not|never|nothing|no longer|didn't|did not|wasn't|haven't|scratch that|actually no|put (them|it) back)\b",
    re.IGNORECASE,
)
_LATER = r"tomorrow|tmrw?|2mo?r+ow|tonight|tonite|later today|next week"
"""A time ahead. Never read as "not a movement": "grabbed 3 ONTs for the job tomorrow" and "picked up 3 ONTs for
tomorrow" are takes that happened, and no list of past-tense verbs is complete, so the post is declined."""
_SLACK_MARKUP = re.compile(r"<[@#!][^>]*>|:[a-z0-9_+-]+:")
_TICKET = re.compile(r"^(ticket|job|wo|work order)\s*#?\d+\s*[:,-]\s*", re.IGNORECASE)
_TIME = re.compile(r"\bat \d{1,2}(:\d{2})?\s*(am|pm)\b", re.IGNORECASE)
_SERIAL = re.compile(
    r"(?<![\w/-])(?:s/?n\b\s*[:#]?|serial\s*[:#]|serial\s+(?=\d))\s*((?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{3,})\b",
    re.IGNORECASE,
)
"""A serial after a label: "SN SR5-000456", "s/n: 123-AB", "serial # AB12", "serial 1234". A serial holds a digit,
and the word "serial" needs a ":", "#" or a digit after it: "USB serial adapters" and "snap-in" hold none."""


class _Verb(Enum):
    TAKE = "take"
    RETURN = "return"
    RECEIVE = "receive"
    CONSUME = "consume"


_VERBS = {
    "took": _Verb.TAKE,
    "grabbed": _Verb.TAKE,
    "pulled": _Verb.TAKE,
    "put": _Verb.TAKE,
    "loaded": _Verb.TAKE,
    "moved": _Verb.TAKE,
    "dropped off": _Verb.TAKE,
    "returned": _Verb.RETURN,
    "brought back": _Verb.RETURN,
    "received": _Verb.RECEIVE,
    "rcvd": _Verb.RECEIVE,
    "installed": _Verb.CONSUME,
    "used": _Verb.CONSUME,
}
_OTHER_VERBS = (
    r"took|grabbed|pulled|put|loaded|moved|dropped|brought|returned|received|rcvd|installed|used|swapped|replaced|gave"
)
"""Any of these after the first verb means a second movement, which this extractor declines."""

_PREPOSITIONS = re.compile(r"(?:^|\s)(from|off|out of|to|into|onto|on|in|at|for|→|->)(?=\s|$)", re.IGNORECASE)
_SOURCE_WORDS = {"from", "off", "out of"}

_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}  # fmt: skip
_EACH = {"each", "ea", "pc", "pcs", "piece", "pieces", "unit", "units", "x"}
_CONTAINERS = {"box", "bag", "pack", "case", "roll", "spool", "bundle", "carton", "pallet"}
_LENGTHS = {"ft": "ft", "feet": "ft", "foot": "ft", "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m"}
_NUMBER = r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
"""A number as written: "1,500" is fifteen hundred (a comma between digit groups is not a list comma)."""
_LIST_COMMA = r"(?<!\d),|,(?!\d)"
"""A comma between digits never separates items: "1,500" is one number, and "1,5" is declined."""
_MULTIPLIER = re.compile(
    rf"(?i)\s*((dozen|doz|dz|half|score|gross|hundreds?|thousands?|millions?|grand|k|{'|'.join(_NUMBERS)})\b|\d*\s*[/\u2044])"
)
"""A number word or a fraction after a quantity: "two dozen", "3 doz", "a half dozen", "twenty five", "a hundred",
"10 ½". Never guessed. A digit may follow ("1 16 port switch" reads as 1 of "16 port switch"); a digit before a
plural is a second item ("2 12 ONTs" declines, see `_second_item`)."""

_NOT_STOCK = re.compile(
    r"\b(sec(ond)?|min(ute)?|hour|hr|day|week|month|year|while|time|pto|lunch|break|meeting|call|update|complaint"
    r"|guy|people|folks|customer|ticket|more|floor|trip|mile|left|still|remaining|leftover)s?\b(?!-)",
    re.IGNORECASE,
)
"""Words that name time, people or events, or a report of what is left: "3 hours at the Smith job",
"2 ONTs left on truck 1" are not movements."""
_NOT_A_PLACE = re.compile(
    rf"\b({_LATER}|today|yesterday|morning|afternoon|lol|jk|haha|kidding|maybe|\d{1, 2}(:\d{2})?\s*(am|pm))\b"
    r"|^(find|get|see|check|fix|help|do|make|go)\b",
    re.IGNORECASE,
)
"""Words that make a place text more than a place: "my truck lol jk", "to find the switch", "to 3pm"."""
_VEHICLE_WORDS = re.compile(r"\b(truck|van|trailer|vehicle)s?\b|\bvan\d", re.IGNORECASE)
_WAREHOUSE_WORDS = re.compile(r"\b(receiving|main stock|warehouse|shop|rma|stock ?room|storage)\b", re.IGNORECASE)


class _Kind(Enum):
    VEHICLE = "vehicle"
    WAREHOUSE = "warehouse"
    SITE = "site"


class _Decline(Exception):
    """The text is outside what this extractor reads."""


@dataclass(frozen=True)
class _Quantity:
    value: Decimal | None
    unit: str | None = None
    pack_size: Decimal | None = None
    vague: bool = False


def post_flags(text: str) -> frozenset[PostFlag]:
    """Review reasons about a post as a whole, from its text."""
    flags = set()
    if _SUSPICIOUS.search(text):
        flags.add(PostFlag.SUSPICIOUS_INSTRUCTIONS)
    if _SENSITIVE.search(text):
        flags.add(PostFlag.CONTAINS_SENSITIVE)
    return frozenset(flags)


def extract(messages: Sequence[MessageInput]) -> Extraction:
    """Extract the movements of one post: its messages by people, with bots already filtered out."""
    live = [message for message in messages if not message.deleted]
    flags = post_flags("\n".join(message.text for message in live))
    if not live:
        return Extraction(EXTRACTOR, ())
    if len(live) > 1 or live[0].file_ids:
        return Extraction(EXTRACTOR, (), flags, declined=True)
    text = _clean(live[0].text)
    if len(live[0].text.strip().splitlines()) > 1:
        return Extraction(EXTRACTOR, (), flags, declined=True)  # lists, and a line after the movement
    if _NOT_A_MOVEMENT.search(text):
        return Extraction(EXTRACTOR, (), flags)
    if re.search(rf"(?i)\b({_LATER})\b", text):
        return Extraction(EXTRACTOR, (), flags, declined=True)
    try:
        movement = _movement(text)
    except _Decline:
        return Extraction(EXTRACTOR, (), flags, declined=True)
    return Extraction(EXTRACTOR, (movement,), flags)


def _clean(text: str) -> str:
    """Slack markup, instruction sentences, ticket numbers and times removed; spacing folded."""
    text = unicodedata.normalize("NFKC", text)
    text = _SLACK_MARKUP.sub(" ", text)
    sentences = re.split(r"(?<=[.!])\s+", text.strip())
    text = " ".join(sentence for sentence in sentences if not _SUSPICIOUS.search(sentence))
    text = _TICKET.sub("", text.strip())
    text = _TIME.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip().rstrip(".!")


def _movement(text: str) -> CandidateMovement:
    if re.search(rf"(?i)\b({_OTHER_VERBS})\b.*\b({_OTHER_VERBS})\b", text):
        raise _Decline  # a second movement
    if found := re.fullmatch(r"(?i)(.*?)\b(?:stock ?)?count\s*:\s*(.+)", text):
        return _count(found.group(1), found.group(2))
    if re.search(r"(?i)\bcount", text):
        raise _Decline  # a count needs "count:"; it sets an absolute quantity, so it is never guessed
    if found := re.fullmatch(r"(?i)(?:on\s+)?([^:]+?)\s*:\s*(.+)", text):
        return _load(found.group(1), found.group(2))  # "Truck 1: 2 Disc 40" is a load
    if ":" in text:
        raise _Decline
    verb, rest = _verb(text)
    items_text, source, destination = _places(rest)
    return _candidate(_movement_type(verb, source, destination), items_text, source, destination)


def _count(header: str, rest: str) -> CandidateMovement:
    """ "truck 2 count: 3 Disc 40, 0 ONTs" or "Stock count: 14 SFP modules in main stock"."""
    items_text, source, destination = _places(rest)
    place = header.strip(" ,-")
    if source is not None or (place and destination is not None) or _PREPOSITIONS.search(f" {place} "):
        raise _Decline
    if place:
        destination = _place(place)
    return _candidate(MovementType.COUNT, items_text, None, destination, zero=True)


def _load(header: str, rest: str) -> CandidateMovement:
    """ "Sam's truck: 2 d40s, 1 disc 60". Only a vehicle may head the list, and nothing else may be said."""
    place = _place(header)
    items_text, source, destination = _places(rest)
    if place is None or _kind(place) is not _Kind.VEHICLE or _PREPOSITIONS.search(f" {header} "):
        raise _Decline  # "truck 1 → truck 2: ...", "Received from supplier: ..."
    if source is not None or destination is not None:
        raise _Decline
    return _candidate(MovementType.TAKE_TO_VEHICLE, items_text, None, place)


def _candidate(
    movement_type: MovementType, items_text: str, source: str | None, destination: str | None, *, zero: bool = False
) -> CandidateMovement:
    parts = [part for part in re.split(rf"{_LIST_COMMA}|\s+(?:and|\+|&)\s+", items_text) if part.strip()]
    if len(parts) > 1 and _SERIAL.search(items_text):
        raise _Decline  # "ONTs sn 1234, 5678": a list of serials, or of items? Unread.
    read = [_line_item(part, zero) for part in parts]
    if not read:
        raise _Decline
    items = tuple(item for item, _ in read)
    flags = _flags(movement_type, items, source, destination)
    if any(vague for _, vague in read):
        flags.add(Flag.QUANTITY_VAGUE)
    if any(item.quantity is None and not vague for item, vague in read):
        flags.add(Flag.QUANTITY_MISSING)
    return CandidateMovement(movement_type, items, source, destination, frozenset(flags))


def _verb(text: str) -> tuple[_Verb | None, str]:
    """The movement verb that opens the text, and the rest. No verb is allowed when a quantity opens it."""
    lowered = text.lower()
    for words, verb in _VERBS.items():
        if lowered.startswith(words + " "):
            return verb, text[len(words) + 1 :]
    first = lowered.split(" ", 1)[0]
    counted = re.match(rf"(?:{'|'.join(_CONTAINERS)}) of \d", lowered)  # "box of 100 rj45s"
    if not (re.match(r"\d", first) or first in _NUMBERS or counted):
        raise _Decline  # "Priya took ...", "router installed", "fyi ...", "the job went fine on truck 1"
    if re.search(rf"(?i)\b({_OTHER_VERBS})\b", text):
        raise _Decline  # "2 ONTs installed at smith job": the verb after the item names the movement
    return None, text


def _places(rest: str) -> tuple[str, str | None, str | None]:
    """Split "2 ONTs from main stock to van 3" into the items, the source and the destination."""
    found = list(_PREPOSITIONS.finditer(rest))
    if not found:
        return rest, None, None
    source = destination = None
    for index, match in enumerate(found):
        end = found[index + 1].start() if index + 1 < len(found) else len(rest)
        place = _place(rest[match.end() : end])
        if place is None:
            continue  # "at" with nothing after the removed time
        if match.group(1).lower() in _SOURCE_WORDS:
            if source is not None:
                raise _Decline
            source = place
        else:
            if destination is not None:
                raise _Decline  # "to truck 1 and 3 to van 3"
            destination = place
    return rest[: found[0].start()], source, destination


_SIGN_OFF = re.compile(r"(?i)(thanks|thx|ty|thank you|cheers)?")
"""What may follow a comma after a location without being a second statement."""
_MORE_THAN_A_PLACE = re.compile(
    r"(?i)\s(and|with|w/|but|then|minus|plus|instead|except|or|so|because|since|until|after|before|when"
    r"|sorry|meant|oops|wait|nvm|actually)\s|[()*~]|\s-\s"
)
"""A second clause or a correction inside a place: "truck 1 but 2 were DOA", "truck 1 sorry meant truck 2",
"truck 1 *truck 2", "truck 1 - 1 was DOA"."""
_MORE_THAN_AN_ITEM = re.compile(
    r"(?i)\s(with|w/|plus|but|then|minus|instead|except|or)\s|[()/\u2044]|\bx\s*\d|\b\d+\s+([a-z]\w*)"
)
"""A second item or clause inside an item: "ONTs with 2 SFPs", "ONTs/3 SFPs", "ONTs 3 SFPs", "ONTs x 3"."""
_STATE_WORDS = re.compile(
    r"(?i)\b(broken|bad|dead|doa|damaged|defective|faulty|busted|fried|found|sitting|spare|extra|out|back|missing"
    r"|ordered|backordered|delivered|went|install|needed|swapped|replaced|returned|received)$"
)
"""A last word that says what state an item is in or what happened to it, not what it is: "2 ONTs broken on
truck 1", "2 ONTs DOA at smith job", "put 2 ONTs back on truck 1" (a return, not a take)."""


def _place(text: str) -> str | None:
    """Location text as written, without a leading article. Declines when anything follows a comma."""
    text, *after = re.split(r"[,;.!]", text, maxsplit=1)
    if after and not _SIGN_OFF.fullmatch(after[0].strip(" ,;.!")):
        raise _Decline  # "truck 1, jk", "truck 1, 2 SFPs too": what follows is unread, so nothing is
    text = text.strip()
    text = re.sub(r"(?i)^the\s+", "", text)
    if _MORE_THAN_A_PLACE.search(f" {text} ") or _NOT_A_PLACE.search(text):
        raise _Decline  # "truck 1 but 2 were DOA", "truck 1 (3 were bad)"
    return text or None


def _kind(place: str) -> _Kind:
    if _VEHICLE_WORDS.search(place):
        return _Kind.VEHICLE
    if _WAREHOUSE_WORDS.search(place):
        return _Kind.WAREHOUSE
    return _Kind.SITE


def _movement_type(verb: _Verb | None, source: str | None, destination: str | None) -> MovementType:
    to = _kind(destination) if destination else None
    origin = _kind(source) if source else None
    match verb:
        case _Verb.TAKE | None:
            if to is _Kind.VEHICLE:
                return MovementType.TAKE_TO_VEHICLE
            if to is _Kind.SITE:
                return MovementType.TAKE_TO_SITE
            if verb is _Verb.TAKE and to is _Kind.WAREHOUSE and origin is _Kind.WAREHOUSE:
                return MovementType.TRANSFER
        case _Verb.RETURN:
            if to is _Kind.VEHICLE:
                return MovementType.UNCLEAR  # a return onto a vehicle contradicts itself
            return MovementType.RETURN
        case _Verb.RECEIVE:
            if source is None and to in (None, _Kind.WAREHOUSE):
                return MovementType.RECEIVE
        case _Verb.CONSUME:
            if to in (None, _Kind.SITE):
                return MovementType.CONSUME
    raise _Decline


def _line_item(text: str, zero: bool = False) -> tuple[LineItem, bool]:
    """One item, and whether its quantity is vague. Zero is a quantity only in a count (`zero`)."""
    text = text.strip()
    serials = tuple(_SERIAL.findall(text))
    if (labelled := _SERIAL.search(text)) and _SERIAL.sub("", text[labelled.start() :]).strip():
        raise _Decline  # "sn 1234 5678", "serial 2000 series switch": a serial ends the item, or it is unread
    text = _SERIAL.sub("", text).strip()
    quantity, rest = _quantity(text)
    if _MULTIPLIER.match(rest):
        raise _Decline  # "two dozen", "twenty five", "a hundred feet": the first number is not the quantity
    rest = re.sub(r"(?i)^of\s+", "", rest.strip())
    item = rest.strip(" ,;:-") or None
    if item is not None and (not re.search(r"\w", item) or "," in item or _NOT_STOCK.search(item)):
        raise _Decline
    if item is not None and (_second_item(item) or _STATE_WORDS.search(item) or re.match(r"(?i)lots?\b", item)):
        raise _Decline
    if quantity.value == 0 and not zero:
        raise _Decline
    return LineItem(item, quantity.value, quantity.unit, quantity.pack_size, serials), quantity.vague


def _quantity(text: str) -> tuple[_Quantity, str]:
    """The quantity that opens an item, and the text after it. Declines when there is none."""
    lowered = text.lower()
    patterns: list[tuple[str, _Quantity]] = [
        (r"half a dozen\b", _Quantity(Decimal(6))),
        (r"a dozen\b", _Quantity(Decimal(12))),
        (r"a pair of\b", _Quantity(Decimal(2))),
        (r"a couple( of)?\b", _Quantity(Decimal(2), vague=True)),
        (r"(some|a few|several)\b", _Quantity(None, vague=True)),
        (
            r"(an? (bunch|lot|ton|handful|pile|load|bit|number)|lots|tons|loads|bunches) of\b",
            _Quantity(None, vague=True),
        ),
        (rf"{_NUMBER}\s*-\s*{_NUMBER}\b", _Quantity(None, vague=True)),
    ]
    for pattern, quantity in patterns:
        if found := re.match(pattern, lowered):
            return quantity, text[found.end() :]
    if found := re.match(rf"half an? ({'|'.join(_CONTAINERS)})\b", lowered):
        return _Quantity(Decimal("0.5"), found.group(1)), text[found.end() :]
    if found := re.match(rf"an? {_NUMBER}[- ](?:piece|pc|pack|count|ct) ({'|'.join(_CONTAINERS)})\b", lowered):
        return _Quantity(Decimal(1), found.group(2), _decimal(found.group(1))), text[found.end() :]
    if found := re.match(rf"(?:an? )?({'|'.join(_CONTAINERS)}) of {_NUMBER}\b", lowered):
        return _Quantity(Decimal(1), found.group(1), _decimal(found.group(2))), text[found.end() :]
    if found := re.match(rf"(?:about|around|approx\.?|~)\s*{_NUMBER}\b", lowered):
        return _unit(_Quantity(_decimal(found.group(1)), vague=True), text[found.end() :])
    if re.match(r"\d{1,3}\.\d{3}(?!\d)|\d+\s*x\s*\d", lowered):
        raise _Decline  # "2.000 ONTs" (two, or two thousand?), "2 x 3 ONTs"
    if re.match(rf"{_NUMBER}\s*(\+|-?\s*ish\b|-?\s*something\b|odd\b|or so\b)", lowered):
        raise _Decline  # "5+ ONTs", "5-ish", "20-something", "10 odd": about, not exactly
    if found := re.match(rf"{_NUMBER}\s*x?\b", lowered):
        quantity = _Quantity(_decimal(found.group(1)))
        rest = re.sub(r"(?i)^\s*of the \d+\b", "", text[found.end() :])  # "2 of the 5"
        return _unit(quantity, rest)
    first = lowered.split(" ", 1)[0]
    if first in _NUMBERS:
        return _unit(_Quantity(Decimal(_NUMBERS[first])), text[len(first) :])
    if first in ("a", "an", "the"):
        rest = text[len(first) :]
        if first == "the" and _plural(rest):
            return _Quantity(None), rest  # "the APs": how many is not said
        return _unit(_Quantity(Decimal(1)), rest)
    raise _Decline


def _unit(quantity: _Quantity, rest: str) -> tuple[_Quantity, str]:
    """Read a unit word after a quantity: "5 boxes of", "300 ft of", "12 pieces of"."""
    found = re.match(r"\s*([a-z]+)\b\s*(?:of\b)?", rest, re.IGNORECASE)
    if not found:
        return quantity, rest
    word = found.group(1).lower()
    singular = word[:-2] if word.endswith("xes") else word[:-1] if word.endswith("s") else word
    after = rest[found.end() :]
    if word in _EACH or singular in _EACH:
        return quantity, after
    if singular in _CONTAINERS:
        return _Quantity(quantity.value, singular, quantity.pack_size, quantity.vague), after
    if word in _LENGTHS:
        return _Quantity(quantity.value, _LENGTHS[word], quantity.pack_size, quantity.vague), after
    return quantity, rest


def _second_item(item: str) -> bool:
    """True when an item text holds another item or a clause. A number before a unit or a model word stays:
    "Cat6 patch cables 3 ft", "UPS 600 VA"; a number before a plural does not: "ONTs 3 SFPs"."""
    for found in _MORE_THAN_AN_ITEM.finditer(f" {item} "):
        word = found.group(2)
        if word is None:
            return True
        if word.lower() not in _LENGTHS and _plural(word) and len(word) > 2:
            return True
    return False


def _decimal(number: str) -> Decimal:
    return Decimal(number.replace(",", ""))


def _plural(text: str) -> bool:
    last = re.findall(r"[A-Za-z]+", text)
    return bool(last) and last[-1].lower().endswith("s") and not last[-1].lower().endswith("ss")


def _flags(
    movement_type: MovementType, items: tuple[LineItem, ...], source: str | None, destination: str | None
) -> set[Flag]:
    """The flags that follow from the fields (eval/README.md, "Flags"). Quantity flags are added by the caller."""
    flags = set()
    if source is None and movement_type not in (MovementType.RECEIVE, MovementType.COUNT):
        flags.add(Flag.IMPLIED_SOURCE)
    if destination is None and movement_type is not MovementType.CONSUME:
        flags.add(Flag.IMPLIED_DESTINATION)
    if any(item.item_text is None for item in items):
        flags.add(Flag.ITEMS_MISSING)
    if any(item.unit in _CONTAINERS or item.pack_size is not None for item in items):
        flags.add(Flag.PACK_UNIT_AMBIGUOUS)
    return flags
