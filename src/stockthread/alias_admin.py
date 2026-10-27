# SPDX-License-Identifier: MIT
"""What an admin sees of the aliases (`stockthread aliases`, ADR 0012): a list, an export, a removal, and an alias
typed by hand (#174).

Alias text and user ids come from Slack, so they are printed quoted, with every character that is not printable
(control characters, DEL, C1 codes, bidi overrides) escaped: none reaches the terminal as itself.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from stockthread.core.matching import Catalog, location_rivals, match_location, match_part, part_rivals, words
from stockthread.core.model import MatchRule, MatchStatus
from stockthread.learning import ARTICLES, DEICTIC, VEHICLE_WORDS
from stockthread.printable import printable
from stockthread.store import AliasEvent, AliasKind, StoredAlias

EXPORT_FORMAT = "stockthread-aliases"
EXPORT_VERSION = 1


def quoted(text: str) -> str:
    """`text` as a JSON string, with every character that is not printable escaped."""
    return printable(json.dumps(text, ensure_ascii=False))


def describe(alias: StoredAlias) -> str:
    """One alias on one line: its id, text, record, whose posts use it, and where it was learned."""
    whose = "everyone's posts" if alias.user is None else f"posts by {quoted(alias.user)} only"
    if alias.learned_from is None:
        source = "typed by an admin"
    else:
        post = alias.learned_from.post
        source = f"learned {alias.updated_at} from post {post.channel_id} {post.ts}"
    return f"{alias.id:>5}  {quoted(alias.text)} -> {alias.kind.value} {alias.record}, {whose}; {source}"


def alias_lines(aliases: Sequence[StoredAlias]) -> list[str]:
    """The lines of `stockthread aliases list`: part aliases, then location aliases."""
    if not aliases:
        return ["no aliases stored: one is learned when a confirmed plan is applied and verified"]
    lines: list[str] = []
    for kind in AliasKind:
        of_kind = [alias for alias in aliases if alias.kind is kind]
        if of_kind:
            lines.append(f"{kind.value} aliases:")
            lines.extend(describe(alias) for alias in of_kind)
    lines.append(
        "`stockthread trail <channel> <ts>` shows a post's trail; `stockthread aliases remove <id>` forgets one"
    )
    return lines


def history_lines(events: Sequence[AliasEvent]) -> list[str]:
    """The lines of `stockthread aliases history`: every audit row of an alias, oldest first, with the record it
    stood for (#192), so the log says which record a text matched to also after the alias is gone."""
    if not events:
        return ["no alias was ever learned, added or removed"]
    lines = []
    for event in events:
        stood = "record not logged" if event.kind is None else f"{event.kind.value} {event.record}"
        post = "" if event.post is None else f", post {event.post.channel_id} {event.post.ts}"
        lines.append(f"{event.at}  {event.actor:<7}  {event.event:<15}  alias {event.alias_id}: {stood}{post}")
    lines.append("`stockthread aliases list` shows each alias's text while it stands")
    return lines


def export(aliases: Sequence[StoredAlias]) -> dict[str, Any]:
    """Every alias as JSON, for a backup or a review: `stockthread aliases export`."""
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "aliases": [
            {
                "id": alias.id,
                "kind": alias.kind.value,
                "text": alias.text,
                "record": alias.record,
                "user": alias.user,
                "rivals": None if alias.rivals is None else sorted(alias.rivals),
                "learned_from": None
                if alias.learned_from is None
                else {
                    "workspace": alias.learned_from.post.workspace_id,
                    "channel": alias.learned_from.post.channel_id,
                    "ts": alias.learned_from.post.ts,
                    "revision": alias.learned_from.revision,
                    "request": alias.learned_from.sequence,
                },
                "created_at": alias.created_at,
                "updated_at": alias.updated_at,
            }
            for alias in aliases
        ],
    }


def refused(kind: AliasKind, text: str) -> str | None:
    """Why `text` cannot be an alias an admin types, or None. The rules a learned alias follows (`learning.lessons`)
    hold: an alias is for everyone's posts, so a word whose meaning depends on who posted it or when ("my van", "this
    cable") is refused; so is a text with no letter ("40") or only articles ("the"), and a place named by vehicle
    words alone ("the van"), which names another vehicle once a second one exists."""
    said = words(text)
    if not any(any(c.isalpha() for c in word) for word in said) or set(said) <= ARTICLES:
        return "it has no word with a letter other than an article"
    named = sorted(DEICTIC.intersection(said))
    if named:
        return f"{quoted(named[0])} means another thing for another poster or on another day"
    if kind is AliasKind.LOCATION and set(said) <= VEHICLE_WORDS | ARTICLES:
        return "a vehicle word alone names another vehicle once there are two; give the vehicle's name or number"
    return None


def unmatchable(catalog: Catalog, kind: AliasKind, record: int) -> str | None:
    """Why the matcher would ignore an alias of `record` (`matching.stale_aliases`), or None when it can be used."""
    if kind is AliasKind.PART:
        part = next((part for part in catalog.parts if part.pk == record), None)
        if part is None or not part.active:
            return "is not an active part in InvenTree (made inactive, or deleted)"
        if part.template or part.virtual:
            return f"is a {'template' if part.template else 'virtual'} part (it holds no stock)"
        return None
    location = next((location for location in catalog.locations if location.pk == record), None)
    if location is None:
        return "is not a stock location in InvenTree"
    if location.structural:
        return "is a structural location (it cannot hold stock)"
    return None


def named(catalog: Catalog, kind: AliasKind, record: int) -> str:
    """The record as an admin knows it: a part's name and IPN, or a location's path."""
    if kind is AliasKind.PART:
        part = next(part for part in catalog.parts if part.pk == record)
        return f"{quoted(part.name)}" + (f" (IPN {quoted(part.ipn)})" if part.ipn else "")
    return quoted(next(location for location in catalog.locations if location.pk == record).path)


def shadowed(catalog: Catalog, kind: AliasKind, text: str) -> str | None:
    """Why an alias for `text` would never be used, or None: the matcher tries a record's own name (exact and
    normalized rules) before any alias, so a text that is one record's name is matched to that record."""
    match = match_part(text, catalog) if kind is AliasKind.PART else match_location(text, catalog, "")
    if match.status is MatchStatus.UNMATCHED or match.rule not in (MatchRule.EXACT, MatchRule.NORMALIZED):
        return None
    records = (match.record,) if match.record is not None else match.candidates
    if len(records) == 1:
        return f"it is the name of {kind.value} {records[0]}, which the matcher tries before any alias"
    listed = ", ".join(str(record) for record in records[:-1]) + f" and {records[-1]}"
    return f"it is the name of {kind.value}s {listed}, which the matcher tries before any alias"


def rivals(catalog: Catalog, kind: AliasKind, text: str, record: int) -> str | None:
    """The other records the whole-word or fuzzy rule fits `text` to now, or None. An admin alias is never rechecked
    and authorizes a write with no click (ADR 0004), so a post that would have asked which one no longer asks."""
    fits = part_rivals(text, catalog) if kind is AliasKind.PART else location_rivals(text, catalog, "")
    others = sorted(fits - {record})
    if not others:
        return None
    listed = ", ".join(f"{kind.value} {pk} {named(catalog, kind, pk)}" for pk in others)
    return f"{quoted(text.strip())} also fits {listed}: a post that says it is no longer asked which one it means"
