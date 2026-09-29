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
        self.assertEqual(score.normalize("Sam\u2019s truck"), "sam's truck")
        self.assertEqual(score.normalize("theatre kit"), "theatre kit")
        self.assertEqual(score.normalize("\uff2f\uff2e\uff34"), "ont")
        self.assertEqual(score.normalize("  "), "")
        self.assertEqual(score.normalize("?"), "")

    def test_plural_endings(self) -> None:
        pairs = {
            "Patch Cables": "patch cable",
            "ONTs": "ONT",
            "ONT's": "ONT",
            "drop 150s": "drop 150",
            "switches": "switch",
            "boxes": "box",
            "batteries": "battery",
            "buses": "bus",
            "lenses": "lens",
            "hoses": "hose",
            "fuses": "fuse",
            "cases": "case",
            "zip-ties": "zip-tie",
            "zip ties": "zip tie",
        }
        for plural, single in pairs.items():
            with self.subTest(plural):
                self.assertTrue(score.text_matches(plural, single, None))
                self.assertTrue(score.text_matches(single, plural, None))

    def test_plural_rule_ignores_letter_case(self) -> None:
        for predicted, expected in (("ups", "UPS"), ("Ups", "UPS"), ("ONTS", "ONTs"), ("onts", "ONTS")):
            with self.subTest(predicted):
                self.assertTrue(score.text_matches(predicted, expected, None))

    def test_words_that_are_not_plurals(self) -> None:
        different = {"UPS": "UP", "glass": "glas", "as": "a", "tape": "tap", "Disc 40": "Disc 4"}
        for text, other in different.items():
            with self.subTest(text):
                self.assertFalse(score.text_matches(text, other, None))
                self.assertTrue(score.text_matches(text, text, None))

    def test_plural_rule_can_be_turned_off(self) -> None:
        self.assertFalse(score.text_matches("rolls", "roll", None, plural=False))
        self.assertTrue(score.text_matches("Roll", "roll", None, plural=False))

    def test_text_matches(self) -> None:
        self.assertTrue(score.text_matches("Switches", "switch", None))
        self.assertTrue(score.text_matches("the routers", "router", ["AX3000"]))
        self.assertFalse(score.text_matches("router", "switch", ["switches"]))
        self.assertTrue(score.text_matches(None, None, None))
        self.assertFalse(score.text_matches("truck", None, None))
        self.assertFalse(score.text_matches(None, "truck", None))

    def test_an_empty_text_is_not_null(self) -> None:
        for text in ("", "  ", "?", "--"):
            with self.subTest(text):
                self.assertFalse(score.text_matches(text, None, None))

    def test_wrong_types_never_match(self) -> None:
        for value in (40, 4.5, True, ["truck"], {"text": "truck"}):
            with self.subTest(value):
                self.assertFalse(score.text_matches(value, "truck", None))
                self.assertFalse(score.text_matches(value, None, None))

    def test_same_number(self) -> None:
        self.assertTrue(score.same_number(2, 2.0))
        self.assertTrue(score.same_number(None, None))
        for value in (None, "2", True, float("nan"), float("inf"), 10**400, [2]):
            with self.subTest(value):
                self.assertFalse(score.same_number(value, 2))
        self.assertFalse(score.same_number(2, None))

    def test_match_equal(self) -> None:
        ambiguous = {"status": "ambiguous", "candidates": ["A", "B"]}
        self.assertTrue(score.match_equal({"status": "ambiguous", "candidates": ["B", "A"]}, ambiguous))
        for candidates in ("AB", {"A": 1, "B": 2}, ["A", "B", "B"], ["A", 1], 5, None):
            with self.subTest(candidates):
                self.assertFalse(score.match_equal({"status": "ambiguous", "candidates": candidates}, ambiguous))
        matched = {"status": "matched", "part": "A"}
        self.assertFalse(score.match_equal({"status": "matched", "part": ["A"]}, matched))
        self.assertFalse(score.match_equal("matched", matched))
        self.assertTrue(score.match_equal({"status": "unmatched"}, {"status": "unmatched"}))


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

    def test_missed_movement_counts_against_every_metric(self) -> None:
        report = run(predict())
        self.assertEqual(report.detection.false_negative, 1)
        self.assertEqual(report.line_items.false_negative, 2)
        self.assertEqual(report.flags.false_negative, 2)
        for ratio in (report.movement_type, report.unit, report.location_text, report.part_match):
            self.assertEqual(ratio.value, 0.0)
        self.assertEqual(report.location_match.as_dict(), {"correct": 0, "total": 1, "value": 0.0})
        self.assertEqual(report.failed_cases, ["sample-001"])

    def test_missed_movement_is_not_a_hard_failure(self) -> None:
        # README: a movement that was never extracted cannot be applied.
        self.assertEqual(run(predict()).hard_failures, 0)

    def test_one_of_two_movements_missing(self) -> None:
        second = movement(destination_text="van 3", flags=["actor_not_poster", "implied_source", "quantity_vague"])
        cases = [case("sample-001", [movement(), second])]
        report = score.score(predict(sample_001=[movement()]), cases=cases, corpus_version=VERSION)
        self.assertEqual(report.movement_type.as_dict()["total"], 2)
        self.assertEqual(report.movement_type.correct, 1)
        self.assertEqual(report.flags.false_negative, 3)
        self.assertEqual(report.missed_flags, [])
        self.assertEqual(report.failed_cases, ["sample-001"])

    def test_a_wrong_match_shows_even_when_the_quantity_is_wrong(self) -> None:
        got = movement()
        got["line_items"][0]["quantity"] = 3
        got["line_items"][0]["match"] = {"status": "matched", "part": "WRONG"}
        report = run(predict(sample_001=[got]))
        self.assertEqual(report.part_match.as_dict(), {"correct": 1, "total": 2, "value": 0.5})

    def test_made_up_serials_fail_the_case(self) -> None:
        got = movement()
        got["line_items"][0]["serials"] = ["FAKE"]
        self.assertEqual(run(predict(sample_001=[got])).failed_cases, ["sample-001"])

    def test_serials(self) -> None:
        want = movement()
        want["line_items"][0]["serials"] = ["SN-2", "SN-1"]
        cases = [case("sample-001", [want])]
        got = movement()
        got["line_items"][0]["serials"] = ["SN-1", "SN-2"]
        self.assertEqual(score.score(predict(sample_001=[got]), cases=cases, corpus_version=VERSION).failed_cases, [])
        for serials in (["SN-1"], "SN-1", 5, ["SN-1", 2], None, []):
            with self.subTest(serials):
                got["line_items"][0]["serials"] = serials
                report = score.score(predict(sample_001=[got]), cases=cases, corpus_version=VERSION)
                self.assertEqual(report.failed_cases, ["sample-001"])

    def test_source_text_and_match(self) -> None:
        want = movement(
            source_text="main stock",
            source_text_accept=["stock room"],
            source_match={"status": "matched", "location": "Warehouse/Main Stock"},
            flags=["pack_unit_ambiguous"],
        )
        cases = [case("sample-001", [want])]
        got = copy.deepcopy(want)
        got["source_text"] = "the stock room"
        report = score.score(predict(sample_001=[got]), cases=cases, corpus_version=VERSION)
        self.assertEqual(report.failed_cases, [])
        self.assertEqual(report.location_match.total, 2)
        got["source_match"] = {"status": "matched", "location": "Warehouse/Receiving"}
        report = score.score(predict(sample_001=[got]), cases=cases, corpus_version=VERSION)
        self.assertEqual(report.location_match.correct, 1)

    def test_flags_of_the_wrong_type(self) -> None:
        for flags in ("implied_source", {"implied_source": True}, [["implied_source"]], [{"a": 1}], 5):
            with self.subTest(flags):
                report = run(predict(sample_001=[movement(flags=flags)]))
                self.assertEqual(report.failed_cases, ["sample-001"])
                self.assertEqual(len(report.missed_flags), 2)
                self.assertEqual(report.flags.false_positive, 1)

    def test_duplicate_flags_count_once(self) -> None:
        flags = ["implied_source", "implied_source", "pack_unit_ambiguous"]
        report = run(predict(sample_001=[movement(flags=flags)]))
        self.assertEqual(report.failed_cases, [])
        self.assertEqual(report.flags.true_positive, 2)

    def test_post_flags_on_a_case_that_expects_none(self) -> None:
        for post_flags in (["contains_sensitive"], "contains_sensitive", {"a": 1}):
            with self.subTest(post_flags):
                predictions = predict(sample_001=[movement()])
                predictions["predictions"][0]["post_flags"] = post_flags
                report = run(predictions)
                self.assertEqual(report.failed_cases, ["sample-001"])
                self.assertEqual(report.flags.false_positive, 1)
                self.assertEqual(report.hard_failures, 0)

    def test_values_of_the_wrong_type_do_not_crash(self) -> None:
        broken = [
            {"movement_type": ["take_to_vehicle"]},
            {"line_items": 5},
            {"line_items": ["Disc 40", None, 7]},
            {"line_items": [{"item_text": 40, "quantity": 10**400, "unit": ["box"], "serials": 5, "match": "x"}]},
            {"source_text": {"a": 1}, "destination_text": 12},
            {"destination_match": {"status": "ambiguous", "candidates": 5}},
            {"flags": 5},
        ]
        for change in broken:
            with self.subTest(change):
                report = run(predict(sample_001=[movement(**change)]))
                self.assertEqual(report.failed_cases, ["sample-001"])
        report = run(predict(sample_001=[movement()], negative_001=[{"flags": 5, "line_items": 5}]))
        self.assertEqual(report.false_movements, ["negative-001"])

    def test_an_empty_movement_is_wrong_but_counts_as_extracted(self) -> None:
        report = run(predict(sample_001=[{}]))
        self.assertEqual(report.detection.true_positive, 1)
        self.assertEqual(report.movement_type.value, 0.0)
        self.assertEqual(report.location_text.as_dict(), {"correct": 1, "total": 2, "value": 0.5})
        self.assertEqual(len(report.missed_flags), 2)

    def test_items_with_the_same_text_pair_by_quantity(self) -> None:
        want = movement()
        want["line_items"] = [
            {"item_text": "ONTs", "quantity": 2, "unit": None},
            {"item_text": "ONTs", "quantity": 3, "unit": None},
        ]
        got = movement()
        got["line_items"] = [{"item_text": "ONTs", "quantity": 3, "unit": None}]
        cases = [case("sample-001", [want])]
        report = score.score(predict(sample_001=[got]), cases=cases, corpus_version=VERSION)
        counts = (report.line_items.true_positive, report.line_items.false_positive, report.line_items.false_negative)
        self.assertEqual(counts, (1, 0, 1))

    def test_a_field_that_is_left_out_is_read_as_null(self) -> None:
        want = movement(flags=[], destination_text=None)
        del want["destination_match"]
        want["line_items"] = [{"item_text": "ONTs", "quantity": 2, "unit": None}]
        got = {"movement_type": "take_to_vehicle", "line_items": [{"item_text": "ONT", "quantity": 2}]}
        report = score.score(predict(sample_001=[got]), cases=[case("sample-001", [want])], corpus_version=VERSION)
        self.assertEqual(report.failed_cases, [])

    def test_a_repeated_item_is_used_once(self) -> None:
        got = movement()
        got["line_items"] = [got["line_items"][0], copy.deepcopy(got["line_items"][0])]
        report = run(predict(sample_001=[got]))
        self.assertEqual(report.line_items.true_positive, 1)
        self.assertEqual(report.line_items.false_positive, 1)
        self.assertEqual(report.line_items.false_negative, 1)

    def test_pairing_looks_at_flags(self) -> None:
        first = movement(flags=["implied_source", "pack_unit_ambiguous"])
        second = movement(flags=["implied_source", "pack_unit_ambiguous", "quantity_vague"])
        cases = [case("sample-001", [first, second])]
        swapped = predict(sample_001=[copy.deepcopy(second), copy.deepcopy(first)])
        report = score.score(swapped, cases=cases, corpus_version=VERSION)
        self.assertEqual(report.failed_cases, [])
        self.assertEqual(report.hard_failures, 0)

    def test_a_tie_keeps_the_order(self) -> None:
        self.assertEqual(score.pair_movements([movement(), movement()], [movement(), movement()]), [(0, 0), (1, 1)])

    def test_pairing_with_more_and_fewer_predictions(self) -> None:
        other = movement(movement_type="return", destination_text="receiving")
        self.assertEqual(score.pair_movements([other, movement()], [movement()]), [(1, 0)])
        self.assertEqual(score.pair_movements([movement()], [other, movement()]), [(0, 1)])
        self.assertEqual(score.pair_movements([], [movement()]), [])

    def test_many_movements_are_paired_in_order(self) -> None:
        junk = movement(movement_type="return", destination_text="receiving", line_items=[])
        predicted = [junk] * score.MAX_PAIRING_SIZE + [movement()]
        self.assertEqual(score.pair_movements(predicted, [movement()]), [(0, 0)])

    def test_skip_matching_and_prompt_injection(self) -> None:
        got = movement(destination_match={"status": "matched", "location": "Vehicles/Other Van"})
        predictions = predict()
        predictions["predictions"].append(
            {"id": "adv-001", "movements": [got], "post_flags": ["suspicious_instructions"]}
        )
        cases = [injection_case()]
        with_matching = score.score(predictions, cases=cases, corpus_version=VERSION)
        self.assertEqual(with_matching.obeyed_instructions, ["adv-001"])
        without = score.score(predictions, cases=cases, corpus_version=VERSION, matching=False)
        self.assertEqual(without.obeyed_instructions, [])
        self.assertIn("extraction only", score.render(without))

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
        got["line_items"][1]["item_text"] = "the Switches"
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
        report = run(predict(sample_001=[got]))
        self.assertEqual(report.part_match.as_dict(), {"correct": 1, "total": 2, "value": 0.5})

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
        self.assertEqual(report.false_movements, [])  # the extra movement carries review reasons

    def test_an_extra_movement_without_flags_is_a_false_movement(self) -> None:
        invented = movement(flags=[], destination_text="van 3")
        invented["line_items"] = [{"item_text": "switches", "quantity": 5, "unit": None}]
        for flags in ([], None, "implied_source", [3]):
            with self.subTest(flags):
                invented["flags"] = flags
                report = run(predict(sample_001=[movement(), invented]))
                self.assertEqual(report.false_movements, ["sample-001"])
                self.assertEqual(report.hard_failures, 1)
        report = run(predict(sample_001=[movement(), invented, copy.deepcopy(invented)]))
        self.assertEqual(report.false_movements, ["sample-001"])  # listed once per case

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
        with self.assertRaisesRegex(ValueError, "list of movements"):
            run({"corpus_version": VERSION, "predictions": [{"id": "sample-001", "movements": ["none"]}]})
        with self.assertRaisesRegex(ValueError, "JSON object"):
            run([])
        with self.assertRaisesRegex(ValueError, "must be a list"):
            run({"corpus_version": VERSION, "predictions": None})
        for entry in ("sample-001", {"id": ["sample-001"], "movements": []}, {"movements": []}):
            with self.assertRaisesRegex(ValueError, "string id"):
                run({"corpus_version": VERSION, "predictions": [entry]})
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
        ratios = (report.movement_type, report.unit, report.location_text, report.part_match, report.location_match)
        for ratio in ratios:
            self.assertEqual(ratio.value, 1.0)
        for both in (report.detection, report.line_items, report.flags):
            self.assertEqual((both.precision, both.recall), (1.0, 1.0))

    def test_an_extractor_that_says_nothing(self) -> None:
        version = (score.EVAL_DIR / "VERSION").read_text(encoding="utf-8").strip()
        report = score.score({"corpus_version": version, "predictions": []})
        self.assertEqual(report.detection.recall, 0.0)
        self.assertEqual(report.false_movements, [])
        for ratio in (report.movement_type, report.unit, report.location_text, report.part_match):
            self.assertEqual(ratio.value, 0.0)
        self.assertTrue(all(" post: " in entry for entry in report.missed_flags))
        self.assertGreater(len(report.missed_flags), 0)

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
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["hard_failures"]["false_movements"], [])
            self.assertEqual(score.main([str(predictions), "--json", str(output)]), 0)
            written = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(written["hard_failures"]["false_movements"], [negative["id"]])
            self.assertEqual(score.main([str(Path(directory) / "missing.json")]), 2)
            predictions.write_text("[]", encoding="utf-8")
            self.assertEqual(score.main([str(predictions)]), 2)
            predictions.write_text("{", encoding="utf-8")
            self.assertEqual(score.main([str(predictions)]), 2)
            predictions.write_text(json.dumps(self.oracle()), encoding="utf-8")
            unwritable = Path(directory) / "no-such-directory" / "report.json"
            self.assertEqual(score.main([str(predictions), "--json", str(unwritable)]), 2)
            odd = self.oracle()
            odd["extractor"] = "\ud800"
            predictions.write_text(json.dumps(odd), encoding="utf-8")
            self.assertEqual(score.main([str(predictions)]), 0)


if __name__ == "__main__":
    unittest.main()
