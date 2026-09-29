"""Deterministic matching of item and location text to InvenTree records (design section 10).

The rules run in order, and the first rule with any hit decides:

1. exact: the text equals a part's IPN or name, or a location's path or name;
2. normalized: the same after folding case, punctuation, possessives and plural endings;
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

from stockthread.core.model import Match, MatchRule, MatchStatus

TYPO_RATIO = 0.8
"""Two words of 4 letters or more count as the same word at this `difflib` similarity (for example "ondrl")."""
MY_VEHICLE = re.compile(r"^my (truck|van|vehicle|car|trailer)$")
"""Phrases that mean the poster's own vehicle."""
_WORD = re.compile(r"[a-z0-9]+")
_LETTERS_DIGITS = re.compile(r"[a-z]+|[0-9]+")


@dataclass(frozen=True)
class PartRecord:
    pk: int
    ipn: str
    name: str
    keywords: str = ""
    active: bool = True
    template: bool = False
    virtual: bool = False


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


@dataclass(frozen=True)
class Catalog:
    parts: tuple[PartRecord, ...]
    locations: tuple[LocationRecord, ...]
    part_aliases: tuple[Alias, ...] = ()
    location_aliases: tuple[Alias, ...] = ()
    vehicles: dict[str, int] = field(default_factory=dict)
    """Slack user id -> the location of the vehicle assigned to that user."""


def words(text: str) -> list[str]:
    """Lower-case words without punctuation or possessives: "Sam's J-Arm" -> ["sam", "j", "arm"]."""
    folded = unicodedata.normalize("NFKC", text).lower()
    folded = re.sub("['\N{RIGHT SINGLE QUOTATION MARK}]s\\b", "", folded)
    return _WORD.findall(folded)


def _forms(word: str, capitals: bool = False) -> set[str]:
    """The word and its possible singular forms. Numbers, capitalized words ("UPS") and "-ss" words have none."""
    forms = {word}
    if capitals or word.isdigit() or word.endswith("ss") or len(word) < 3:
        return forms
    if word.endswith("ies"):
        forms.add(word[:-3] + "y")
    if word.endswith("es"):
        forms.add(word[:-2])
    if word.endswith("s"):
        forms.add(word[:-1])
    return forms


def _text_words(text: str) -> list[set[str]]:
    capitals = {w.lower() for w in re.findall(r"[A-Za-z]+", text) if len(w) > 1 and w.isupper()}
    return [_forms(w, w in capitals) for w in words(text)]


def _normal(text: str) -> tuple[str, ...]:
    """A canonical form for rules 2 and 3: every word reduced to its shortest singular form."""
    return tuple(min(forms, key=lambda form: (len(form), form)) for forms in _text_words(text))


def _vocabulary(texts: Iterable[str]) -> set[str]:
    vocabulary: set[str] = set()
    for text in texts:
        for forms in _text_words(text):
            vocabulary |= forms
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
    if not stripped:
        return Match(MatchStatus.UNMATCHED)
    exact = (pk for pk, names in records.items() if stripped in names)
    if decided := _decide(exact, MatchRule.EXACT):
        return decided
    normal = _normal(stripped)
    normalized = (pk for pk, names in records.items() if normal in {_normal(name) for name in names})
    if decided := _decide(normalized, MatchRule.NORMALIZED):
        return decided
    by_alias = (alias.record for alias in aliases if alias.record in records and _normal(alias.text) == normal)
    if decided := _decide(by_alias, MatchRule.ALIAS):
        return decided
    wanted = _text_words(stripped)
    for rule, typos in ((MatchRule.WORDS, False), (MatchRule.FUZZY, True)):
        hits = (pk for pk in records if all(_found(forms, vocabulary(pk), typos) for forms in wanted))
        if decided := _decide(hits, rule):
            return decided
    return Match(MatchStatus.UNMATCHED)


def match_part(text: str, catalog: Catalog) -> Match:
    """Match item text to a part. Inactive, template and virtual parts are never a match."""
    parts = {p.pk: p for p in catalog.parts if p.active and not p.template and not p.virtual}
    aliases = {pk: [a.text for a in catalog.part_aliases if a.record == pk] for pk in parts}

    def vocabulary(pk: int) -> set[str]:
        part = parts[pk]
        return _vocabulary([part.name, part.keywords, *aliases[pk]])

    return _match(text, {pk: (p.ipn, p.name) for pk, p in parts.items()}, catalog.part_aliases, vocabulary)


def match_location(text: str, catalog: Catalog, poster: str) -> Match:
    """Match location text to a stock location. `poster` is the Slack user id of the post's author.

    "My truck" is the poster's assigned vehicle, and unmatched when there is none. Aliases of other users
    do not apply. Structural locations are never a match, because they cannot hold stock.
    """
    locations = {loc.pk: loc for loc in catalog.locations if not loc.structural}
    if MY_VEHICLE.match(" ".join(words(text))):
        vehicle = catalog.vehicles.get(poster)
        if vehicle in locations:
            return Match(MatchStatus.MATCHED, rule=MatchRule.ALIAS, record=vehicle)
        return Match(MatchStatus.UNMATCHED)
    aliases = [a for a in catalog.location_aliases if a.user in (None, poster)]
    alias_texts = {pk: [a.text for a in aliases if a.record == pk] for pk in locations}

    def vocabulary(pk: int) -> set[str]:
        location = locations[pk]
        return _vocabulary([location.path.replace("/", " "), location.description, *alias_texts[pk]])

    names = {pk: (loc.path, loc.name) for pk, loc in locations.items()}
    return _match(text, names, aliases, vocabulary)
