"""The domain model: invariants, keys, hashes, and agreement with the eval corpus and the settings."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, get_args

import pytest

from stockthread import config
from stockthread.core.model import (
    CandidateMovement,
    Flag,
    LineItem,
    Match,
    MatchRule,
    MatchStatus,
    MessageInput,
    MovementType,
    Operation,
    PostFlag,
    PostKey,
    ReasonCode,
    ReasonSource,
    RequestKey,
    input_hash,
    operation_for,
    reason_for_flag,
)

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "eval" / "schema"


def schema_enum(name: str) -> set[str]:
    (path,) = SCHEMA_DIR.glob("*.json")
    definitions: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["$defs"]
    return set(definitions[name]["enum"])


def test_vocabularies_match_the_eval_corpus() -> None:
    assert {member.value for member in MovementType} == schema_enum("movement_type")
    assert {member.value for member in Flag} == schema_enum("flag")
    assert {member.value for member in PostFlag} == schema_enum("post_flag")


def test_settings_accept_every_movement_type_but_unclear() -> None:
    assert set(get_args(config.MovementType)) == {member.value for member in MovementType} - {"unclear"}


@pytest.mark.parametrize(
    ("movement_type", "operation"),
    [
        (MovementType.TAKE_TO_VEHICLE, Operation.TRANSFER),
        (MovementType.TAKE_TO_SITE, Operation.TRANSFER),
        (MovementType.RETURN, Operation.TRANSFER),
        (MovementType.TRANSFER, Operation.TRANSFER),
        (MovementType.CONSUME, Operation.REMOVE),
        (MovementType.RECEIVE, Operation.ADD),
        (MovementType.COUNT, Operation.COUNT),
        (MovementType.UNCLEAR, None),
    ],
)
def test_operation_for(movement_type: MovementType, operation: Operation | None) -> None:
    assert operation_for(movement_type) == operation


def test_consume_can_be_a_transfer_by_policy() -> None:
    assert operation_for(MovementType.CONSUME, consume_as_transfer=True) == Operation.TRANSFER
    assert operation_for(MovementType.RETURN, consume_as_transfer=True) == Operation.TRANSFER


def test_every_flag_is_a_review_reason_from_the_model() -> None:
    for flag in [*Flag, PostFlag.SUSPICIOUS_INSTRUCTIONS]:
        reason = reason_for_flag(flag)
        assert reason.code.value == flag.value
        assert reason.source is ReasonSource.MODEL
    with pytest.raises(ValueError, match="not a review reason"):
        reason_for_flag(PostFlag.CONTAINS_SENSITIVE)


def test_keys() -> None:
    post = PostKey("T0ACME01", "C0STOCK1", "1727600000.000100")
    assert str(post) == "T0ACME01/C0STOCK1/1727600000.000100"
    assert str(RequestKey(post, 2, 1)) == "T0ACME01/C0STOCK1/1727600000.000100/r2/1"
    assert RequestKey(post, 1, 2) < RequestKey(post, 2, 1)
    assert {PostKey("T0ACME01", "C0STOCK1", "1.000100"), PostKey("T0ACME01", "C0STOCK1", "1.000100")} == {
        PostKey("T0ACME01", "C0STOCK1", "1.000100")
    }


@pytest.mark.parametrize(("workspace", "channel", "ts"), [("", "C1", "1.0"), ("T1", "", "1.0"), ("T1", "C/1", "1.0")])
def test_post_key_refuses_bad_parts(workspace: str, channel: str, ts: str) -> None:
    with pytest.raises(ValueError, match="post key"):
        PostKey(workspace, channel, ts)


@pytest.mark.parametrize(("revision", "sequence"), [(0, 1), (1, 0), (-1, 1)])
def test_request_key_counts_from_one(revision: int, sequence: int) -> None:
    with pytest.raises(ValueError, match="start at 1"):
        RequestKey(PostKey("T1", "C1", "1.0"), revision, sequence)


def test_input_hash_ignores_order_and_sees_every_change() -> None:
    first = MessageInput("1.000100", "2 Disc 40 on my truck")
    photo = MessageInput("1.000200", "", file_ids=("F0PHOTO1",))
    base = input_hash([first, photo])
    assert input_hash([photo, first]) == base
    changed = [
        [first],
        [MessageInput("1.000100", "3 Disc 40 on my truck"), photo],
        [MessageInput("1.000100", "2 Disc 40 on my truck", edited_ts="1.000300"), photo],
        [MessageInput("1.000100", "2 Disc 40 on my truck", deleted=True), photo],
        [first, MessageInput("1.000200", "", file_ids=("F0PHOTO2",))],
        [first, photo, MessageInput("1.000400", "to van 3 actually")],
    ]
    hashes = {input_hash(messages) for messages in changed}
    assert len(hashes) == len(changed)
    assert base not in hashes


def test_input_hash_is_stable_across_runs() -> None:
    # A stored hash must still match after a restart or an upgrade. Change this value only on purpose.
    expected = "67f2874989179caef49041f80d0086108b63304ea8591cb69e8b9ea014262e15"
    assert input_hash([MessageInput("1.000100", "2 ONTs to Sam\u2019s truck")]) == expected


def test_line_item_invariants() -> None:
    LineItem("Disc 40", Decimal(0), None)  # a count can be zero
    LineItem(None, None)
    for bad in (Decimal(-1), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError, match="quantity"):
            LineItem("Disc 40", bad)
    for bad in (Decimal(0), Decimal(-4)):
        with pytest.raises(ValueError, match="pack size"):
            LineItem("patch cable", Decimal(1), "box", pack_size=bad)
    with pytest.raises(ValueError, match="twice"):
        LineItem("ONT", Decimal(2), serials=("SN-1", "SN-1"))


def test_candidate_movement_is_frozen() -> None:
    movement = CandidateMovement(MovementType.TAKE_TO_VEHICLE, (LineItem("ONT", Decimal(2)),), None, "my truck")
    assert movement.flags == frozenset()
    with pytest.raises(AttributeError):
        movement.destination_text = "van 3"  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": MatchStatus.MATCHED},
        {"status": MatchStatus.MATCHED, "record": 7},
        {"status": MatchStatus.MATCHED, "rule": MatchRule.EXACT},
        {"status": MatchStatus.MATCHED, "rule": MatchRule.EXACT, "record": 7, "candidates": (7, 8)},
        {"status": MatchStatus.AMBIGUOUS, "candidates": (7,)},
        {"status": MatchStatus.AMBIGUOUS, "candidates": (7, 7)},
        {"status": MatchStatus.AMBIGUOUS, "record": 7, "candidates": (7, 8)},
        {"status": MatchStatus.UNMATCHED, "candidates": (7,)},
        {"status": MatchStatus.UNMATCHED, "rule": MatchRule.FUZZY},
        {"status": MatchStatus.UNMATCHED, "record": 7},
    ],
)
def test_impossible_match_results_cannot_be_built(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"match|result|candidate"):
        Match(**kwargs)


@pytest.mark.parametrize("rule", [MatchRule.EXACT, MatchRule.NORMALIZED, MatchRule.ALIAS])
def test_a_deterministic_match_can_authorize_a_write(rule: MatchRule) -> None:
    match = Match(MatchStatus.MATCHED, rule=rule, record=7)
    assert match.can_authorize_a_write
    assert match.review_reason(kind="part") is None


@pytest.mark.parametrize(
    ("match", "kind", "code"),
    [
        (Match(MatchStatus.MATCHED, rule=MatchRule.FUZZY, record=7), "part", ReasonCode.FUZZY_MATCH),
        (Match(MatchStatus.MATCHED, rule=MatchRule.WORDS, record=7), "part", ReasonCode.FUZZY_MATCH),
        (Match(MatchStatus.AMBIGUOUS, rule=MatchRule.FUZZY, candidates=(7, 8)), "part", ReasonCode.PART_AMBIGUOUS),
        (
            Match(MatchStatus.AMBIGUOUS, rule=MatchRule.ALIAS, candidates=(7, 8)),
            "location",
            ReasonCode.LOCATION_AMBIGUOUS,
        ),
        (Match(MatchStatus.UNMATCHED), "part", ReasonCode.PART_UNMATCHED),
        (Match(MatchStatus.UNMATCHED), "location", ReasonCode.LOCATION_UNMATCHED),
    ],
)
def test_every_other_match_is_a_review_reason(
    match: Match, kind: Literal["part", "location"], code: ReasonCode
) -> None:
    assert not match.can_authorize_a_write
    reason = match.review_reason(kind=kind)
    assert reason is not None
    assert reason.code is code
    assert reason.source is ReasonSource.RULE


def test_only_exact_normalized_alias_and_answer_authorize_a_write() -> None:
    authorizing = {rule for rule in MatchRule if Match(MatchStatus.MATCHED, rule=rule, record=7).can_authorize_a_write}
    assert authorizing == {MatchRule.EXACT, MatchRule.NORMALIZED, MatchRule.ALIAS, MatchRule.ANSWER}
