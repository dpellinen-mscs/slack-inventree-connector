# SPDX-License-Identifier: MIT
"""What a plan an approver confirmed teaches (ADR 0012, narrowed by ADR 0021): each item and place text of it
that the whole-word and fuzzy rules fit to that one record alone becomes an alias of it. A pick among several records
the text fits teaches nothing. The worker stores them once every request of the plan is verified (`Worker.work_due`);
a plan the policy approved, a dry run and anything a model suggested teach nothing. A name a person typed in the
thread for a line or a place teaches the post's own text for it, not the typed name, under the same rules.

The gate writes no plan with a bare whole-word or fuzzy match (`Match.can_authorize_a_write`): the thread asks to
confirm it first, and the poster's "yes" or pick makes it an answer match. Those rules stay listed so a plan that ever
holds one is taught the same way.

A lesson needs all of these:
- The text is the poster's own words: it occurs in a message the poster wrote, as the plan's dry run read it. An
  item or place text the model wrote, or read from a photo, is never learned (only confirmed human corrections
  become aliases).
- It has a word with a letter, and no word whose meaning depends on who posted it or when ("my van", "this cable",
  "the usual ones"): everyone's posts use an alias, and there are no per-user aliases (ADR 0021).
- Its record is the one record the whole-word and fuzzy rules fit it to now, with or without its articles. A text
  that fits other records too ("cable" picked among three cables) would make every later post that says it a write
  to the record picked once, and a text that fits no record ("hdmi cable" corrected to a DisplayPort cable, "the
  regular") names nothing the catalog backs: both are asked again instead. A place named only by a vehicle word
  ("the van") is never learned either.
- The same plan decided it for one record only."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from stockthread.core.matching import Catalog, location_rivals, part_rivals, same_words, words
from stockthread.core.model import Match, MatchRule, MatchStatus
from stockthread.core.validate import says
from stockthread.slack.replies import MovementView
from stockthread.store import AliasKind

TAUGHT_BY = frozenset({MatchRule.WORDS, MatchRule.FUZZY, MatchRule.ANSWER})
"""The rules whose match a confirmation turns into an alias. Exact, normalized and alias matches need none."""
DEICTIC = frozenset(
    {
        *("i", "me", "my", "mine", "we", "us", "our", "ours", "you", "your", "yours"),
        *("he", "him", "his", "she", "her", "hers", "they", "them", "their", "theirs"),
        *("this", "that", "these", "those", "it", "here", "there", "usual", "same", "other", "another"),
        *("one", "ones", "thing", "things", "stuff", "some", "few", "couple", "spare", "spares", "old", "new", "last"),
    }
)
"""Words that name a different thing for another poster or on another day."""
ARTICLES = frozenset({"the", "a", "an"})
VEHICLE_WORDS = frozenset(
    {"truck", "trucks", "van", "vans", "vehicle", "vehicles", "car", "cars", "trailer", "trailers"}
)
"""A place named by one of these alone ("the van") names another vehicle once a second one exists: never learned."""


@dataclass(frozen=True)
class Lesson:
    """One alias to store: `text` names `record`. `rivals`: the records the whole-word and fuzzy rules fit the text to
    now (`Store.learn_alias`). `movement`: the number of the movement whose request it is learned from, from 1."""

    kind: AliasKind
    text: str
    record: int
    rivals: frozenset[int]
    movement: int


def lessons(views: Sequence[MovementView], catalog: Catalog, poster: str, said: Sequence[str]) -> list[Lesson]:
    """The aliases a confirmed and verified plan of `views`, posted by `poster` in messages whose texts are `said`,
    teaches, read against `catalog`: parts in the post's order, then places. A text is learned once, from the first
    movement that decided it."""
    found: list[tuple[AliasKind, str, Match | None, str | None, int]] = []
    for number, view in enumerate(views, start=1):
        movement = view.movement
        for line, (item, part) in enumerate(zip(movement.line_items, view.items, strict=True)):
            found.append((AliasKind.PART, item.item_text or "", part, view.named.get(line), number))
    for number, view in enumerate(views, start=1):
        movement = view.movement
        for side, text, place in (
            ("source", movement.source_text, view.source),
            ("destination", movement.destination_text, view.destination),
        ):
            found.append((AliasKind.LOCATION, text or "", place, view.named.get(side), number))
    taught: list[tuple[AliasKind, str, int, int]] = []
    clashing: set[int] = set()
    for kind, text, match, named, number in found:
        record = _taught(text, match, named)
        if record is None:
            continue
        # The store keeps one alias per text as the matcher reads it ("patch cables" is "patch cable").
        same = [i for i, (k, known, _, _) in enumerate(taught) if k is kind and same_words(known, text)]
        if not same:
            taught.append((kind, text.strip(), record, number))
        elif taught[same[0]][2] != record:
            clashing.add(same[0])
    result = []
    for i, (kind, text, record, number) in enumerate(taught):
        rivals = _rivals(kind, text, catalog, poster)
        # "the truck" fits no record by its words, but "truck" fits every truck: the articles do not count.
        plain = " ".join(word for word in words(text) if word not in ARTICLES)
        if i in clashing or rivals | _rivals(kind, plain, catalog, poster) != {record}:
            continue
        if kind is AliasKind.LOCATION and set(words(plain)) <= VEHICLE_WORDS:
            continue
        if not any(says(message, text) for message in said):
            continue
        result.append(Lesson(kind, text, record, rivals, number))
    return result


def _taught(text: str, match: Match | None, named: str | None) -> int | None:
    """The record `text` becomes an alias of, if any. `named`: the name a person typed for it in the thread, which
    the match was made from; a typed name that is not the post's own text corrects it, whatever rule matched the
    name."""
    said = words(text)
    if match is None or match.status is not MatchStatus.MATCHED:
        return None
    corrected = named is not None and not same_words(named, text)
    if match.rule not in TAUGHT_BY and not corrected:
        return None
    if not any(any(c.isalpha() for c in word) for word in said) or DEICTIC.intersection(said):
        return None
    return match.record


def _rivals(kind: AliasKind, text: str, catalog: Catalog, poster: str) -> frozenset[int]:
    if kind is AliasKind.PART:
        return part_rivals(text, catalog, poster)
    return location_rivals(text, catalog, poster)
