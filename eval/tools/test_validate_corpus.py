#!/usr/bin/env python3
"""Tests for validate_corpus.py. Standard library only:

    python3 -m unittest discover -s eval/tools -p "test_*.py"
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
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
            "messages": [{"ts": "1790000000.000100", "user": "U0SAM0001", "text": "2 Disc 40 on my truck"}],
        },
        "expected": {
            "movements": [
                {
                    "movement_type": "take_to_vehicle",
                    "line_items": [
                        {
                            "item_text": "Disc 40",
                            "quantity": 2,
                            "unit": None,
                            "match": {"status": "matched", "part": "AP-NV-D40"},
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
        item["match"] = {"status": "ambiguous", "candidates": ["AP-NV-D40"]}
        self.assert_rejected(case, "at least two candidates")
        item["match"] = {"status": "ambiguous", "candidates": ["AP-NV-D60", "AP-NV-D40"]}
        self.assert_rejected(case, "candidates must be sorted")

    def test_bad_quantity(self) -> None:
        for quantity in (-2, True, "2"):
            case = good_case()
            self.movement(case)["line_items"][0]["quantity"] = quantity
            self.assert_rejected(case, "quantity must be null or a number")

    def test_zero_quantity_only_in_a_count(self) -> None:
        case = good_case()
        self.movement(case)["line_items"][0]["quantity"] = 0
        self.assert_rejected(case, "valid only in a count")
        movement = self.movement(case)
        movement["movement_type"] = "count"
        movement["flags"] = []
        self.assertEqual(self.errors_for(case), [])

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

    def with_photo(self, case: dict) -> dict:
        case["post"]["messages"][0]["files"] = [
            {"id": "F0TEST0001", "mimetype": "image/jpeg", "synthetic_scene": "A label."}
        ]
        return case

    def test_typed_serial_in_a_photo_post_needs_no_flag(self) -> None:
        case = self.with_photo(good_case())
        self.movement(case)["line_items"][0]["serials"] = ["SN-1"]
        self.assertEqual(self.errors_for(case), [])

    def test_serial_flag_needs_serials_and_files(self) -> None:
        case = self.with_photo(good_case())
        self.movement(case)["flags"] = ["implied_source", "serial_unconfirmed"]
        self.assert_rejected(case, "no item has serials")
        case = good_case()
        self.movement(case)["line_items"][0]["serials"] = ["SN-1"]
        self.movement(case)["flags"] = ["implied_source", "serial_unconfirmed"]
        self.assert_rejected(case, "the post has no files")

    def test_bad_serials_and_pack_size(self) -> None:
        case = good_case()
        self.movement(case)["line_items"][0]["serials"] = [""]
        self.assert_rejected(case, "serials must be a list")
        case = good_case()
        self.movement(case)["line_items"][0]["pack_size"] = 0
        self.assert_rejected(case, "pack_size must be a number above zero")

    def test_pack_size_needs_flag(self) -> None:
        case = good_case()
        self.movement(case)["line_items"][0]["pack_size"] = 12
        self.assert_rejected(case, "missing flag pack_unit_ambiguous")

    def test_photo_without_text_needs_photo_only(self) -> None:
        case = self.with_photo(good_case())
        case["post"]["messages"][0]["text"] = ""
        self.assert_rejected(case, "missing flag photo_only")

    def test_consume_needs_no_destination(self) -> None:
        case = good_case()
        movement = self.movement(case)
        movement["movement_type"] = "consume"
        movement["destination_text"] = None
        del movement["destination_match"]
        self.assertEqual(self.errors_for(case), [])
        movement["flags"] = ["implied_destination", "implied_source"]
        self.assert_rejected(case, "implied_destination does not apply to consume")

    def test_unknown_tag(self) -> None:
        case = good_case()
        case["tags"] = ["brand-new-tag"]
        self.assert_rejected(case, "unknown tag")

    def test_unsorted_tags_and_flags(self) -> None:
        case = good_case()
        case["tags"] = ["typo", "alias"]
        self.assert_rejected(case, "tags must be sorted")
        case = good_case()
        self.movement(case)["flags"] = ["quantity_vague", "implied_source"]
        self.assert_rejected(case, "flags must be sorted")

    def test_post_flags(self) -> None:
        case = good_case()
        case["expected"]["post_flags"] = ["contains_sensitive"]
        self.assertEqual(self.errors_for(case), [])
        case["expected"]["post_flags"] = ["implied_source"]
        self.assert_rejected(case, "unknown post flag")
        case = good_case()
        self.movement(case)["flags"] = ["contains_sensitive", "implied_source"]
        self.assert_rejected(case, "unknown flag")

    def test_accept_lists(self) -> None:
        case = good_case()
        self.movement(case)["line_items"][0]["item_text_accept"] = ["Disc 40"]
        self.assert_rejected(case, "item_text_accept repeats a value")
        case = good_case()
        self.movement(case)["destination_text_accept"] = "truck"
        self.assert_rejected(case, "destination_text_accept must be a list")
        case = good_case()
        self.movement(case)["movement_type_accept"] = ["take_to_vehicle"]
        self.assert_rejected(case, "movement_type_accept repeats")

    def test_negative_case_cannot_expect_a_movement(self) -> None:
        case = good_case()
        case["id"] = "negative-999"
        self.assert_rejected(case, "a negative case cannot expect a movement")

    def test_bot_posts(self) -> None:
        case = good_case()
        case["post"]["user"] = "U0BOT00009"
        case["post"]["messages"][0]["user"] = "U0BOT00009"
        self.assert_rejected(case, "is_bot must be set for bot users")
        case["post"]["messages"][0]["is_bot"] = True
        self.assert_rejected(case, "only of bot messages")
        case["expected"]["movements"] = []
        self.assertEqual(self.errors_for(case), [])

    def test_post_user_is_the_first_poster(self) -> None:
        case = good_case()
        case["post"]["user"] = "U0MIKE0002"
        self.assert_rejected(case, "post.user must be the user of the first message")

    def test_corrected_in_thread_needs_a_second_message(self) -> None:
        case = good_case()
        self.movement(case)["flags"] = ["corrected_in_thread", "implied_source"]
        self.assert_rejected(case, "needs more than one message")

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


class CorpusDirectoryTest(unittest.TestCase):
    """Checks that look at the whole directory, run against a copy of the shipped corpus."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.eval_dir = Path(self.directory.name) / "eval"
        source = validate_corpus.EVAL_DIR
        for name in ("catalog", "schema"):
            shutil.copytree(source / name, self.eval_dir / name)
        (self.eval_dir / "cases").mkdir()
        (self.eval_dir / "VERSION").write_text("1.2.3\n", encoding="utf-8")
        self.write_cases("01.json", [good_case()])

    def write_cases(self, name: str, cases: list, **extra) -> None:
        data = {"format_version": 1, "cases": cases, **extra}
        (self.eval_dir / "cases" / name).write_text(json.dumps(data), encoding="utf-8")

    def edit_catalog(self, name: str, change) -> None:
        path = self.eval_dir / "catalog" / name
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_copy_is_valid(self) -> None:
        self.assertEqual(validate_corpus.main(self.eval_dir), 0)

    def test_duplicate_case_id(self) -> None:
        self.write_cases("02.json", [good_case()])
        self.assertEqual(validate_corpus.main(self.eval_dir), 1)

    def test_duplicate_file_id(self) -> None:
        first, second = good_case(), good_case()
        second["id"] = "sample-002"
        for case in (first, second):
            case["post"]["messages"][0]["files"] = [
                {"id": "F0TEST0001", "mimetype": "image/jpeg", "synthetic_scene": "A label."}
            ]
        self.write_cases("01.json", [first, second])
        self.assertEqual(validate_corpus.main(self.eval_dir), 1)

    def test_invalid_json(self) -> None:
        (self.eval_dir / "cases" / "02.json").write_text("{", encoding="utf-8")
        self.assertEqual(validate_corpus.main(self.eval_dir), 1)

    def test_wrong_format_version_and_extra_keys(self) -> None:
        (self.eval_dir / "cases" / "01.json").write_text(
            json.dumps({"format_version": 2, "cases": [good_case()]}), encoding="utf-8"
        )
        self.assertEqual(validate_corpus.main(self.eval_dir), 1)
        self.write_cases("01.json", [good_case()], comment="hello")
        self.assertEqual(validate_corpus.main(self.eval_dir), 1)

    def test_case_that_is_not_an_object(self) -> None:
        self.write_cases("01.json", ["clear-001"])
        self.assertEqual(validate_corpus.main(self.eval_dir), 1)

    def test_no_case_files(self) -> None:
        (self.eval_dir / "cases" / "01.json").unlink()
        self.assertEqual(validate_corpus.main(self.eval_dir), 1)

    def test_bad_version(self) -> None:
        (self.eval_dir / "VERSION").write_text("v1\n", encoding="utf-8")
        self.assertEqual(validate_corpus.main(self.eval_dir), 1)

    def test_catalog_checks(self) -> None:
        changes = {
            "alias to unknown part": ("aliases.json", lambda d: d["parts"].append({"text": "x1", "part": "NOPE"})),
            "alias to structural location": (
                "aliases.json",
                lambda d: d["locations"].append({"text": "yard", "location": "Vehicles"}),
            ),
            "alias not normalized": (
                "aliases.json",
                lambda d: d["locations"].append({"text": "The Yard", "location": "Vehicles/Trailer"}),
            ),
            "duplicate alias": ("aliases.json", lambda d: d["parts"].append(dict(d["parts"][0]))),
            "duplicate part": ("parts.json", lambda d: d["parts"].append(dict(d["parts"][0]))),
            "orphan location": (
                "locations.json",
                lambda d: d["locations"].append({"path": "Depot/Bay 1", "structural": False}),
            ),
            "user with unknown vehicle": (
                "users.json",
                lambda d: d["users"][0].update(vehicle="Vehicles/Truck 9"),
            ),
        }
        for label, (name, change) in changes.items():
            with self.subTest(label):
                original = (self.eval_dir / "catalog" / name).read_text(encoding="utf-8")
                self.edit_catalog(name, change)
                checker = validate_corpus.Checker(self.eval_dir)
                self.assertNotEqual(checker.errors, [])
                (self.eval_dir / "catalog" / name).write_text(original, encoding="utf-8")


class SchemaTest(unittest.TestCase):
    """The JSON Schema and the validator must accept the same corpus."""

    def setUp(self) -> None:
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        schema_path = validate_corpus.EVAL_DIR / "schema" / "case-file.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        self.validator = jsonschema.Draft202012Validator(schema)

    def test_shipped_corpus_matches_the_schema(self) -> None:
        for path in sorted((validate_corpus.EVAL_DIR / "cases").glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            errors = [f"{list(e.absolute_path)}: {e.message}" for e in self.validator.iter_errors(data)]
            self.assertEqual(errors, [], path.name)

    def test_schema_rejects_what_the_validator_rejects(self) -> None:
        def file_with(change) -> dict:
            case = good_case()
            change(case)
            return {"format_version": 1, "cases": [case]}

        movement = lambda case: case["expected"]["movements"][0]  # noqa: E731
        bad = {
            "unknown key": lambda c: c.update(expectd={}),
            "bad id": lambda c: c.update(id="Sample_1"),
            "unknown tag": lambda c: c.update(tags=["brand-new-tag"]),
            "unknown type": lambda c: movement(c).update(movement_type="teleport"),
            "unknown flag": lambda c: movement(c).update(flags=["looks_fine"]),
            "post flag on a movement": lambda c: movement(c).update(flags=["contains_sensitive"]),
            "negative quantity": lambda c: movement(c)["line_items"][0].update(quantity=-1),
            "upper-case unit": lambda c: movement(c)["line_items"][0].update(unit="Box"),
            "one candidate": lambda c: movement(c)["line_items"][0].update(
                match={"status": "ambiguous", "candidates": ["AP-NV-D40"]}
            ),
        }
        self.assertEqual(list(self.validator.iter_errors(file_with(lambda c: None))), [])
        for label, change in bad.items():
            with self.subTest(label):
                self.assertNotEqual(list(self.validator.iter_errors(file_with(change))), [])


if __name__ == "__main__":
    unittest.main()
