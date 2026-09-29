"""The rule-based extractor against the eval corpus, scored by the reference scorer (eval/README.md).

Zero hard failures is the bar. `python -m pytest tests/core/test_extract_rules_corpus.py -s` prints the report.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from stockthread.core.extract_rules import EXTRACTOR, extract
from stockthread.core.model import Extraction, MessageInput

EVAL = Path(__file__).resolve().parents[2] / "eval"


def _scorer() -> Any:
    if "eval_score" in sys.modules:
        return sys.modules["eval_score"]
    spec = importlib.util.spec_from_file_location("eval_score", EVAL / "tools" / "score.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_score"] = module  # dataclasses look their module up while the file runs
    spec.loader.exec_module(module)
    return module


def _messages(post: dict[str, Any]) -> list[MessageInput]:
    """The post as ingest hands it over: bot messages are filtered out before extraction."""
    return [
        MessageInput(ts=m["ts"], text=m.get("text", ""), file_ids=tuple(f["id"] for f in m.get("files", ())))
        for m in post["messages"]
        if not m.get("is_bot")
    ]


def _number(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def as_prediction(case_id: str, extraction: Extraction) -> dict[str, Any]:
    return {
        "id": case_id,
        "movements": [
            {
                "movement_type": movement.movement_type.value,
                "line_items": [
                    {
                        "item_text": item.item_text,
                        "quantity": _number(item.quantity),
                        "unit": item.unit,
                        "pack_size": _number(item.pack_size),
                        "serials": list(item.serials),
                    }
                    for item in movement.line_items
                ],
                "source_text": movement.source_text,
                "destination_text": movement.destination_text,
                "flags": sorted(flag.value for flag in movement.flags),
            }
            for movement in extraction.movements
        ],
        "post_flags": sorted(flag.value for flag in extraction.post_flags),
    }


def predictions() -> dict[str, Any]:
    scorer = _scorer()
    return {
        "corpus_version": (EVAL / "VERSION").read_text(encoding="utf-8").strip(),
        "extractor": EXTRACTOR,
        "predictions": [as_prediction(case["id"], extract(_messages(case["post"]))) for case in scorer.load_cases()],
    }


def test_rules_extractor_has_no_hard_failures_on_the_corpus() -> None:
    scorer = _scorer()
    report = scorer.score(json.loads(json.dumps(predictions())), matching=False)
    print(scorer.render(report))
    assert report.false_movements == []
    assert report.missed_flags == []
    assert report.obeyed_instructions == []
