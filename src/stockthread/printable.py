# SPDX-License-Identifier: MIT
"""Text from Slack made safe for a terminal: every character that is not printable (control characters, DEL, C1
codes, bidi overrides) is escaped as `\\uXXXX`, or as the UTF-16 surrogate pair JSON writes above U+FFFF, so none
reaches the terminal as itself and no escape is ever read as fewer digits. `stockthread trail` and `stockthread
aliases` print through it.
"""

from __future__ import annotations


def printable(text: str) -> str:
    """`text` with every character that is not printable escaped."""
    return "".join(char if char.isprintable() else _escaped(char) for char in text)


def _escaped(char: str) -> str:
    code = ord(char)
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    code -= 0x10000
    return f"\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}"
