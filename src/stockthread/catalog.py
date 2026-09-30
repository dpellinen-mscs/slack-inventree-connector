"""The catalog snapshot the matcher works on, read from InvenTree (design section 10).

The snapshot is all or nothing: a record with a missing or mistyped field fails the whole load, because a
partial catalog would make a part or a location look unknown. Aliases and vehicle assignments are not read
from InvenTree; they come with alias learning (M4) and vehicle mapping.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stockthread.core.matching import Catalog, LocationRecord, PartRecord
from stockthread.inventree import InvenTreeError, InvenTreeReader


async def load_catalog(reader: InvenTreeReader) -> Catalog:
    """Every active part and every stock location, as the matcher takes them."""
    parts = tuple(_part(row) for row in await reader.parts())
    locations = tuple(_location(row) for row in await reader.locations())
    return Catalog(parts=parts, locations=locations)


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
