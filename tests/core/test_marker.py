"""The marker format of design section 7."""

import pytest

from stockthread.core.marker import ALPHABET, ID_LENGTH, AttemptKind, Marker, is_marker_id, marked, new_marker_id


def test_the_design_example_round_trips() -> None:
    marker = Marker.parse("[sic:k7m2q9x4t1b8w3n6p5r0zd:a01]")
    assert marker == Marker("k7m2q9x4t1b8w3n6p5r0zd", AttemptKind.APPLY, 1)
    assert str(marker) == "[sic:k7m2q9x4t1b8w3n6p5r0zd:a01]"


def test_a_marker_has_a_fixed_length() -> None:
    lengths = {len(str(Marker(new_marker_id(), kind, attempt))) for kind in AttemptKind for attempt in (1, 9, 99)}
    assert lengths == {len("[sic::a01]") + ID_LENGTH}


def test_new_ids_use_the_alphabet_and_differ() -> None:
    ids = {new_marker_id() for _ in range(1000)}
    assert len(ids) == 1000
    assert all(is_marker_id(value) and set(value) <= set(ALPHABET) for value in ids)


@pytest.mark.parametrize(
    "text",
    [
        "[sic:k7m2q9x4t1b8w3n6p5r0zd:a1]",  # one digit
        "[sic:k7m2q9x4t1b8w3n6p5r0zd:a00]",  # attempt 0
        "[sic:k7m2q9x4t1b8w3n6p5r0zd:x01]",  # unknown kind
        "[sic:k7m2q9x4t1b8w3n6p5r0z:a01]",  # 21 characters
        "[sic:K7M2Q9X4T1B8W3N6P5R0ZD:a01]",  # upper case
        "[sic:k7m2q9x4t1b8w3n6p5r0zu:a01]",  # u is not in the alphabet
        "note [sic:k7m2q9x4t1b8w3n6p5r0zd:a01]",  # surrounding text
        "[sic:k7m2q9x4t1b8w3n6p5r0zd:a01]\n",
    ],
)
def test_parse_refuses_anything_but_a_whole_marker(text: str) -> None:
    with pytest.raises(ValueError, match=r"marker|attempt"):
        Marker.parse(text)


@pytest.mark.parametrize("attempt", [0, 100, -1])
def test_attempt_numbers_have_two_digits(attempt: int) -> None:
    with pytest.raises(ValueError, match="attempt"):
        Marker(new_marker_id(), AttemptKind.UNDO, attempt)


ID = "k7m2q9x4t1b8w3n6p5r0zd"
OTHER = "k7m2q9x4t1b8w3n6p5r0zc"


@pytest.mark.parametrize(
    ("notes", "found"),
    [
        (
            f"[sic:{ID}:a01] U0SAM0001 https://acme-fiber.slack.com/archives/C0ACME0001/p1",
            Marker(ID, AttemptKind.APPLY, 1),
        ),
        (f"[sic:{ID}:u02]", Marker(ID, AttemptKind.UNDO, 2)),
        (f"[sic:{ID}:a03] U0SAM0001", Marker(ID, AttemptKind.APPLY, 3)),
    ],
)
def test_marked_finds_a_whole_marker_of_the_id_leading_the_notes(notes: str, found: Marker) -> None:
    assert marked(notes, ID) == found


@pytest.mark.parametrize(
    "notes",
    [
        f"[sic:{OTHER}:a01] U0SAM0001",  # another request
        f"see [sic:{ID}:a01]",  # not first: someone quoted it
        f"[sic:{ID}:a01]x U0SAM0001",  # not a whole token
        f" [sic:{ID}:a01]",
        f"[sic:{ID}:a00]",  # not a marker: attempts start at 1
        f"sic:{ID}:a01",
        ID,
        "",
        None,
        42,
    ],
)
def test_marked_refuses_anything_else(notes: object) -> None:
    assert marked(notes, ID) is None


def test_marked_refuses_an_id_that_is_not_one() -> None:
    assert marked(f"[sic:{ID}:a01]", ID[:-1]) is None
