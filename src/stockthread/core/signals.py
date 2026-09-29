"""Text patterns that the rule-based extractor and the validator share.

They are heuristics over informal English. A hit is a review reason or a reason to decline,
never permission to write, so a pattern that is too wide costs a click, not a wrong movement.
"""

from __future__ import annotations

import re

SUSPICIOUS = re.compile(
    r"\b(ignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|rules)|system\s*:|without review"
    r"|writes? (are|is) (now )?(on|enabled)|mark (this|it) as (confirmed|approved)|you are now|disregard)",
    re.IGNORECASE,
)
"""Text that tries to instruct the connector."""
SENSITIVE = re.compile(
    r"\b(gate code|door code|lock ?box|alarm code|pass(word|code)|pin code|customer (cell|phone|number)"
    r"|phone|cell)\b|\b\d{3}[-.]\d{4}\b",
    re.IGNORECASE,
)
"""Customer details and secrets."""
QUESTION_OR_REQUEST = re.compile(
    r"\?|\b(can|could|would) (some|any)one\b|\b(can|could) you\b|\bneeds?\b|\bplease\b|\bshould\b|\bset \w+ aside\b",
    re.IGNORECASE,
)
PLAN_OR_CONDITION = re.compile(
    r"\b(going to|gonna|will|i'll|tomorrow|tonight|later today|next week|want to|wanna|about to|plan(ning)? to)\b"
    r"|^if\b|, if\b",
    re.IGNORECASE,
)
NEGATION = re.compile(
    r"\b(not|never|nothing|no longer|didn't|didnt|did not|don't|dont|wasn't|haven't|scratch that|actually no"
    r"|put (them|it) back|nvm|never ?mind|cancel(led)?|wrong|oops|undo)\b",
    re.IGNORECASE,
)
VAGUE = re.compile(
    r"\b(some|a few|few|several|about|around|approx(imately)?|roughly|couple|or so)\b|~\s*\d"
    r"|(?<![\w-])\d{1,3}\s*(-|to|or)\s*\d{1,3}(?![\w-])",
    re.IGNORECASE,
)
"""A quantity that is approximate, a range or a vague word."""
CORRECTION = re.compile(r"\b(actually|correction|make that|i mean|sorry|wait)\b|(^|\s)\*\w", re.IGNORECASE)
"""A later message or phrase that changes a stated value."""
THIRD_PERSON = re.compile(
    r"(?:^|[.!\n]\s*)(?!(?:i|we|just|also|then|and|finally|already|so)\b)([a-z]+)"
    r" (took|grabbed|put|loaded|moved|returned|brought|used|installed|received|dropped|pulled|counted)\b",
    re.IGNORECASE,
)
"""Someone other than the poster did it: "Mike put 2 poe 8 on his truck"."""
HEDGE = re.compile(r"\b(i think|maybe|probably|i guess|not sure|might have|might be|or so|ish)\b", re.IGNORECASE)

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
}  # fmt: skip
CONTAINERS = frozenset({"box", "bag", "pack", "case", "roll", "spool", "bundle", "carton", "pallet"})
CONTAINER_WORDS = re.compile(
    r"\b(box(es)?|bags?|packs?|cases?|rolls?|spools?|bundles?|cartons?|pallets?)\b", re.IGNORECASE
)
VEHICLE_WORDS = re.compile(r"\b(truck|van|trailer|vehicle)s?\b|\bvan\d", re.IGNORECASE)
