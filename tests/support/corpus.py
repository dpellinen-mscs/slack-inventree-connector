"""The eval corpus for tests: its cases, its expected movements as candidates, and its synthetic catalog."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from stockthread.core.matching import Alias, Catalog, LocationRecord, PartRecord, match_location, match_part
from stockthread.core.model import CandidateMovement, LineItem, Match, MatchStatus, MovementType
from stockthread.core.validate import validate
from stockthread.slack.replies import MovementView

EVAL = Path(__file__).resolve().parents[2] / "eval"


def cases() -> list[dict[str, Any]]:
    cases = []
    for path in sorted((EVAL / "cases").glob("*.json")):
        cases.extend(json.loads(path.read_text(encoding="utf-8"))["cases"])
    return cases


def candidate(expected: dict[str, Any]) -> CandidateMovement:
    """An expected movement as an extractor that raised no flag at all would return it."""
    return CandidateMovement(
        MovementType(expected["movement_type"]),
        tuple(
            LineItem(
                item["item_text"],
                None if item["quantity"] is None else Decimal(str(item["quantity"])),
                item.get("unit"),
                None if item.get("pack_size") is None else Decimal(str(item["pack_size"])),
            )
            for item in expected["line_items"]
        ),
        expected["source_text"],
        expected["destination_text"],
    )


def _load(name: str) -> Any:
    return json.loads((EVAL / "catalog" / f"{name}.json").read_text(encoding="utf-8"))


def eval_catalog() -> tuple[Catalog, dict[int, str], dict[int, str]]:
    """The synthetic catalog, with made-up primary keys. Returns it with pk -> IPN and pk -> path."""
    parts = tuple(
        PartRecord(pk, p["ipn"], p["name"], p["keywords"]) for pk, p in enumerate(_load("parts")["parts"], start=1)
    )
    rows = _load("locations")["locations"]
    location_pk = {loc["path"]: pk for pk, loc in enumerate(rows, start=101)}
    # A location's parent is the one whose path is its path less the last name, as InvenTree's tree would have it.
    locations = tuple(
        LocationRecord(
            pk,
            loc["path"],
            loc["description"],
            loc["structural"],
            location_pk.get(loc["path"][: max(loc["path"].rfind("/"), 0)]),
        )
        for pk, loc in enumerate(rows, start=101)
    )
    part_pk = {p.ipn: p.pk for p in parts}
    aliases = _load("aliases")

    def alias(entry: dict[str, Any], record: str, pks: dict[str, int]) -> Alias:
        # A learned alias holds the records that fitted its text when it was confirmed; an admin's has none.
        rivals = None if "rivals" not in entry else frozenset(pks[rival] for rival in entry["rivals"])
        return Alias(entry["text"], pks[entry[record]], rivals=rivals)

    catalog = Catalog(
        parts=parts,
        locations=locations,
        part_aliases=tuple(alias(a, "part", part_pk) for a in aliases["parts"]),
        location_aliases=tuple(alias(a, "location", location_pk) for a in aliases["locations"]),
        vehicles={u["id"]: location_pk[u["vehicle"]] for u in _load("users")["users"] if u["vehicle"]},
    )
    return catalog, {v: k for k, v in part_pk.items()}, {v: k for k, v in location_pk.items()}


CATALOG = eval_catalog()[0]


def corpus_views(case: dict[str, Any]) -> list[MovementView]:
    """The expected movements of a case, matched against the eval catalog and validated by rule."""
    text = "\n".join(message.get("text") or "" for message in case["post"]["messages"])
    poster = case["post"]["user"]
    views = []
    kinds = {candidate(expected).movement_type for expected in case["expected"]["movements"]}
    for expected in case["expected"]["movements"]:
        movement = candidate(expected)
        views.append(
            MovementView(
                movement,
                tuple(
                    match_part(item.item_text, CATALOG, poster) if item.item_text else Match(MatchStatus.UNMATCHED)
                    for item in movement.line_items
                ),
                match_location(movement.source_text, CATALOG, poster) if movement.source_text else None,
                match_location(movement.destination_text, CATALOG, poster) if movement.destination_text else None,
                validate(movement, text, max_quantity=100, kinds=kinds),
            )
        )
    return views
