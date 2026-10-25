"""Deterministic matching of item and location text to InvenTree records (design section 10).

The rules run in order, and the first rule with any hit decides:

1. exact: the text equals a part's IPN or name, or a location's path or name;
2. normalized: the same after folding case, punctuation, possessives and plural endings
   (a trailing "+" is kept: "SFP+" is another model than "SFP");
3. alias: the normalized text equals a confirmed alias;
4. words: every word of the text is one of a record's words (name, keywords, aliases; path
   and description for locations), so "Disc 40" finds "Norvane Disc 40 Access Point";
5. fuzzy: as words, but a word of 4 letters or more may differ by a typo ("ondrl").
   Words with digits must be found exactly, so "truck 7" never becomes "Truck 1".

One hit is `matched`, several are `ambiguous` with every hit as a candidate, and none is
`unmatched`. Matching never falls through to a later rule to find a single winner. The
model never picks the record; this module does, from catalog fields only.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from stockthread.core.model import Match, MatchRule, MatchStatus, MovementType

TYPO_RATIO = 0.8
"""Two words of 4 letters or more count as the same word at this `difflib` similarity (for example "ondrl")."""
MY_VEHICLE = re.compile(r"^my (truck|van|vehicle|car|trailer)$")
"""Phrases that mean the poster's own vehicle, when the word names the kind of vehicle assigned."""
_WORD = re.compile(r"[^\W_]+\+*")
"""Letters and digits of any script, so "10\N{MICRO SIGN}F" never loses its unit, and a trailing "+" ("PoE+")."""
_HISSING = ("ss", "x", "z", "ch", "sh")
"""Endings after which "-es" is a plural ending: "boxes", "switches", "glasses", but not "tapes"."""
_LETTERS_DIGITS = re.compile(r"[^\W\d_]+|\d+")


@dataclass(frozen=True)
class PartRecord:
    pk: int
    ipn: str
    name: str
    keywords: str = ""
    active: bool = True
    template: bool = False
    virtual: bool = False
    units: str = ""
    """The stocking unit in InvenTree ("m", "ft"); "" when the part has none and is counted by the piece."""


@dataclass(frozen=True)
class LocationRecord:
    pk: int
    path: str
    """Names from the root, joined with "/"."""
    description: str = ""
    structural: bool = False

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Alias:
    """A confirmed alias. Aliases with words such as "my truck" belong to one Slack user (design section 10)."""

    text: str
    record: int
    user: str | None = None
    rivals: frozenset[int] | None = None
    """The records the whole-word and fuzzy rules fitted when the alias was confirmed (ADR 0012); a record that fits
    now and is not one of them makes the alias ambiguous. None for an alias an admin typed: never rechecked."""


@dataclass(frozen=True)
class Catalog:
    parts: tuple[PartRecord, ...]
    locations: tuple[LocationRecord, ...]
    part_aliases: tuple[Alias, ...] = ()
    location_aliases: tuple[Alias, ...] = ()
    vehicles: dict[str, int] = field(default_factory=dict)
    """Slack user id -> the location of the vehicle assigned to that user."""
    default_sources: dict[MovementType, int] = field(default_factory=dict)
    """Movement type -> the location `policy.default_source` names for it, for a movement that states no source."""


def words(text: str) -> list[str]:
    """Lower-case words without punctuation or possessives: "Sam's J-Arm, SFP+" -> ["sam", "j", "arm", "sfp+"]."""
    folded = unicodedata.normalize("NFKC", text).lower()
    folded = re.sub("['\N{RIGHT SINGLE QUOTATION MARK}]s\\b", "", folded)
    return _WORD.findall(folded)


def _forms(word: str, *, loose: bool = False) -> set[str]:
    """The word and its possible singular forms.

    "-ss" words and words under 3 letters have none. A word with digits has one only when `loose`:
    "d40s" is probably two "d40", but "40S" may be another model, so only the rules that cannot
    authorize a write (4 and 5) accept it. The same goes for "-es" after anything but a hissing sound
    ("tapes" may be "tap") and for dropping a trailing "+" ("poe+" may be "poe"). The forms may
    still over-reduce ("ups" -> "up"), so rules compare form sets, and a collision is ambiguous.
    """
    forms = {word}
    if loose and word.endswith("+"):
        forms.add(word.rstrip("+"))
    if word.endswith(("ss", "+")) or len(word) < 3 or (not loose and any(char.isdigit() for char in word)):
        return forms
    if word.endswith("ies"):
        forms.add(word[:-3] + "y")
    if word.endswith("es") and (loose or word[:-2].endswith(_HISSING)):
        forms.add(word[:-2])
    if word.endswith("s"):
        forms.add(word[:-1])
    return forms


def _text_words(text: str, *, loose: bool = False) -> list[set[str]]:
    return [_forms(word, loose=loose) for word in words(text)]


def _same(text: list[set[str]], name: list[set[str]]) -> bool:
    """Rules 2 and 3: the same number of words, and each pair of words shares a form."""
    return bool(text) and len(text) == len(name) and all(a & b for a, b in zip(text, name, strict=True))


def _vocabulary(texts: Iterable[str]) -> set[str]:
    vocabulary: set[str] = set()
    for text in texts:
        for forms in _text_words(text):
            vocabulary |= forms | {form.rstrip("+") for form in forms}  # rules 4 and 5 only
    return vocabulary


def _found(forms: set[str], vocabulary: set[str], typos: bool) -> bool:
    """Whether a text word is in the vocabulary. With `typos`, a word without digits may be misspelled."""
    if forms & vocabulary:
        return True
    word = min(forms, key=len)
    pieces = _LETTERS_DIGITS.findall(word)
    if len(pieces) > 1 and all(_forms(piece) & vocabulary for piece in pieces):  # "3ft", "van3"
        return True
    if not typos or any(char.isdigit() for char in word) or len(word) < 4:
        return False
    return any(
        len(known) >= 4 and known.isalpha() and SequenceMatcher(None, word, known).ratio() >= TYPO_RATIO
        for known in vocabulary
    )


def _decide(hits: Iterable[int], rule: MatchRule) -> Match | None:
    found = sorted(set(hits))
    if not found:
        return None
    if len(found) == 1:
        return Match(MatchStatus.MATCHED, rule=rule, record=found[0])
    return Match(MatchStatus.AMBIGUOUS, rule=rule, candidates=tuple(found))


def _match(
    text: str,
    records: Mapping[int, tuple[str, ...]],
    aliases: Iterable[Alias],
    vocabulary: Callable[[int], set[str]],
) -> Match:
    """Rules 1 to 5 over `records` (pk -> the texts that name it exactly)."""
    stripped = text.strip()
    wanted = _text_words(stripped)
    if not wanted:  # punctuation, emoji or nothing: no word to match
        return Match(MatchStatus.UNMATCHED)
    exact = (pk for pk, names in records.items() if stripped in names)
    if decided := _decide(exact, MatchRule.EXACT):
        return decided
    normalized = (pk for pk, names in records.items() if any(_same(wanted, _text_words(name)) for name in names))
    if decided := _decide(normalized, MatchRule.NORMALIZED):
        return decided
    used = [alias for alias in aliases if alias.record in records and _same(wanted, _text_words(alias.text))]
    loose = _text_words(stripped, loose=True)
    if decided := _decide((alias.record for alias in used), MatchRule.ALIAS):
        return _rechecked(decided, used, _fits(loose, records, vocabulary, typos=True))
    for rule, typos in ((MatchRule.WORDS, False), (MatchRule.FUZZY, True)):
        if decided := _decide(_fits(loose, records, vocabulary, typos=typos), rule):
            return decided
    return Match(MatchStatus.UNMATCHED)


def _fits(
    loose: list[set[str]], records: Iterable[int], vocabulary: Callable[[int], set[str]], *, typos: bool
) -> list[int]:
    """Rule 4 (rule 5 with `typos`, which finds every record rule 4 finds and more)."""
    return [pk for pk in records if all(_found(forms, vocabulary(pk), typos) for forms in loose)]


def same_words(a: str, b: str) -> bool:
    """Whether the alias rule takes text `a` for text `b`: the same words, plurals folded ("patch cables" is
    "patch cable"). One alias per such text (`Store.learn_alias`)."""
    return _same(_text_words(a), _text_words(b))


def _rechecked(decided: Match, used: list[Alias], fits: Iterable[int]) -> Match:
    """An alias match is ambiguous when a record fits its text now by the whole-word or fuzzy rule that fitted none
    of the records it was confirmed against (ADR 0012): "Disc 40" is asked again once a "Disc 40 Pro" exists.

    An alias an admin typed (no `rivals`) is not rechecked, and when one is among the aliases that hit, none is: the
    admin vouched for that text. Otherwise the records any hitting alias was confirmed against count as known."""
    if decided.record is None or any(alias.rivals is None for alias in used):
        return decided
    known = {pk for alias in used for pk in alias.rivals or ()}
    new = set(fits) - known - {decided.record}
    if not new:
        return decided
    return Match(MatchStatus.AMBIGUOUS, rule=MatchRule.ALIAS, candidates=tuple(sorted({decided.record, *new})))


def _names(*texts: str) -> tuple[str, ...]:
    return tuple(text for text in texts if text.strip())


def stale_aliases(catalog: Catalog) -> list[Alias]:
    """Aliases that point to a part or location that can no longer match. They are ignored, and reported."""
    parts = {p.pk for p in catalog.parts if p.active and not p.template and not p.virtual}
    locations = {loc.pk for loc in catalog.locations if not loc.structural}
    return [a for a in catalog.part_aliases if a.record not in parts] + [
        a for a in catalog.location_aliases if a.record not in locations
    ]


def _part_rules(
    catalog: Catalog, poster: str | None
) -> tuple[dict[int, tuple[str, ...]], list[Alias], Callable[[int], set[str]]]:
    parts = {p.pk: p for p in catalog.parts if p.active and not p.template and not p.virtual}
    aliases = [a for a in catalog.part_aliases if a.user is None or a.user == poster]
    alias_texts = {pk: [a.text for a in aliases if a.record == pk] for pk in parts}

    def vocabulary(pk: int) -> set[str]:
        part = parts[pk]
        return _vocabulary([part.name, part.keywords, *alias_texts[pk]])

    return {pk: _names(p.ipn, p.name) for pk, p in parts.items()}, aliases, vocabulary


def match_part(text: str, catalog: Catalog, poster: str | None = None) -> Match:
    """Match item text to a part. Inactive, template and virtual parts are never a match.

    Aliases of a user other than `poster` do not apply.
    """
    return _match(text, *_part_rules(catalog, poster))


def part_rivals(text: str, catalog: Catalog, poster: str | None = None) -> frozenset[int]:
    """The parts the whole-word or fuzzy rule fits `text` to now: the `rivals` to store with an alias confirmed now,
    the same set `_rechecked` compares with later."""
    records, _, vocabulary = _part_rules(catalog, poster)
    return frozenset(_fits(_text_words(text.strip(), loose=True), records, vocabulary, typos=True))


def _location_rules(
    catalog: Catalog, poster: str
) -> tuple[dict[int, LocationRecord], dict[int, tuple[str, ...]], list[Alias], Callable[[int], set[str]]]:
    locations = {loc.pk: loc for loc in catalog.locations if not loc.structural}
    aliases = [a for a in catalog.location_aliases if a.user is None or a.user == poster]
    alias_texts = {pk: [a.text for a in aliases if a.record == pk] for pk in locations}

    def vocabulary(pk: int) -> set[str]:
        location = locations[pk]
        return _vocabulary([location.path.replace("/", " "), location.description, *alias_texts[pk]])

    names = {pk: _names(loc.path, loc.name) for pk, loc in locations.items()}
    return locations, names, aliases, vocabulary


def match_location(text: str, catalog: Catalog, poster: str) -> Match:
    """Match location text to a stock location. `poster` is the Slack user id of the post's author.

    "My truck" is the poster's assigned vehicle, and unmatched when there is none. The word must name
    that vehicle ("truck" in its path, description or aliases); otherwise "my trailer" goes through
    the other rules. Aliases of other users do not apply. Structural locations are never a match,
    because they cannot hold stock.
    """
    locations, names, aliases, vocabulary = _location_rules(catalog, poster)
    if mine := MY_VEHICLE.match(" ".join(words(text))):
        vehicle = catalog.vehicles.get(poster)
        if vehicle not in locations:
            return Match(MatchStatus.UNMATCHED)
        if _forms(mine.group(1)) & vocabulary(vehicle):
            return Match(MatchStatus.MATCHED, rule=MatchRule.ALIAS, record=vehicle)

    return _match(text, names, aliases, vocabulary)


def location_rivals(text: str, catalog: Catalog, poster: str) -> frozenset[int]:
    """As `part_rivals`, for stock locations."""
    _, names, _, vocabulary = _location_rules(catalog, poster)
    return frozenset(_fits(_text_words(text.strip(), loose=True), names, vocabulary, typos=True))
