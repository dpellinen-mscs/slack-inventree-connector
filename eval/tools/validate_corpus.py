#!/usr/bin/env python3
"""Check the eval corpus for structural and referential errors.

Standard library only, so it runs anywhere Python 3.9+ is installed:

    python3 eval/tools/validate_corpus.py

The JSON Schema in eval/schema/ is the formal definition of the format. This
script enforces the same rules without needing a schema library, and adds the
checks a schema cannot express (unique ids, catalog references, message order).
Exit code 0 means the corpus is valid.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent.parent
FORMAT_VERSION = 1

ID_RE = re.compile(r"^[a-z]+-[0-9]{3}$")
TAG_RE = re.compile(r"^[a-z0-9-]+$")
TS_RE = re.compile(r"^[0-9]{10}\.[0-9]{6}$")
UNIT_RE = re.compile(r"^[a-z]+$")
USER_RE = re.compile(r"^U[A-Z0-9]+$")
CHANNEL_RE = re.compile(r"^C[A-Z0-9]+$")
FILE_RE = re.compile(r"^F[A-Z0-9]+$")

# Units that mean "one of the item". They normalize to a null unit.
EACH_UNITS = {"each", "ea", "pc", "pcs", "piece", "pieces", "x", "unit", "units"}
# Units that name a container. They always carry the pack_unit_ambiguous flag.
CONTAINER_UNITS = {"box", "bag", "pack", "case", "roll", "spool", "bundle", "carton", "pallet"}
# Movement types that take stock from a source location.
NEEDS_SOURCE = {"take_to_vehicle", "take_to_site", "return", "consume", "unclear"}

CASE_KEYS = {"id", "tags", "post", "expected", "notes"}
POST_KEYS = {"channel", "user", "messages"}
MESSAGE_KEYS = {"ts", "thread_ts", "user", "text", "is_bot", "files"}
FILE_KEYS = {"id", "mimetype", "synthetic_scene", "image_path"}
MOVEMENT_KEYS = {
    "movement_type",
    "movement_type_accept",
    "line_items",
    "source_text",
    "source_text_accept",
    "source_match",
    "destination_text",
    "destination_text_accept",
    "destination_match",
    "flags",
}
LINE_ITEM_KEYS = {"item_text", "item_text_accept", "quantity", "unit", "pack_size", "serials", "match"}


def load_schema_enums() -> tuple[set[str], set[str]]:
    """Read the enums from the schema so the two definitions cannot drift."""
    schema = json.loads((EVAL_DIR / "schema" / "case-file.schema.json").read_text(encoding="utf-8"))
    defs = schema["$defs"]
    return set(defs["movement_type"]["enum"]), set(defs["flag"]["enum"])


class Checker:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.movement_types, self.flags = load_schema_enums()
        catalog = EVAL_DIR / "catalog"
        parts = json.loads((catalog / "parts.json").read_text(encoding="utf-8"))["parts"]
        locations = json.loads((catalog / "locations.json").read_text(encoding="utf-8"))["locations"]
        users = json.loads((catalog / "users.json").read_text(encoding="utf-8"))["users"]
        aliases = json.loads((catalog / "aliases.json").read_text(encoding="utf-8"))
        self.parts = {p["ipn"] for p in parts}
        self.locations = {loc["path"]: loc for loc in locations}
        self.users = {u["id"] for u in users}
        self.check_catalog(parts, locations, users, aliases)

    def error(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    # ----- catalog -----

    def check_catalog(self, parts: list, locations: list, users: list, aliases: dict) -> None:
        if len(self.parts) != len(parts):
            self.error("catalog/parts.json", "duplicate IPN")
        if len(self.locations) != len(locations):
            self.error("catalog/locations.json", "duplicate location path")
        for path in self.locations:
            parent = path.rpartition("/")[0]
            if parent and parent not in self.locations:
                self.error("catalog/locations.json", f"{path!r} has no parent location {parent!r}")
        for user in users:
            vehicle = user.get("vehicle")
            if vehicle is not None:
                self.check_location_ref("catalog/users.json", vehicle)
        for alias in aliases.get("parts", []):
            if alias["part"] not in self.parts:
                self.error("catalog/aliases.json", f"unknown part {alias['part']!r}")
        for alias in aliases.get("locations", []):
            self.check_location_ref("catalog/aliases.json", alias["location"])

    def check_location_ref(self, where: str, path: str) -> None:
        location = self.locations.get(path)
        if location is None:
            self.error(where, f"unknown location {path!r}")
        elif location.get("structural"):
            self.error(where, f"{path!r} is structural and cannot hold stock")

    # ----- cases -----

    def check_keys(self, where: str, obj: dict, allowed: set[str], required: set[str]) -> None:
        for key in obj.keys() - allowed:
            self.error(where, f"unknown key {key!r}")
        for key in required - obj.keys():
            self.error(where, f"missing key {key!r}")

    def check_case(self, where: str, case: dict) -> None:
        self.check_keys(where, case, CASE_KEYS, {"id", "tags", "post", "expected"})
        if not ID_RE.match(str(case.get("id", ""))):
            self.error(where, "id must look like 'name-001'")
        tags = case.get("tags", [])
        if tags != sorted(set(tags)):
            self.error(where, "tags must be sorted and unique")
        for tag in tags:
            if not TAG_RE.match(tag):
                self.error(where, f"bad tag {tag!r}")
        self.check_post(where, case.get("post", {}))
        expected = case.get("expected", {})
        self.check_keys(where, expected, {"movements"}, {"movements"})
        has_files = any(m.get("files") for m in case.get("post", {}).get("messages", []))
        for index, movement in enumerate(expected.get("movements", [])):
            self.check_movement(f"{where} movement {index}", movement, has_files)

    def check_post(self, where: str, post: dict) -> None:
        self.check_keys(where, post, POST_KEYS, POST_KEYS)
        if not CHANNEL_RE.match(str(post.get("channel", ""))):
            self.error(where, "bad channel id")
        if post.get("user") not in self.users:
            self.error(where, f"unknown user {post.get('user')!r}")
        messages = post.get("messages", [])
        if not messages:
            self.error(where, "a post needs at least one message")
            return
        root_ts = messages[0].get("ts")
        previous = ""
        for index, message in enumerate(messages):
            at = f"{where} message {index}"
            self.check_keys(at, message, MESSAGE_KEYS, {"ts", "user", "text"})
            ts = str(message.get("ts", ""))
            if not TS_RE.match(ts):
                self.error(at, "bad ts")
            if ts <= previous:
                self.error(at, "messages must be in ascending ts order")
            previous = ts
            if message.get("user") not in self.users:
                self.error(at, f"unknown user {message.get('user')!r}")
            if "thread_ts" in message and message["thread_ts"] != root_ts:
                self.error(at, "thread_ts must equal the ts of the first message")
            if not isinstance(message.get("text"), str):
                self.error(at, "text must be a string")
            files = message.get("files", [])
            if not message.get("text") and not files:
                self.error(at, "a message needs text or files")
            for file in files:
                self.check_keys(at, file, FILE_KEYS, {"id", "mimetype", "synthetic_scene"})
                if not FILE_RE.match(str(file.get("id", ""))):
                    self.error(at, "bad file id")
                if "image_path" in file and not (EVAL_DIR / file["image_path"]).is_file():
                    self.error(at, f"image_path {file['image_path']!r} does not exist")
        if messages[0].get("is_bot"):
            self.error(where, "the first message of a post cannot be a bot message")

    def check_movement(self, where: str, movement: dict, has_files: bool) -> None:
        self.check_keys(where, movement, MOVEMENT_KEYS, MOVEMENT_KEYS - {
            "movement_type_accept", "source_text_accept", "source_match",
            "destination_text_accept", "destination_match",
        })
        movement_type = movement.get("movement_type")
        for value in [movement_type, *movement.get("movement_type_accept", [])]:
            if value not in self.movement_types:
                self.error(where, f"unknown movement_type {value!r}")
        if movement_type in movement.get("movement_type_accept", []):
            self.error(where, "movement_type_accept repeats movement_type")

        flags = movement.get("flags", [])
        if flags != sorted(set(flags)):
            self.error(where, "flags must be sorted and unique")
        for flag in flags:
            if flag not in self.flags:
                self.error(where, f"unknown flag {flag!r}")

        for side in ("source", "destination"):
            text = movement.get(f"{side}_text")
            if text is not None and (not isinstance(text, str) or not text.strip()):
                self.error(where, f"{side}_text must be null or a non-empty string")
            match = movement.get(f"{side}_match")
            if match is not None:
                if text is None:
                    self.error(where, f"{side}_match is set but {side}_text is null")
                self.check_match(where, match, "location")

        # Flags that follow mechanically from the expected fields must agree with them.
        source_missing = movement.get("source_text") is None
        if movement_type in NEEDS_SOURCE:
            self.expect_flag(where, flags, "implied_source", source_missing)
        elif "implied_source" in flags:
            self.error(where, f"implied_source does not apply to {movement_type}")
        self.expect_flag(where, flags, "implied_destination", movement.get("destination_text") is None)

        items = movement.get("line_items", [])
        no_item_text = not items or any(item.get("item_text") is None for item in items)
        self.expect_flag(where, flags, "items_missing", no_item_text)
        uses_container = False
        has_serials = False
        null_quantity = False
        for index, item in enumerate(items):
            at = f"{where} item {index}"
            self.check_keys(at, item, LINE_ITEM_KEYS, {"item_text", "quantity", "unit"})
            quantity = item.get("quantity")
            if quantity is None:
                null_quantity = True
            elif isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or quantity <= 0:
                self.error(at, "quantity must be null or a number above zero")
            unit = item.get("unit")
            if unit is not None:
                if not UNIT_RE.match(str(unit)):
                    self.error(at, "unit must be lower-case letters")
                if unit in EACH_UNITS:
                    self.error(at, f"unit {unit!r} means each and must be null")
                if unit in CONTAINER_UNITS:
                    uses_container = True
            if "pack_size" in item:
                uses_container = True
            if item.get("serials"):
                has_serials = True
            if "match" in item:
                if item.get("item_text") is None:
                    self.error(at, "match is set but item_text is null")
                self.check_match(at, item["match"], "part")
        self.expect_flag(where, flags, "pack_unit_ambiguous", uses_container)
        if null_quantity and not {"quantity_missing", "quantity_vague"} & set(flags):
            self.error(where, "a null quantity needs quantity_missing or quantity_vague")
        if "quantity_missing" in flags and not null_quantity:
            self.error(where, "quantity_missing is set but every quantity is stated")
        if has_serials and has_files and "serial_unconfirmed" not in flags:
            self.error(where, "serials in a post with photos need serial_unconfirmed")
        for flag in ("photo_only", "quantity_from_photo", "photo_text_mismatch"):
            if flag in flags and not has_files:
                self.error(where, f"{flag} is set but the post has no files")

    def expect_flag(self, where: str, flags: list, flag: str, expected: bool) -> None:
        if expected and flag not in flags:
            self.error(where, f"missing flag {flag}")
        if not expected and flag in flags:
            self.error(where, f"flag {flag} contradicts the expected fields")

    def check_match(self, where: str, match: dict, kind: str) -> None:
        status = match.get("status")
        single = "part" if kind == "part" else "location"
        if status == "matched":
            self.check_keys(where, match, {"status", single}, {"status", single})
            refs = [match.get(single)]
        elif status == "ambiguous":
            self.check_keys(where, match, {"status", "candidates"}, {"status", "candidates"})
            refs = match.get("candidates", [])
            if len(refs) < 2:
                self.error(where, "ambiguous needs at least two candidates")
            if refs != sorted(set(refs)):
                self.error(where, "candidates must be sorted and unique")
        elif status == "unmatched":
            self.check_keys(where, match, {"status"}, {"status"})
            refs = []
        else:
            self.error(where, f"unknown match status {status!r}")
            return
        for ref in refs:
            if kind == "part":
                if ref not in self.parts:
                    self.error(where, f"unknown part {ref!r}")
            else:
                self.check_location_ref(where, ref)


def main() -> int:
    checker = Checker()
    seen: dict[str, str] = {}
    file_ids: dict[str, str] = {}
    total = 0
    case_files = sorted((EVAL_DIR / "cases").glob("*.json"))
    if not case_files:
        checker.error("cases/", "no case files found")
    for path in case_files:
        name = f"cases/{path.name}"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            checker.error(name, f"invalid JSON: {exc}")
            continue
        if data.get("format_version") != FORMAT_VERSION:
            checker.error(name, f"format_version must be {FORMAT_VERSION}")
        for case in data.get("cases", []):
            case_id = str(case.get("id"))
            where = f"{name} {case_id}"
            if case_id in seen:
                checker.error(where, f"duplicate id, first seen in {seen[case_id]}")
            seen[case_id] = name
            for message in case.get("post", {}).get("messages", []):
                for file in message.get("files", []):
                    if file.get("id") in file_ids:
                        checker.error(where, f"duplicate file id {file.get('id')!r}")
                    file_ids[file.get("id")] = case_id
            checker.check_case(where, case)
            total += 1

    version = (EVAL_DIR / "VERSION").read_text(encoding="utf-8").strip()
    if not re.match(r"^[0-9]+\.[0-9]+\.[0-9]+$", version):
        checker.error("VERSION", "must be MAJOR.MINOR.PATCH")

    if checker.errors:
        for line in checker.errors:
            print(f"ERROR {line}", file=sys.stderr)
        print(f"{len(checker.errors)} error(s) in {total} cases", file=sys.stderr)
        return 1
    print(f"OK: corpus {version}, {total} cases in {len(case_files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
