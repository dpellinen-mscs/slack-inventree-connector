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
LATER_WORDS = r"tomorrow|tmrw?|2mo?r+ow|tonight|tonite|later today|next week"
LATER = re.compile(rf"\b({LATER_WORDS})\b", re.IGNORECASE)
"""A time ahead. The extractor never reads it as "not a movement": "grabbed 3 ONTs for the job tomorrow" and
"picked up 3 ONTs for tomorrow" are takes that happened, and no list of past-tense verbs is complete."""
PLAN_OR_CONDITION = re.compile(
    r"\b(going to|gonna|i'll|want to|wanna|about to|plan(ning)? to)\b"
    r"|\b(i|we|he|she|they|you|someone|who) will\b|\bwill (be|take|grab|bring|need|get|pick|drop|return|load|use)\b"
    r"|^if\b|, if\b",
    re.IGNORECASE,
)
"""A plan or a condition. A time ahead is `LATER`, which is not a plan by itself."""
NEGATION = re.compile(
    r"\b(not|never|nothing|no longer|didn't|didnt|did not|don't|dont|wasn't|haven't|scratch that|actually no"
    r"|put (them|it) back|nvm|never ?mind|cancel(led)?|wrong|oops|undo)\b(?!-)",
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
_WHEN = r"(?:then|yesterday|today|earlier|this morning|also|so)"
_ADVERB = r"(?:\s+(?:just|also|already|then|finally))?"
_SOMEONE = r"(?!(?:i|we)\b)[a-z']+"
THIRD_PERSON = re.compile(
    rf"(?:(?:^|[.!\n]\s*)(?:(?:fyi|btw|ok|{_WHEN}),?\s+)?(?!(?:just|and|finally|already|{_WHEN})\b)"
    rf"{_SOMEONE}(?:\s+{_SOMEONE}){{0,2}}?{_ADVERB}|<@\w+(?:\|[^>]*)?>{_ADVERB}|(?<![\w@])@\w+{_ADVERB})"
    r"\s+(took|grabbed|put|loaded|moved|returned|brought|used|installed|received|dropped|pulled|picked up|counted)\b",
    re.IGNORECASE,
)
"""Someone other than the poster did it: "Mike put 2 poe 8 on his truck", "Then Mike Smith just took",
"<@U04ABC> grabbed", "fyi, the new guy took"."""
MENTION = re.compile(r"<@\w+(?:\|[^>]*)?>|(?<![\w@])@\w+")
"""A person named by a Slack mention: they may be the one who moved the stock."""
HEDGE = re.compile(r"\b(i think|maybe|probably|i guess|not sure|might have|might be|or so|ish)\b", re.IGNORECASE)

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}  # fmt: skip
CONTAINERS = frozenset({"box", "bag", "pack", "case", "roll", "spool", "bundle", "carton", "pallet"})
CONTAINER_WORDS = re.compile(
    r"\b(box(es)?|bags?|packs?|cases?|rolls?|spools?|bundles?|cartons?|pallets?)\b", re.IGNORECASE
)
VEHICLE_WORDS = re.compile(r"\b(truck|van|trailer|vehicle)s?\b|\bvan\d", re.IGNORECASE)
