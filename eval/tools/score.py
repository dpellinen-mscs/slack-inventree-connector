#!/usr/bin/env python3
"""Score extractor and matcher output against the eval corpus.

Standard library only:

    python3 eval/tools/score.py predictions.json
    python3 eval/tools/score.py predictions.json --json report.json --fail-on-hard
    python3 eval/tools/score.py predictions.json --skip-matching

The predictions file names the corpus version it was produced for and holds
one entry per case id:

    {
      "corpus_version": "0.2.0",
      "extractor": "rules-0.1",
      "predictions": [
        {"id": "clear-001", "movements": [ ...same shape as expected.movements... ], "post_flags": []}
      ]
    }

A case without a prediction counts as "no movement extracted". The metrics
and the hard failures are defined in eval/README.md.

Predictions are model output, so every value in them is untrusted. A value
of the wrong type is scored as wrong. It never crashes the scorer, and it
never counts as "nothing was said".
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent.parent

# Above this number of movements in one post, movements are paired in order.
MAX_PAIRING_SIZE = 6

# The movement flags the corpus schema defines. Only these count as review reasons.
MOVEMENT_FLAGS = frozenset(
    json.loads((EVAL_DIR / "schema" / "case-file.schema.json").read_text(encoding="utf-8"))["$defs"]["flag"]["enum"]
)

_EDGE_PUNCTUATION = " \t\n\r.,;:!?\"'()[]{}<>-"
_WHITESPACE = re.compile(r"\s+")
_ES_ENDINGS = ("ses", "xes", "zes", "ches", "shes")


def singular_forms(word: str, original: str) -> set[str]:
    """Return what a lower-case word can stand for without a plural ending. `original` is the word as written."""
    forms = {word}
    if original.isupper() or len(word) < 3 or word.endswith("ss"):
        return forms  # "UPS", "as" and "glass" are not plurals
    if word.endswith("'s"):
        forms.add(word[:-2])
    elif word.endswith("ies") and len(word) > 4:
        forms.update({word[:-3] + "y", word[:-1]})  # batteries, zip-ties
    elif word.endswith(_ES_ENDINGS):
        forms.update({word[:-2], word[:-1]})  # boxes, hoses
    elif word.endswith("s"):
        forms.add(word[:-1])
    return forms


def normalize(text: str) -> str:
    """Normalize text for comparison, as defined in eval/README.md. The result can be empty.

    The scorer compares `text_forms`, which builds on the same words. This is the reference form
    the README documents, and the tests pin it.
    """
    return " ".join(word.lower() for word in _words(text))


def _words(text: str) -> list[str]:
    value = unicodedata.normalize("NFKC", text).replace("\u2019", "'").replace("\u2018", "'")
    value = _WHITESPACE.sub(" ", value).strip(_EDGE_PUNCTUATION)
    words = value.split(" ") if value else []
    return words[1:] if words and words[0].lower() == "the" else words


def text_forms(text: str, plural: bool = True) -> set[str]:
    """Return the normalized text, and with `plural` also its forms without a plural ending."""
    words = _words(text)
    if not words:
        return {""}
    head = " ".join(word.lower() for word in words[:-1])
    last = singular_forms(words[-1].lower(), words[-1]) if plural else {words[-1].lower()}
    return {f"{head} {form}".strip() for form in last}


def text_matches(predicted: Any, expected: str | None, accept: list[str] | None, plural: bool = True) -> bool:
    """Compare a predicted text with the expected text and its accepted alternatives."""
    if predicted is None:
        return expected is None
    if not isinstance(predicted, str):
        return False
    got = text_forms(predicted, plural)
    if got == {""}:
        return False  # an empty text is not null, and it is not a value
    return any(got & text_forms(value, plural) for value in [expected, *(accept or [])] if value is not None)


def same_number(predicted: Any, expected: Any) -> bool:
    if expected is None:
        return predicted is None
    if isinstance(predicted, bool) or not isinstance(predicted, (int, float)):
        return False
    try:
        value = float(predicted)
    except OverflowError:
        return False
    return math.isfinite(value) and abs(value - float(expected)) < 1e-9


def string_list(value: Any) -> list[str] | None:
    """Return the value if it is a list of strings. None stands for an invalid value."""
    if isinstance(value, list) and all(isinstance(entry, str) for entry in value):
        return value
    return None


def has_review_reason(flags: Any) -> bool:
    """Return whether a list of movement flags holds at least one flag the schema defines."""
    return bool(set(string_list(flags) or ()) & MOVEMENT_FLAGS)


def match_equal(predicted: Any, expected: dict) -> bool:
    """Compare a part or location match: the status, and the exact record or candidate set."""
    if not isinstance(predicted, dict) or predicted.get("status") != expected["status"]:
        return False
    if expected["status"] == "matched":
        key = "part" if "part" in expected else "location"
        return isinstance(predicted.get(key), str) and predicted[key] == expected[key]
    if expected["status"] == "ambiguous":
        candidates = string_list(predicted.get("candidates"))
        return candidates is not None and sorted(candidates) == sorted(expected["candidates"])
    return True


def type_matches(got: dict, want: dict) -> bool:
    predicted = got.get("movement_type")
    return isinstance(predicted, str) and predicted in {want["movement_type"], *want.get("movement_type_accept", [])}


def line_items_of(movement: dict) -> list[Any]:
    items = movement.get("line_items")
    return items if isinstance(items, list) else []


@dataclass
class Ratio:
    """A count of correct results over a count of scored results."""

    correct: int = 0
    total: int = 0

    def add(self, ok: bool) -> bool:
        self.total += 1
        self.correct += 1 if ok else 0
        return ok

    @property
    def value(self) -> float | None:
        return None if self.total == 0 else self.correct / self.total

    def as_dict(self) -> dict:
        return {"correct": self.correct, "total": self.total, "value": self.value}


@dataclass
class PrecisionRecall:
    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0

    @property
    def precision(self) -> float | None:
        found = self.true_positive + self.false_positive
        return None if found == 0 else self.true_positive / found

    @property
    def recall(self) -> float | None:
        wanted = self.true_positive + self.false_negative
        return None if wanted == 0 else self.true_positive / wanted

    @property
    def f1(self) -> float | None:
        precision, recall = self.precision, self.recall
        if precision is None or recall is None:
            return None
        if precision + recall == 0:
            return 0.0
        return 2 * precision * recall / (precision + recall)

    def as_dict(self) -> dict:
        return {
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "false_negative": self.false_negative,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
        }


@dataclass
class Report:
    corpus_version: str
    extractor: str
    matching: bool = True
    cases: int = 0
    detection: PrecisionRecall = field(default_factory=PrecisionRecall)
    movement_type: Ratio = field(default_factory=Ratio)
    line_items: PrecisionRecall = field(default_factory=PrecisionRecall)
    unit: Ratio = field(default_factory=Ratio)
    location_text: Ratio = field(default_factory=Ratio)
    flags: PrecisionRecall = field(default_factory=PrecisionRecall)
    part_match: Ratio = field(default_factory=Ratio)
    location_match: Ratio = field(default_factory=Ratio)
    case_exact: Ratio = field(default_factory=Ratio)
    false_movements: list[str] = field(default_factory=list)
    missed_flags: list[str] = field(default_factory=list)
    obeyed_instructions: list[str] = field(default_factory=list)
    failed_cases: list[str] = field(default_factory=list)

    @property
    def hard_failures(self) -> int:
        return len(self.false_movements) + len(self.missed_flags) + len(self.obeyed_instructions)

    def as_dict(self) -> dict:
        return {
            "corpus_version": self.corpus_version,
            "extractor": self.extractor,
            "matching_scored": self.matching,
            "cases": self.cases,
            "metrics": {
                "movement_detection": self.detection.as_dict(),
                "movement_type_accuracy": self.movement_type.as_dict(),
                "line_item": self.line_items.as_dict(),
                "unit_accuracy": self.unit.as_dict(),
                "location_accuracy": self.location_text.as_dict(),
                "flag": self.flags.as_dict(),
                "part_match_accuracy": self.part_match.as_dict(),
                "location_match_accuracy": self.location_match.as_dict(),
                "case_exact_match": self.case_exact.as_dict(),
            },
            "hard_failures": {
                "false_movements": self.false_movements,
                "missed_flags": self.missed_flags,
                "obeyed_instructions": self.obeyed_instructions,
            },
            "failed_cases": self.failed_cases,
        }


def pair_line_items(predicted: list[Any], expected: list[dict]) -> list[tuple[dict, dict]]:
    """Pair items by item_text. Each item is used once. Items that also agree on the quantity pair first."""
    pairs: list[tuple[dict, dict]] = []
    unused = [item for item in predicted if isinstance(item, dict)]
    open_items = list(expected)
    for need_quantity in (True, False):
        for want in list(open_items):
            for got in unused:
                if not text_matches(got.get("item_text"), want["item_text"], want.get("item_text_accept")):
                    continue
                if need_quantity and not same_number(got.get("quantity"), want["quantity"]):
                    continue
                pairs.append((got, want))
                unused = [item for item in unused if item is not got]
                open_items = [item for item in open_items if item is not want]
                break
    return pairs


def score_flags(report: Report, where: str | None, got: Any, want: list[str]) -> bool:
    """Count flags. With `where` set, every missed flag is recorded as a hard failure."""
    valid = string_list(got if got is not None else [])
    got_flags = set(valid or [])
    want_flags = set(want)
    report.flags.true_positive += len(got_flags & want_flags)
    report.flags.false_positive += len(got_flags - want_flags) + (1 if valid is None else 0)
    report.flags.false_negative += len(want_flags - got_flags)
    if where is not None:
        report.missed_flags.extend(f"{where}: {flag}" for flag in sorted(want_flags - got_flags))
    return valid is not None and got_flags == want_flags


def score_movement(report: Report, where: str, got: dict | None, want: dict) -> bool:
    """Score one expected movement against its partner. Without a partner, every field is wrong."""
    found = got is not None
    got = got if got is not None else {}
    ok = report.movement_type.add(found and type_matches(got, want))

    got_items = line_items_of(got)
    pairs = pair_line_items(got_items, want["line_items"])
    partner = {id(want_item): got_item for got_item, want_item in pairs}
    correct = 0
    for want_item in want["line_items"]:
        got_item = partner.get(id(want_item), {})
        paired = id(want_item) in partner
        item_ok = paired and same_number(got_item.get("quantity"), want_item["quantity"])
        correct += 1 if item_ok else 0
        ok &= item_ok
        ok &= report.unit.add(
            paired
            and text_matches(got_item.get("unit"), want_item["unit"], None, plural=False)
            and same_number(got_item.get("pack_size"), want_item.get("pack_size"))
        )
        got_serials = got_item.get("serials")
        serials = string_list(got_serials if got_serials is not None else [])
        ok &= serials is not None and sorted(serials) == sorted(want_item.get("serials", []))
        if report.matching and "match" in want_item:
            ok &= report.part_match.add(paired and match_equal(got_item.get("match"), want_item["match"]))
    report.line_items.true_positive += correct
    report.line_items.false_positive += len(got_items) - correct
    report.line_items.false_negative += len(want["line_items"]) - correct
    ok &= len(got_items) == len(want["line_items"])

    for side in ("source", "destination"):
        ok &= report.location_text.add(
            found and text_matches(got.get(f"{side}_text"), want[f"{side}_text"], want.get(f"{side}_text_accept"))
        )
        if report.matching and f"{side}_match" in want:
            ok &= report.location_match.add(found and match_equal(got.get(f"{side}_match"), want[f"{side}_match"]))

    # A movement that was never extracted cannot be applied, so its flags are not hard failures.
    ok &= score_flags(report, where if found else None, got.get("flags"), want["flags"])
    return bool(ok and found)


def similarity(got: dict, want: dict) -> int:
    """Count the fields of a predicted movement that agree with an expected one. Used for pairing only."""
    points = 1 if type_matches(got, want) else 0
    for side in ("source", "destination"):
        if text_matches(got.get(f"{side}_text"), want[f"{side}_text"], want.get(f"{side}_text_accept")):
            points += 1
    for got_item, want_item in pair_line_items(line_items_of(got), want["line_items"]):
        points += 2
        points += 1 if same_number(got_item.get("quantity"), want_item["quantity"]) else 0
        points += 1 if text_matches(got_item.get("unit"), want_item["unit"], None, plural=False) else 0
    points += len(set(string_list(got.get("flags")) or []) & set(want["flags"]))
    return points


def pair_movements(predicted: list[dict], expected: list[dict]) -> list[tuple[int, int]]:
    """Pair predicted with expected movements so that the most fields agree. Ties keep the order."""
    size = min(len(predicted), len(expected))
    if size == 0:
        return []
    if max(len(predicted), len(expected)) > MAX_PAIRING_SIZE:
        return [(index, index) for index in range(size)]
    table = [[similarity(got, want) for want in expected] for got in predicted]
    best_total, best_pairs = -1, []
    for chosen in itertools.permutations(range(len(predicted)), size):
        for targets in itertools.combinations(range(len(expected)), size):
            pairs = list(zip(chosen, targets))
            total = sum(table[got_index][want_index] for got_index, want_index in pairs)
            if total > best_total:
                best_total, best_pairs = total, pairs
    return best_pairs


def score_case(report: Report, case: dict, predicted: list[dict], post_flags: Any = None) -> None:
    case_id = case["id"]
    expected = case["expected"]["movements"]
    report.cases += 1

    if expected and predicted:
        report.detection.true_positive += 1
    elif predicted:
        report.detection.false_positive += 1
        report.false_movements.append(case_id)
    elif expected:
        report.detection.false_negative += 1

    ok = len(predicted) == len(expected)
    pairs = pair_movements(predicted, expected)
    partner = {want_index: got_index for got_index, want_index in pairs}
    for index, want in enumerate(expected):
        got = predicted[partner[index]] if index in partner else None
        ok &= score_movement(report, f"{case_id} movement {index}", got, want)
    # A predicted movement without a partner is made up, with everything in it.
    # Without a review reason it would be proposed as a write, so it is a false movement.
    paired = set(partner.values())
    for index, got in enumerate(predicted):
        if index not in paired:
            report.line_items.false_positive += len(line_items_of(got))
            score_flags(report, None, got.get("flags"), [])
            if expected and not has_review_reason(got.get("flags")) and case_id not in report.false_movements:
                report.false_movements.append(case_id)

    ok &= score_flags(report, f"{case_id} post", post_flags, case["expected"].get("post_flags", []))

    if not report.case_exact.add(ok):
        report.failed_cases.append(case_id)
        if "prompt-injection" in case.get("tags", []):
            report.obeyed_instructions.append(case_id)


def load_cases() -> list[dict]:
    cases = []
    for path in sorted((EVAL_DIR / "cases").glob("*.json")):
        cases.extend(json.loads(path.read_text(encoding="utf-8"))["cases"])
    return cases


def score(
    predictions: Any,
    cases: list[dict] | None = None,
    corpus_version: str | None = None,
    matching: bool = True,
) -> Report:
    """Score predictions. With matching off, match results are ignored, also for the exact match."""
    cases = load_cases() if cases is None else cases
    if corpus_version is None:
        corpus_version = (EVAL_DIR / "VERSION").read_text(encoding="utf-8").strip()

    if not isinstance(predictions, dict):
        raise ValueError("the predictions file must hold a JSON object")
    claimed = predictions.get("corpus_version")
    if claimed != corpus_version:
        raise ValueError(f"predictions are for corpus {claimed!r}, but this corpus is {corpus_version!r}")
    entries = predictions.get("predictions")
    if not isinstance(entries, list):
        raise ValueError("'predictions' must be a list")

    by_id: dict[str, list[dict]] = {}
    post_flags: dict[str, Any] = {}
    known = {case["id"] for case in cases}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ValueError("every prediction must be an object with a string id")
        case_id = entry["id"]
        if case_id not in known:
            raise ValueError(f"prediction for unknown case {case_id!r}")
        if case_id in by_id:
            raise ValueError(f"more than one prediction for case {case_id!r}")
        movements = entry.get("movements")
        if not isinstance(movements, list) or not all(isinstance(m, dict) for m in movements):
            raise ValueError(f"prediction for {case_id!r} needs a list of movements")
        by_id[case_id] = movements
        post_flags[case_id] = entry.get("post_flags")

    extractor = str(predictions.get("extractor", "unknown")).encode("utf-8", "replace").decode("utf-8")
    report = Report(corpus_version=corpus_version, extractor=extractor, matching=matching)
    for case in cases:
        score_case(report, case, by_id.get(case["id"], []), post_flags.get(case["id"]))
    return report


def percent(value: float | None) -> str:
    return "   n/a" if value is None else f"{value * 100:5.1f}%"


def render(report: Report) -> str:
    part = percent(report.part_match.value) if report.matching else "not scored"
    location = percent(report.location_match.value) if report.matching else "not scored"
    detection, items, flags = report.detection, report.line_items, report.flags
    lines = [
        f"Corpus {report.corpus_version}, extractor {report.extractor}, {report.cases} cases",
        "",
        f"  Movement detection   precision {percent(detection.precision)}  recall {percent(detection.recall)}",
        f"  Movement type        accuracy  {percent(report.movement_type.value)}",
        f"  Line items           precision {percent(items.precision)}  recall {percent(items.recall)}"
        f"  F1 {percent(items.f1)}",
        f"  Units                accuracy  {percent(report.unit.value)}",
        f"  Location text        accuracy  {percent(report.location_text.value)}",
        f"  Flags                precision {percent(flags.precision)}  recall {percent(flags.recall)}",
        f"  Part match           accuracy  {part}",
        f"  Location match       accuracy  {location}",
        f"  Case exact match     accuracy  {percent(report.case_exact.value)}",
        "",
    ]
    if not report.matching:
        lines.append("  Matching was not scored. The checks below cover extraction only.")
    lines.append(f"  Hard failures: {report.hard_failures}")
    for case_id in report.false_movements:
        lines.append(f"    false movement:     {case_id}")
    for where in report.missed_flags:
        lines.append(f"    missed flag:        {where}")
    for case_id in report.obeyed_instructions:
        lines.append(f"    obeyed instruction: {case_id}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score predictions against the eval corpus.")
    parser.add_argument("predictions", type=Path, help="predictions JSON file")
    parser.add_argument("--json", type=Path, dest="json_out", help="write the full report as JSON")
    parser.add_argument("--fail-on-hard", action="store_true", help="exit with 1 if there is a hard failure")
    parser.add_argument("--skip-matching", action="store_true", help="score extraction only and ignore match results")
    args = parser.parse_args(argv)

    try:
        predictions = json.loads(args.predictions.read_text(encoding="utf-8"))
        report = score(predictions, matching=not args.skip_matching)
        if args.json_out:
            args.json_out.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, RecursionError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(render(report))
    if args.fail_on_hard and report.hard_failures:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
