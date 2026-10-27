# SPDX-License-Identifier: MIT
"""The catalog snapshot the matcher works on, read from InvenTree (design section 10).

The snapshot is all or nothing: a record with a missing or mistyped field fails the whole load, because a
partial catalog would make a part or a location look unknown. Aliases are not read from InvenTree: the store
keeps them (`with_aliases`, ADR 0012). Vehicle assignments come from the `[vehicles]` settings, default sources
from `policy.default_source`, and sites from `locations.sites`; each is resolved against every snapshot
(`assign_vehicles`, `assign_default_sources`, `assign_sites`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from stockthread.core.matching import Catalog, LocationRecord, PartRecord, stale_aliases
from stockthread.core.model import MovementType
from stockthread.inventree import InvenTreeError, InvenTreeReader
from stockthread.store import AliasKind, Store

log = logging.getLogger(__name__)


async def load_catalog(reader: InvenTreeReader) -> Catalog:
    """Every active part and every stock location, as the matcher takes them."""
    parts = tuple(_part(row) for row in await reader.parts())
    locations = tuple(_location(row) for row in await reader.locations())
    return Catalog(parts=parts, locations=locations)


def with_aliases(load: Callable[[], Catalog], store: Store) -> Callable[[], Catalog]:
    """Each snapshot `load` gives, with the aliases in the store now: one learned since the last catalog read is
    matched by the next post, not after the next read.

    The records of aliases the matcher ignores, because their part or location cannot be matched now, are named in
    the log once until that list changes; by kind and record id, never by the alias's words."""
    reported: list[tuple[str, ...]] = [()]

    def read() -> Catalog:
        catalog = replace(
            load(), part_aliases=store.aliases(AliasKind.PART), location_aliases=store.aliases(AliasKind.LOCATION)
        )
        parts = stale_aliases(replace(catalog, location_aliases=()))
        places = stale_aliases(replace(catalog, part_aliases=()))
        ignored = tuple(sorted({f"part {a.record}" for a in parts} | {f"location {a.record}" for a in places}))
        if ignored != reported[0]:
            reported[0] = ignored
            if ignored:
                log.warning(
                    "aliases: stored aliases name %s, which cannot be matched now: they are ignored"
                    " (`stockthread check-setup` lists them by id)",
                    ", ".join(ignored),
                )
        return catalog

    return read


def assign_vehicles(catalog: Catalog, vehicles: Mapping[str, str]) -> tuple[Catalog, tuple[str, ...]]:
    """The catalog with each person's vehicle (`[vehicles]`: Slack user id -> location path), and the users
    whose vehicle was not found.

    A path names a location only when it is that location's whole path, letter for letter, and the location
    can hold stock. Anything else (renamed, moved, structural, the same path twice) leaves the person without
    a vehicle, so "my truck" is unmatched and waits for a person: a near miss is never guessed.
    """
    found = {user: pk for user, path in vehicles.items() if (pk := _holding(catalog, path)) is not None}
    return replace(catalog, vehicles=found), tuple(sorted(set(vehicles) - set(found)))


def assign_default_sources(catalog: Catalog, defaults: Mapping[str, str]) -> tuple[Catalog, tuple[str, ...]]:
    """The catalog with each movement type's default source (`policy.default_source`: type -> location path), and
    the types whose path was not found. A path names a location as for `assign_vehicles`; a type whose path names
    none keeps its implied source a review reason."""
    found = {MovementType(kind): pk for kind, path in defaults.items() if (pk := _holding(catalog, path)) is not None}
    return replace(catalog, default_sources=found), tuple(kind for kind in defaults if kind not in found)


def assign_sites(catalog: Catalog, sites: Sequence[str]) -> tuple[Catalog, tuple[str, ...]]:
    """The catalog with its sites (`locations.sites`: location paths), and the paths not found. A path names a
    location when it is the whole path of exactly one location, letter for letter; a structural folder counts. The
    sites are those locations and every location under them, by the parent each location has in InvenTree, not by
    its path: a name may hold a "/"."""
    found: dict[str, int] = {}
    for path in sites:
        at = [location.pk for location in catalog.locations if location.path == path]
        if len(at) == 1:
            found[path] = at[0]
    children: dict[int, list[int]] = {}
    for location in catalog.locations:
        if location.parent is not None:
            children.setdefault(location.parent, []).append(location.pk)
    under: set[int] = set()
    todo = list(found.values())
    while todo:
        pk = todo.pop()
        if pk not in under:
            under.add(pk)
            todo.extend(children.get(pk, ()))
    return replace(catalog, sites=frozenset(under)), tuple(path for path in sites if path not in found)


def _holding(catalog: Catalog, path: str) -> int | None:
    """The one location whose whole path is `path`, letter for letter, and that can hold stock; else None."""
    found = [location.pk for location in catalog.locations if location.path == path and not location.structural]
    return found[0] if len(found) == 1 else None


def _part(row: Mapping[str, Any]) -> PartRecord:
    return PartRecord(
        pk=_int(row, "pk", "part"),
        ipn=_text(row, "IPN", "part", optional=True),
        name=_text(row, "name", "part"),
        keywords=_text(row, "keywords", "part", optional=True),
        active=_bool(row, "active", "part"),
        template=_bool(row, "is_template", "part"),
        virtual=_bool(row, "virtual", "part"),
        units=_text(row, "units", "part", optional=True).strip(),
    )


def _location(row: Mapping[str, Any]) -> LocationRecord:
    return LocationRecord(
        pk=_int(row, "pk", "location"),
        path=_text(row, "pathstring", "location"),
        description=_text(row, "description", "location", optional=True),
        structural=_bool(row, "structural", "location"),
        parent=_parent(row),
    )


def _parent(row: Mapping[str, Any]) -> int | None:
    value = row.get("parent", ...)
    if value is None:
        return None
    if type(value) is not int:
        raise InvenTreeError("a location has no valid parent")
    return value


def _int(row: Mapping[str, Any], field: str, what: str) -> int:
    value = row.get(field)
    if type(value) is not int:
        raise InvenTreeError(f"a {what} has no valid {field}")
    return value


def _text(row: Mapping[str, Any], field: str, what: str, *, optional: bool = False) -> str:
    value = row.get(field)
    if value is None and optional:
        return ""
    if not isinstance(value, str) or (not optional and not value.strip()):
        raise InvenTreeError(f"a {what} has no valid {field}")
    return value


def _bool(row: Mapping[str, Any], field: str, what: str) -> bool:
    value = row.get(field)
    if type(value) is not bool:
        raise InvenTreeError(f"a {what} has no valid {field}")
    return value
