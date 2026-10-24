import json

import pytest

from stockthread.printable import printable


def test_printable_text_is_unchanged() -> None:
    assert printable("moved 2 Disc 40 to Sam's truck → bay 3 ✓") == "moved 2 Disc 40 to Sam's truck → bay 3 ✓"


@pytest.mark.parametrize(
    ("char", "escaped"),
    [
        ("\x1b", "\\u001b"),  # ESC: starts terminal control sequences
        ("\n", "\\u000a"),
        ("\x7f", "\\u007f"),  # DEL
        ("\x9b", "\\u009b"),  # C1 CSI
        ("‮", "\\u202e"),  # right-to-left override
        ("\U000e0041", "\\udb40\\udc41"),  # a tag character above U+FFFF: a surrogate pair, as JSON writes it
    ],
)
def test_a_character_that_is_not_printable_is_escaped(char: str, escaped: str) -> None:
    assert printable(f"a{char}b") == f"a{escaped}b"


def test_an_escape_above_u_ffff_reads_back_as_the_same_character() -> None:
    assert json.loads(f'"{printable(chr(0xE0041))}"') == chr(0xE0041)
