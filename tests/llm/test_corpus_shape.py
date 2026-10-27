# SPDX-License-Identifier: MIT
"""The output schema against the eval corpus: every expected extraction can be said in it, and read back whole.

This is not a score of any model. It feeds the parser the answers a perfect model would give, so a schema or
parser change that loses a field (a unit, a pack size, a serial, a flag) fails here. Model scores come from
recorded provider outputs (eval/README.md).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]

from stockthread.llm.extract import OUTPUT_SCHEMA, parse_output

ROOT = Path(__file__).resolve().parents[2]


def _rules_corpus() -> Any:
    spec = importlib.util.spec_from_file_location("rules_corpus", ROOT / "tests/core/test_extract_rules_corpus.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _perfect_answer(expected: dict[str, Any]) -> dict[str, Any]:
    return {
        "readable": True,
        "movements": [
            {
                "movement_type": movement["movement_type"],
                "line_items": [
                    {
                        "item_text": item.get("item_text"),
                        "quantity": item.get("quantity"),
                        "unit": item.get("unit"),
                        "pack_size": item.get("pack_size"),
                        "serials": item.get("serials", []),
                    }
                    for item in movement["line_items"]
                ],
                "source_text": movement.get("source_text"),
                "destination_text": movement.get("destination_text"),
                "flags": movement.get("flags", []),
            }
            for movement in expected["movements"]
        ],
        "post_flags": expected.get("post_flags", []),
    }


def test_every_expected_extraction_of_the_corpus_fits_the_schema_and_is_read_back_whole() -> None:
    helpers = _rules_corpus()
    scorer = helpers._scorer()
    cases = scorer.load_cases()
    predictions = []
    for case in cases:
        answer = _perfect_answer(case["expected"])
        jsonschema.validate(answer, OUTPUT_SCHEMA)
        extraction = parse_output(json.dumps(answer), "llm-test/perfect")
        assert not extraction.declined, case["id"]
        predictions.append(helpers.as_prediction(case["id"], extraction))
    version = (ROOT / "eval" / "VERSION").read_text(encoding="utf-8").strip()
    report = scorer.score(
        {"corpus_version": version, "extractor": "llm-test/perfect", "predictions": predictions}, matching=False
    )
    assert report.hard_failures == 0
    assert "Case exact match     accuracy  100.0%" in scorer.render(report)
