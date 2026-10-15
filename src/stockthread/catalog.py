"""The catalog snapshot the matcher works on, read from InvenTree (design section 10).

The snapshot is all or nothing: a record with a missing or mistyped field fails the whole load, because a
partial catalog would make a part or a location look unknown. Aliases are not read from InvenTree: the store
keeps them (`with_aliases`, ADR 0012). Vehicle assignments come from the `[vehicles]` settings and are resolved
against every snapshot (`assign_vehicles`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from stockthread.core.matching import Catalog, LocationRecord, PartRecord
from stockthread.inventree import InvenTreeError, InvenTreeReader
from stockthread.store import AliasKind, Store


async def load_catalog(reader: InvenTreeReader) -> Catalog:
    """Every active part and every stock location, as the matcher takes them."""
    parts = tuple(_part(row) for row in await reader.parts())
    locations = tuple(_location(row) for row in await reader.locations())
    return Catalog(parts=parts, locations=locations)


def with_aliases(load: Callable[[], Catalog], store: Store) -> Callable[[], Catalog]:
    """Each snapshot `load` gives, with the aliases in the store now: one learned since the last catalog read is
    matched by the next post, not after the next read."""

    def read() -> Catalog:
        return replace(
            load(), part_aliases=store.aliases(AliasKind.PART), location_aliases=store.aliases(AliasKind.LOCATION)
        )

    return read


def assign_vehicles(catalog: Catalog, vehicles: Mapping[str, str]) -> tuple[Catalog, tuple[str, ...]]:
    """The catalog with each person's vehicle (`[vehicles]`: Slack user id -> location path), and the users
    whose vehicle was not found.

    A path names a location only when it is that location's whole path, letter for letter, and the location
    can hold stock. Anything else (renamed, moved, structural, the same path twice) leaves the person without
    a vehicle, so "my truck" is unmatched and waits for a person: a near miss is never guessed.
    """
    by_path: dict[str, list[int]] = {}
    for location in catalog.locations:
        if not location.structural:
            by_path.setdefault(location.path, []).append(location.pk)
    found = {user: by_path[path][0] for user, path in vehicles.items() if len(by_path.get(path, ())) == 1}
    return replace(catalog, vehicles=found), tuple(sorted(set(vehicles) - set(found)))


def _part(row: Mapping[str, Any]) -> PartRecord:
    return PartRecord(
        pk=_int(row, "pk", "part"),
        ipn=_text(row, "IPN", "part", optional=True),
        name=_text(row, "name", "part"),
        keywords=_text(row, "keywords", "part", optional=True),
        active=_bool(row, "active", "part"),
        template=_bool(row, "is_template", "part"),
        virtual=_bool(row, "virtual", "part"),
    )


def _location(row: Mapping[str, Any]) -> LocationRecord:
    return LocationRecord(
        pk=_int(row, "pk", "location"),
        path=_text(row, "pathstring", "location"),
        description=_text(row, "description", "location", optional=True),
        structural=_bool(row, "structural", "location"),
    )


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
