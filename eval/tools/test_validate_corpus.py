#!/usr/bin/env python3
"""Tests for validate_corpus.py. Standard library only:

    python3 -m unittest discover -s eval/tools -p "test_*.py"
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate_corpus  # noqa: E402


def good_case() -> dict:
    return {
        "id": "sample-001",
        "tags": ["brief-example"],
        "post": {
            "channel": "C0ACME001",
            "user": "U0SAM0001",
            "messages": [{"ts": "1790000000.000100", "user": "U0SAM0001", "text": "2 L7 Lite on my truck"}],
        },
        "expected": {
            "movements": [
                {
                    "movement_type": "take_to_vehicle",
                    "line_items": [
                        {
                            "item_text": "L7 Lite",
                            "quantity": 2,
                            "unit": None,
                            "match": {"status": "matched", "part": "AP-NV-L7"},
                        }
                    ],
                    "source_text": None,
                    "destination_text": "my truck",
                    "destination_match": {"status": "matched", "location": "Vehicles/Truck 1"},
                    "flags": ["implied_source"],
                }
            ]
        },
    }


class ValidateCorpusTest(unittest.TestCase):
    def errors_for(self, case: dict) -> list[str]:
        checker = validate_corpus.Checker()
        self.assertEqual(checker.errors, [], "the catalog itself must be valid")
        checker.check_case("test", case)
        return checker.errors

    def assert_rejected(self, case: dict, fragment: str) -> None:
        errors = self.errors_for(case)
        self.assertTrue(
            any(fragment in error for error in errors),
            f"expected an error containing {fragment!r}, got {errors}",
        )

    def movement(self, case: dict) -> dict:
        return case["expected"]["movements"][0]

    def test_shipped_corpus_is_valid(self) -> None:
        self.assertEqual(validate_corpus.main(), 0)

    def test_good_case_passes(self) -> None:
        self.assertEqual(self.errors_for(good_case()), [])

    def test_negative_case_passes(self) -> None:
        case = good_case()
        case["expected"]["movements"] = []
        self.assertEqual(self.errors_for(case), [])

    def test_bad_id(self) -> None:
        case = good_case()
        case["id"] = "Sample_1"
        self.assert_rejected(case, "id must look like")

    def test_unknown_key(self) -> None:
        case = good_case()
        case["expectd"] = {}
        self.assert_rejected(case, "unknown key 'expectd'")

    def test_unknown_user(self) -> None:
        case = good_case()
        case["post"]["user"] = "U0NOBODY00"
        self.assert_rejected(case, "unknown user")

    def test_unknown_movement_type(self) -> None:
        case = good_case()
        self.movement(case)["movement_type"] = "teleport"
        self.assert_rejected(case, "unknown movement_type")

    def test_unknown_flag(self) -> None:
        case = good_case()
        self.movement(case)["flags"] = ["implied_source", "looks_fine"]
        self.assert_rejected(case, "unknown flag")

    def test_missing_implied_source(self) -> None:
        case = good_case()
        self.movement(case)["flags"] = []
        self.assert_rejected(case, "missing flag implied_source")

    def test_implied_source_contradicts_stated_source(self) -> None:
        case = good_case()
        self.movement(case)["source_text"] = "main stock"
        self.assert_rejected(case, "flag implied_source contradicts")

    def test_implied_source_not_applicable_to_receive(self) -> None:
        case = good_case()
        self.movement(case)["movement_type"] = "receive"
        self.assert_rejected(case, "implied_source does not apply to receive")

    def test_missing_implied_destination(self) -> None:
        case = good_case()
        movement = self.movement(case)
        movement["destination_text"] = None
        del movement["destination_match"]
        self.assert_rejected(case, "missing flag implied_destination")

    def test_match_without_text(self) -> None:
        case = good_case()
        movement = self.movement(case)
        movement["destination_text"] = None
        movement["flags"] = ["implied_destination", "implied_source"]
        self.assert_rejected(case, "destination_match is set but destination_text is null")

    def test_unknown_part(self) -> None:
        case = good_case()
        self.movement(case)["line_items"][0]["match"]["part"] = "AP-REAL-1"
        self.assert_rejected(case, "unknown part")

    def test_unknown_location(self) -> None:
        case = good_case()
        self.movement(case)["destination_match"]["location"] = "Vehicles/Truck 9"
        self.assert_rejected(case, "unknown location")

    def test_structural_location_cannot_match(self) -> None:
        case = good_case()
        self.movement(case)["destination_match"]["location"] = "Vehicles"
        self.assert_rejected(case, "is structural")

    def test_ambiguous_needs_two_sorted_candidates(self) -> None:
        case = good_case()
        item = self.movement(case)["line_items"][0]
        item["match"] = {"status": "ambiguous", "candidates": ["AP-NV-L7"]}
        self.assert_rejected(case, "at least two candidates")
        item["match"] = {"status": "ambiguous", "candidates": ["AP-NV-L7P", "AP-NV-L7"]}
        self.assert_rejected(case, "candidates must be sorted")

    def test_zero_and_negative_quantity(self) -> None:
        for quantity in (0, -2, True, "2"):
            case = good_case()
            self.movement(case)["line_items"][0]["quantity"] = quantity
            self.assert_rejected(case, "quantity must be null or a number above zero")

    def test_null_quantity_needs_a_flag(self) -> None:
        case = good_case()
        self.movement(case)["line_items"][0]["quantity"] = None
        self.assert_rejected(case, "a null quantity needs")

    def test_each_unit_must_be_null(self) -> None:
        case = good_case()
        self.movement(case)["line_items"][0]["unit"] = "pcs"
        self.assert_rejected(case, "means each and must be null")

    def test_container_unit_needs_flag(self) -> None:
        case = good_case()
        self.movement(case)["line_items"][0]["unit"] = "box"
        self.assert_rejected(case, "missing flag pack_unit_ambiguous")

    def test_missing_items_need_flag(self) -> None:
        case = good_case()
        self.movement(case)["line_items"] = []
        self.assert_rejected(case, "missing flag items_missing")

    def test_photo_flag_needs_files(self) -> None:
        case = good_case()
        self.movement(case)["flags"] = ["implied_source", "quantity_from_photo"]
        self.assert_rejected(case, "the post has no files")

    def test_photo_serials_need_flag(self) -> None:
        case = good_case()
        case["post"]["messages"][0]["files"] = [
            {"id": "F0TEST0001", "mimetype": "image/jpeg", "synthetic_scene": "A label."}
        ]
        self.movement(case)["line_items"][0]["serials"] = ["SN-1"]
        self.assert_rejected(case, "need serial_unconfirmed")

    def test_messages_must_ascend(self) -> None:
        case = good_case()
        case["post"]["messages"].append(copy.deepcopy(case["post"]["messages"][0]))
        self.assert_rejected(case, "ascending ts order")

    def test_thread_ts_must_point_at_root(self) -> None:
        case = good_case()
        case["post"]["messages"].append(
            {"ts": "1790000050.000100", "thread_ts": "1790000001.000100", "user": "U0SAM0001", "text": "make that 3"}
        )
        self.assert_rejected(case, "thread_ts must equal")

    def test_empty_message(self) -> None:
        case = good_case()
        case["post"]["messages"][0]["text"] = ""
        self.assert_rejected(case, "needs text or files")


if __name__ == "__main__":
    unittest.main()
