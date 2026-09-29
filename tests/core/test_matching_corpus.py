"""The matcher against every match expectation in the eval corpus (eval/README.md, "Match conventions").

The matcher gets the expected text, so an extraction error cannot hide a matching error.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stockthread.core.matching import Alias, Catalog, LocationRecord, PartRecord, match_location, match_part
from stockthread.core.model import Match, MatchStatus

EVAL = Path(__file__).resolve().parents[2] / "eval"


def _load(name: str) -> Any:
    return json.loads((EVAL / "catalog" / f"{name}.json").read_text(encoding="utf-8"))


def eval_catalog() -> tuple[Catalog, dict[int, str], dict[int, str]]:
    """The synthetic catalog, with made-up primary keys. Returns it with pk -> IPN and pk -> path."""
    parts = tuple(
        PartRecord(pk, p["ipn"], p["name"], p["keywords"]) for pk, p in enumerate(_load("parts")["parts"], start=1)
    )
    locations = tuple(
        LocationRecord(pk, loc["path"], loc["description"], loc["structural"])
        for pk, loc in enumerate(_load("locations")["locations"], start=101)
    )
    part_pk = {p.ipn: p.pk for p in parts}
    location_pk = {loc.path: loc.pk for loc in locations}
    aliases = _load("aliases")
    catalog = Catalog(
        parts=parts,
        locations=locations,
        part_aliases=tuple(Alias(a["text"], part_pk[a["part"]]) for a in aliases["parts"]),
        location_aliases=tuple(Alias(a["text"], location_pk[a["location"]]) for a in aliases["locations"]),
        vehicles={u["id"]: location_pk[u["vehicle"]] for u in _load("users")["users"] if u["vehicle"]},
    )
    return catalog, {v: k for k, v in part_pk.items()}, {v: k for k, v in location_pk.items()}


def expectations() -> list[tuple[str, str, str, str, dict[str, Any]]]:
    """(case id, kind, text, poster, expected match) for every scored match in the corpus."""
    found = []
    for path in sorted((EVAL / "cases").glob("*.json")):
        for case in json.loads(path.read_text(encoding="utf-8"))["cases"]:
            poster = case["post"]["user"]
            for movement in case["expected"]["movements"]:
                for item in movement["line_items"]:
                    if "match" in item:
                        found.append((case["id"], "part", item["item_text"], poster, item["match"]))
                for side in ("source", "destination"):
                    if f"{side}_match" in movement:
                        found.append(
                            (case["id"], "location", movement[f"{side}_text"], poster, movement[f"{side}_match"])
                        )
    return found


CATALOG, IPN, PATH = eval_catalog()


def as_expected(result: Match, kind: str) -> dict[str, Any]:
    names = IPN if kind == "part" else PATH
    if result.status is MatchStatus.MATCHED:
        assert result.record is not None
        return {"status": "matched", kind: names[result.record]}
    if result.status is MatchStatus.AMBIGUOUS:
        return {"status": "ambiguous", "candidates": sorted(names[pk] for pk in result.candidates)}
    return {"status": "unmatched"}


@pytest.mark.parametrize(("case", "kind", "text", "poster", "expected"), expectations())
def test_corpus_match(case: str, kind: str, text: str, poster: str, expected: dict[str, Any]) -> None:
    result = match_part(text, CATALOG) if kind == "part" else match_location(text, CATALOG, poster)
    assert as_expected(result, kind) == expected
