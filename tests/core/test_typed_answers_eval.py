"""The typed-answer eval set (`eval/typed_answers.json`): every reply reads as the hand-reviewed answer, or as none."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from corpus import EVAL, eval_catalog

from stockthread.core.model import CandidateMovement, LineItem, MovementType, ReasonCode
from stockthread.core.questions import Ask, Question, Side
from stockthread.core.typed_answers import TypedAnswer, read_typed_answer

SET = json.loads((EVAL / "typed_answers.json").read_text(encoding="utf-8"))
CATALOG, IPN, PATH = eval_catalog()
DEFAULT_POSTER = "U0SAM0001"


def answer_to(case: dict[str, Any]) -> TypedAnswer | None:
    ask = Ask(case["ask"])
    kind = MovementType.COUNT if case.get("count") else MovementType.TAKE_TO_VEHICLE
    quantity = Decimal(case["quantity"]) if "quantity" in case else None
    line = LineItem("Disc 40", quantity, case.get("unit"))
    movement = CandidateMovement(kind, (line,), "main stock", "my truck")
    side: Side | None = "destination" if ask is Ask.NAME_LOCATION else None
    question = Question(ask, ReasonCode.QUANTITY_MISSING, 0, 0, side)
    return read_typed_answer(case["text"], question, movement, CATALOG, case.get("poster", DEFAULT_POSTER))


def reading(answer: TypedAnswer | None, ask: Ask) -> object:
    """The answer in the set's terms: a quantity string, a status with IPNs or paths, or the serials."""
    if answer is None:
        return None
    if ask is Ask.QUANTITY:
        assert answer.quantity is not None
        return str(answer.quantity)
    if ask is Ask.SERIALS:
        return list(answer.serials)
    assert answer.match is not None
    names = IPN if ask is Ask.NAME_PART else PATH
    match = answer.match
    pks = list(match.candidates) if match.record is None else [match.record]
    return {"status": match.status.value, "records": [names[pk] for pk in pks]}


def test_the_set_is_well_formed() -> None:
    ids = [case["id"] for case in SET["cases"]]
    assert len(ids) == len(set(ids))
    assert {case["ask"] for case in SET["cases"]} == {"quantity", "name_part", "name_location", "serials"}
    assert any(case["expect"] is None for case in SET["cases"])


@pytest.mark.parametrize("case", SET["cases"], ids=[case["id"] for case in SET["cases"]])
def test_each_reply_reads_as_reviewed(case: dict[str, Any]) -> None:
    assert reading(answer_to(case), Ask(case["ask"])) == case["expect"], case.get("why", "")
