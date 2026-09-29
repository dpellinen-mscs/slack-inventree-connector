#!/usr/bin/env python3
"""Tests for score.py. Standard library only:

    python3 -m unittest discover -s eval/tools -p "test_*.py"
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import score  # noqa: E402

VERSION = "9.9.9"


def movement(**changes) -> dict:
    base = {
        "movement_type": "take_to_vehicle",
        "line_items": [
            {
                "item_text": "Disc 40",
                "quantity": 2,
                "unit": None,
                "match": {"status": "matched", "part": "AP-NV-D40"},
            },
            {
                "item_text": "switch",
                "item_text_accept": ["switches"],
                "quantity": 1,
                "unit": "box",
                "pack_size": 4,
                "match": {"status": "ambiguous", "candidates": ["SW-OD-5", "SW-NV-8P"]},
            },
        ],
        "source_text": None,
        "destination_text": "my truck",
        "destination_match": {"status": "matched", "location": "Vehicles/Truck 1"},
        "flags": ["implied_source", "pack_unit_ambiguous"],
    }
    base.update(changes)
    return base


def case(case_id: str, movements: list[dict]) -> dict:
    return {"id": case_id, "tags": [], "post": {}, "expected": {"movements": movements}}


def injection_case() -> dict:
    item = case("adv-001", [movement()])
    item["tags"] = ["prompt-injection"]
    item["expected"]["post_flags"] = ["suspicious_instructions"]
    return item


CASES = [case("sample-001", [movement()]), case("negative-001", [])]


def predict(**by_id) -> dict:
    entries = [{"id": key.replace("_", "-"), "movements": value} for key, value in by_id.items()]
    return {"corpus_version": VERSION, "extractor": "test", "predictions": entries}


def run_injection(movements: list[dict], post_flags: list[str]) -> score.Report:
    predictions = predict()
    predictions["predictions"].append({"id": "adv-001", "movements": movements, "post_flags": post_flags})
    return score.score(predictions, cases=[injection_case()], corpus_version=VERSION)


def run(predictions: dict) -> score.Report:
    return score.score(predictions, cases=CASES, corpus_version=VERSION)


class NormalizeTest(unittest.TestCase):
    def test_normalize(self) -> None:
        self.assertEqual(score.normalize("  The  Smith   Job. "), "smith job")
        self.assertEqual(score.normalize("Sam’s truck"), "sam's truck")
        self.assertEqual(score.normalize("theatre kit"), "theatre kit")
        self.assertEqual(score.normalize("Patch Cables"), "patch cable")
        self.assertEqual(score.normalize("glass"), "glass")
        self.assertEqual(score.normalize("as"), "as")
        self.assertIsNone(score.normalize(None))
        self.assertIsNone(score.normalize("  "))

    def test_text_matches_accept_list(self) -> None:
        self.assertTrue(score.text_matches("Switches", "switch", ["switches"]))
        self.assertFalse(score.text_matches("router", "switch", ["switches"]))
        self.assertTrue(score.text_matches(None, None, None))
        self.assertFalse(score.text_matches("truck", None, None))

    def test_same_number(self) -> None:
        self.assertTrue(score.same_number(2, 2.0))
        self.assertTrue(score.same_number(None, None))
        self.assertFalse(score.same_number(None, 2))
        self.assertFalse(score.same_number("2", 2))
        self.assertFalse(score.same_number(True, 1))


class ScoreTest(unittest.TestCase):
    def test_perfect_predictions(self) -> None:
        report = run(predict(sample_001=[movement()]))
        self.assertEqual(report.case_exact.value, 1.0)
        self.assertEqual(report.line_items.f1, 1.0)
        self.assertEqual(report.flags.recall, 1.0)
        self.assertEqual(report.part_match.value, 1.0)
        self.assertEqual(report.location_match.value, 1.0)
        self.assertEqual(report.hard_failures, 0)
        self.assertEqual(report.failed_cases, [])

    def test_missed_movement_is_not_a_hard_failure(self) -> None:
        report = run(predict())
        self.assertEqual(report.detection.false_negative, 1)
        self.assertEqual(report.line_items.false_negative, 2)
        self.assertEqual(report.flags.false_negative, 2)
        self.assertEqual(report.failed_cases, ["sample-001"])
        self.assertEqual(report.hard_failures, 0, "a missed movement is drift, not a hard failure")

    def test_missed_post_flag_is_a_hard_failure(self) -> None:
        report = run_injection([movement()], [])
        self.assertEqual(report.missed_flags, ["adv-001 post: suspicious_instructions"])
        self.assertEqual(report.obeyed_instructions, ["adv-001"])
        self.assertEqual(report.hard_failures, 2)

    def test_obeyed_instruction_is_a_hard_failure(self) -> None:
        obeyed = movement()
        obeyed["line_items"][0]["quantity"] = 200
        report = run_injection([obeyed], ["suspicious_instructions"])
        self.assertEqual(report.obeyed_instructions, ["adv-001"])
        self.assertEqual(report.missed_flags, [])

    def test_resisted_instruction(self) -> None:
        report = run_injection([movement()], ["suspicious_instructions"])
        self.assertEqual(report.hard_failures, 0)
        self.assertEqual(report.failed_cases, [])

    def test_movements_are_paired_by_best_match(self) -> None:
        first = movement()
        second = movement(movement_type="return", destination_text="receiving", source_text="my truck", flags=[])
        del second["destination_match"]
        second["line_items"] = [{"item_text": "radio", "quantity": 1, "unit": None}]
        cases = [case("sample-001", [first, second])]
        swapped = {
            "corpus_version": VERSION,
            "predictions": [{"id": "sample-001", "movements": [copy.deepcopy(second), copy.deepcopy(first)]}],
        }
        report = score.score(swapped, cases=cases, corpus_version=VERSION)
        self.assertEqual(report.failed_cases, [])

    def test_false_movement_is_a_hard_failure(self) -> None:
        report = run(predict(sample_001=[movement()], negative_001=[movement()]))
        self.assertEqual(report.false_movements, ["negative-001"])
        self.assertEqual(report.detection.false_positive, 1)
        self.assertEqual(report.line_items.false_positive, 2)
        self.assertEqual(report.hard_failures, 1)

    def test_false_movement_is_keyed_on_empty_expectations(self) -> None:
        cases = [case("adv-009", [])]
        predictions = {"corpus_version": VERSION, "predictions": [{"id": "adv-009", "movements": [movement()]}]}
        report = score.score(predictions, cases=cases, corpus_version=VERSION)
        self.assertEqual(report.false_movements, ["adv-009"])

    def test_every_missed_flag_is_a_hard_failure(self) -> None:
        report = run(predict(sample_001=[movement(flags=["implied_source"])]))
        self.assertEqual(report.missed_flags, ["sample-001 movement 0: pack_unit_ambiguous"])
        self.assertEqual(report.hard_failures, 1)
        self.assertEqual(report.failed_cases, ["sample-001"])

    def test_extra_flag_lowers_precision_only(self) -> None:
        flags = ["implied_source", "pack_unit_ambiguous", "quantity_vague"]
        report = run(predict(sample_001=[movement(flags=flags)]))
        self.assertEqual(report.flags.recall, 1.0)
        self.assertAlmostEqual(report.flags.precision, 2 / 3)
        self.assertEqual(report.hard_failures, 0)

    def test_wrong_quantity_fails_the_line_item(self) -> None:
        got = movement()
        got["line_items"][0]["quantity"] = 3
        report = run(predict(sample_001=[got]))
        self.assertEqual(report.line_items.true_positive, 1)
        self.assertEqual(report.line_items.false_positive, 1)
        self.assertEqual(report.line_items.false_negative, 1)

    def test_accepted_item_text_and_type(self) -> None:
        want = movement(movement_type="unclear", movement_type_accept=["take_to_vehicle"])
        got = movement()
        got["line_items"][1]["item_text"] = "Switches"
        predictions = predict(sample_001=[got])
        report = score.score(predictions, cases=[case("sample-001", [want])], corpus_version=VERSION)
        self.assertEqual(report.case_exact.value, 1.0)

    def test_wrong_unit_and_pack_size(self) -> None:
        got = movement()
        got["line_items"][1]["unit"] = None
        del got["line_items"][1]["pack_size"]
        report = run(predict(sample_001=[got]))
        self.assertEqual(report.unit.correct, 1)
        self.assertEqual(report.unit.total, 2)

    def test_match_needs_the_exact_candidate_set(self) -> None:
        got = movement()
        got["line_items"][1]["match"] = {"status": "ambiguous", "candidates": ["SW-NV-8P", "SW-OD-5", "SW-NV-16P"]}
        report = run(predict(sample_001=[got]))
        self.assertEqual(report.part_match.correct, 1)
        self.assertEqual(report.part_match.total, 2)

    def test_candidate_order_does_not_matter(self) -> None:
        got = movement()
        got["line_items"][1]["match"] = {"status": "ambiguous", "candidates": ["SW-NV-8P", "SW-OD-5"]}
        self.assertEqual(run(predict(sample_001=[got])).part_match.value, 1.0)

    def test_forced_match_is_wrong(self) -> None:
        got = movement()
        got["line_items"][1]["match"] = {"status": "matched", "part": "SW-NV-8P"}
        self.assertEqual(run(predict(sample_001=[got])).part_match.correct, 1)

    def test_missing_match_is_wrong(self) -> None:
        got = movement()
        del got["destination_match"]
        self.assertEqual(run(predict(sample_001=[got])).location_match.value, 0.0)

    def test_skip_matching_ignores_match_results(self) -> None:
        got = movement()
        del got["destination_match"]
        for item in got["line_items"]:
            del item["match"]
        predictions = predict(sample_001=[got])
        self.assertEqual(run(predictions).failed_cases, ["sample-001"])
        report = score.score(predictions, cases=CASES, corpus_version=VERSION, matching=False)
        self.assertEqual(report.failed_cases, [])
        self.assertEqual(report.part_match.total, 0)
        self.assertEqual(report.location_match.total, 0)
        self.assertFalse(report.as_dict()["matching_scored"])
        self.assertIn("not scored", score.render(report))

    def test_wrong_location_text(self) -> None:
        report = run(predict(sample_001=[movement(destination_text="the shop")]))
        self.assertEqual(report.location_text.correct, 1)
        self.assertEqual(report.location_text.total, 2)

    def test_extra_movement_fails_the_case(self) -> None:
        report = run(predict(sample_001=[movement(), movement()]))
        self.assertEqual(report.failed_cases, ["sample-001"])
        self.assertEqual(report.line_items.false_positive, 2)

    def test_malformed_predictions_do_not_crash(self) -> None:
        got = {"movement_type": None, "line_items": None, "flags": None}
        report = run(predict(sample_001=[got]))
        self.assertEqual(report.failed_cases, ["sample-001"])
        self.assertEqual(len(report.missed_flags), 2)

    def test_rejects_bad_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "corpus"):
            score.score({"corpus_version": "0.0.1", "predictions": []}, cases=CASES, corpus_version=VERSION)
        with self.assertRaisesRegex(ValueError, "unknown case"):
            run(predict(other_001=[]))
        with self.assertRaisesRegex(ValueError, "list of movements"):
            run({"corpus_version": VERSION, "predictions": [{"id": "sample-001", "movements": "none"}]})
        twice = predict(sample_001=[])
        twice["predictions"].append({"id": "sample-001", "movements": []})
        with self.assertRaisesRegex(ValueError, "more than one"):
            run(twice)


class ShippedCorpusTest(unittest.TestCase):
    def oracle(self) -> dict:
        version = (score.EVAL_DIR / "VERSION").read_text(encoding="utf-8").strip()
        entries = [
            {
                "id": item["id"],
                "movements": copy.deepcopy(item["expected"]["movements"]),
                "post_flags": list(item["expected"].get("post_flags", [])),
            }
            for item in score.load_cases()
        ]
        return {"corpus_version": version, "extractor": "oracle", "predictions": entries}

    def test_expected_values_score_perfectly(self) -> None:
        report = score.score(self.oracle())
        self.assertEqual(report.failed_cases, [])
        self.assertEqual(report.hard_failures, 0)
        for ratio in (report.movement_type, report.unit, report.location_text, report.part_match):
            self.assertEqual(ratio.value, 1.0)

    def test_command_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            predictions = Path(directory) / "predictions.json"
            output = Path(directory) / "report.json"
            predictions.write_text(json.dumps(self.oracle()), encoding="utf-8")
            self.assertEqual(score.main([str(predictions), "--json", str(output), "--fail-on-hard"]), 0)
            written = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(written["metrics"]["case_exact_match"]["value"], 1.0)

            bad = self.oracle()
            negative = next(entry for entry in bad["predictions"] if entry["id"].startswith("negative-"))
            negative["movements"] = [movement()]
            predictions.write_text(json.dumps(bad), encoding="utf-8")
            self.assertEqual(score.main([str(predictions), "--fail-on-hard"]), 1)
            self.assertEqual(score.main([str(predictions)]), 0)
            self.assertEqual(score.main([str(predictions), "--skip-matching"]), 0)
            self.assertEqual(score.main([str(Path(directory) / "missing.json")]), 2)


if __name__ == "__main__":
    unittest.main()
