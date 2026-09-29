"""The marker that the connector writes into the notes of every stock change (design section 7).

`[sic:<22 random characters>:<a or u><two-digit attempt number>]`. The random id
belongs to one request, and every attempt of that request, apply or undo, shares it.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from enum import StrEnum

ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
"""Crockford's base32 alphabet in lower case: no i, l, o or u, so a marker survives being read aloud."""
ID_LENGTH = 22
"""22 characters of 5 bits: 110 random bits."""
MAX_ATTEMPT = 99

MARKER_RE = re.compile(rf"\[sic:([{ALPHABET}]{{{ID_LENGTH}}}):([au])(\d{{2}})\]")


class AttemptKind(StrEnum):
    APPLY = "a"
    UNDO = "u"


def new_marker_id() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(ID_LENGTH))


def is_marker_id(value: str) -> bool:
    return len(value) == ID_LENGTH and all(char in ALPHABET for char in value)


@dataclass(frozen=True)
class Marker:
    marker_id: str
    kind: AttemptKind
    attempt: int

    def __post_init__(self) -> None:
        if not is_marker_id(self.marker_id):
            raise ValueError(f"a marker id is {ID_LENGTH} characters of the alphabet {ALPHABET}")
        if not 1 <= self.attempt <= MAX_ATTEMPT:
            raise ValueError(f"an attempt number is 1 to {MAX_ATTEMPT}")

    def __str__(self) -> str:
        return f"[sic:{self.marker_id}:{self.kind.value}{self.attempt:02d}]"

    @classmethod
    def parse(cls, text: str) -> Marker:
        """Parse a whole marker. Raise ValueError for anything else, including surrounding text."""
        found = MARKER_RE.fullmatch(text)
        if found is None:
            raise ValueError("not a marker")
        return cls(found.group(1), AttemptKind(found.group(2)), int(found.group(3)))
