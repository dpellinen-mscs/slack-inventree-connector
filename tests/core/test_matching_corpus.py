"""The matcher against every match expectation in the eval corpus (eval/README.md, "Match conventions").

The matcher gets the expected text, so an extraction error cannot hide a matching error.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from corpus import eval_catalog

from stockthread.core.matching import match_location, match_part, same_words
from stockthread.core.model import Match, MatchRule, MatchStatus

EVAL = Path(__file__).resolve().parents[2] / "eval"


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
    result = match_part(text, CATALOG, poster) if kind == "part" else match_location(text, CATALOG, poster)
    assert as_expected(result, kind) == expected


def learned_aliases() -> list[tuple[str, str]]:
    """(kind, text) of every alias in the corpus catalog a confirmation taught: those with `rivals`."""
    aliases = json.loads((EVAL / "catalog" / "aliases.json").read_text(encoding="utf-8"))
    return [
        (kind, a["text"])
        for kind, key in (("part", "parts"), ("location", "locations"))
        for a in aliases[key]
        if "rivals" in a
    ]


@pytest.mark.parametrize(("kind", "alias"), learned_aliases())
def test_every_learned_alias_decides_a_scored_match(kind: str, alias: str) -> None:
    """M4: learned aliases are covered by the corpus. Each decides, at the alias rule, every scored text it names. For
    "ap" the expected value also needs the recheck against its rivals (ADR 0012): without it "APs" is the Disc 40."""
    found = [(text, poster) for _, k, text, poster, _ in expectations() if k == kind and same_words(alias, text)]
    assert found, f"no case scores the learned alias {alias!r}"
    for text, poster in found:
        result = match_part(text, CATALOG, poster) if kind == "part" else match_location(text, CATALOG, poster)
        assert result.rule is MatchRule.ALIAS, text
