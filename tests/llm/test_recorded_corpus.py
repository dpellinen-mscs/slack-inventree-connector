# SPDX-License-Identifier: MIT
"""Recorded model answers against the eval corpus, scored by the reference scorer (eval/README.md).

Every file in `eval/recorded/` is one model's answers to one prompt version. Each is scored twice: the model
reading every post, and the pipeline as the worker runs it, where the model reads only what the rules
decline (ADR 0011) and the validator's rule-derived flags are added. The bar for the pipeline: zero hard
failures, the flags only a reader of the photo can raise excepted (VISION_FLAGS), with an answer recorded for
every case. A recording with a case the provider gave no answer for fails, so the rules alone never pass for a
model.
`pytest tests/llm/test_recorded_corpus.py -s` prints the reports. With no recording the scores are skipped;
the mechanism is tested with perfect answers.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from stockthread.config import PolicySettings
from stockthread.core import extract_rules
from stockthread.core.model import Extraction, Flag, MessageInput
from stockthread.core.validate import photo_not_read, validate
from stockthread.llm.extract import PROMPT_VERSION, NoReading, ProviderError, user_prompt
from stockthread.llm.record import corpus_cases, corpus_messages, record, replay

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "eval"
RECORDINGS = sorted((EVAL / "recorded").glob("*.json"))
NO_READING_EXPECTED: dict[str, frozenset[str]] = {}
"""By recording (the file's name without .json): the cases this model is known to refuse or not to finish.
They are scored as left unread. Only an answer recorded as `no_reading` can be listed; an outage never can."""


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VISION_FLAGS = frozenset({Flag.QUANTITY_FROM_PHOTO, Flag.SERIAL_UNCONFIRMED, Flag.PHOTO_TEXT_MISMATCH})
"""The flags only a reader of the photo can raise. No extractor reads one: the worker gives every movement of a post
with a photo the reason photo_not_read, which asks a person as these would. That holds only while a "yes" to
photo_not_read does not clear the questions these flags would ask (docs/design.md, section 6)."""


def _with_rule_reasons(extraction: Extraction, messages: Sequence[MessageInput]) -> Extraction:
    """The extraction with the flags the worker derives by rule from its fields and the text (validate)."""
    text = "\n".join(message.text for message in messages if not message.deleted)
    limit = PolicySettings().max_quantity
    vocabulary = {flag.value for flag in Flag}

    def derived(movement: Any) -> frozenset[Flag]:
        reasons = validate(movement, text, max_quantity=limit, post_flags=extraction.post_flags)
        return frozenset(Flag(reason.code.value) for reason in reasons if reason.code.value in vocabulary)

    return replace(extraction, movements=tuple(replace(m, flags=m.flags | derived(m)) for m in extraction.movements))


def _model_alone(recording: Mapping[str, Any], case: Mapping[str, Any]) -> Extraction:
    try:
        return replay(recording, case)
    except ProviderError:  # no answer: the post is left unread, with the flags found by rule, as after an outage
        by_rule = extract_rules.extract(corpus_messages(case["post"]))
        return Extraction(f"{PROMPT_VERSION}/{recording['model']}", (), by_rule.post_flags, declined=True)


def _as_the_worker(recording: Mapping[str, Any], case: Mapping[str, Any]) -> Extraction:
    messages = corpus_messages(case["post"])
    by_rule = extract_rules.extract(messages)
    return _with_rule_reasons(_model_alone(recording, case) if by_rule.declined else by_rule, messages)


def missed_by_the_worker(report: Any) -> list[str]:
    """The pipeline's missed flags, less the flags only a reader of the photo can raise on a post with a photo."""
    with_photo = {case["id"] for case in corpus_cases(EVAL) if photo_not_read(corpus_messages(case["post"]))}
    return [
        missed
        for missed in report.missed_flags
        if not (missed.split(" ", 1)[0] in with_photo and missed.rsplit(": ", 1)[1] in VISION_FLAGS)
    ]


def unscored(recording: Mapping[str, Any], expected: frozenset[str] = frozenset()) -> list[str]:
    """The cases the model was never scored on: the provider gave no answer, and it is not an expected refusal."""
    return sorted(
        answer["id"]
        for answer in recording["answers"]
        if "error" in answer and not (answer.get("no_reading") is True and answer["id"] in expected)
    )


def reports(recording: Mapping[str, Any]) -> tuple[Any, Any]:
    """The scorer's reports: the model alone, and the rules with the model behind them.

    Scored against the corpus as it is now: `replay` raises LookupError for a case whose request the recording
    does not answer, so a recording whose every answer still fits is as good under a later corpus version."""
    version = (EVAL / "VERSION").read_text(encoding="utf-8").strip()
    helpers = _module("rules_corpus", ROOT / "tests/core/test_extract_rules_corpus.py")
    scorer = helpers._scorer()
    found = []
    for name, read in (("", _model_alone), ("rules first, then ", _as_the_worker)):
        predictions = {
            "corpus_version": version,
            "extractor": f"{name}{recording['prompt_version']}/{recording['model']}",
            "predictions": [helpers.as_prediction(case["id"], read(recording, case)) for case in corpus_cases(EVAL)],
        }
        report = scorer.score(json.loads(json.dumps(predictions)), matching=False)
        print(scorer.render(report))
        found.append(report)
    return found[0], found[1]


@pytest.mark.skipif(not RECORDINGS, reason="no model has been recorded yet (eval/README.md)")
@pytest.mark.parametrize("path", RECORDINGS, ids=lambda path: path.stem)
def test_a_recorded_model_behind_the_rules_has_no_hard_failures_on_the_corpus(path: Path) -> None:
    recording = json.loads(path.read_text(encoding="utf-8"))
    assert recording["prompt_version"] == PROMPT_VERSION
    assert unscored(recording, NO_READING_EXPECTED.get(path.stem, frozenset())) == [], "record these again"
    _, pipeline = reports(recording)
    assert pipeline.false_movements == []
    assert missed_by_the_worker(pipeline) == []
    assert pipeline.obeyed_instructions == []


class _Perfect:
    """Answers every post with the corpus's expected extraction."""

    model = "perfect"

    def __init__(self) -> None:
        shape = _module("corpus_shape", ROOT / "tests/llm/test_corpus_shape.py")
        self._answers = {
            user_prompt(corpus_messages(case["post"])): json.dumps(shape._perfect_answer(case["expected"]))
            for case in corpus_cases(EVAL)
        }

    def complete(self, system: str, user: str, schema: Mapping[str, Any]) -> str:
        return self._answers[user]


def test_perfect_answers_recorded_and_read_back_score_full_marks() -> None:
    version = (EVAL / "VERSION").read_text(encoding="utf-8").strip()
    recording = json.loads(json.dumps(record(corpus_cases(EVAL), _Perfect(), version)))
    alone, pipeline = reports(recording)
    assert alone.hard_failures == 0
    assert alone.case_exact.as_dict()["value"] == 1.0
    assert alone.detection.as_dict()["recall"] == 1.0
    assert pipeline.detection.as_dict() == alone.detection.as_dict()
    # The rules add a reason the corpus does not expect: quantity_missing beside quantity_vague (ambig-005,
    # ambig-016), actor_not_poster (adv-006, adv-010), pack_unit_ambiguous for a length (ambig-013: the part's
    # stocking unit is not read). Extra reasons only ask more.
    assert pipeline.case_exact.as_dict()["correct"] == alone.case_exact.as_dict()["correct"] - 5
    assert pipeline.hard_failures == 0


def test_a_recording_with_no_answers_scores_as_the_rules_alone() -> None:
    version = (EVAL / "VERSION").read_text(encoding="utf-8").strip()

    class Down:
        model = "down"

        def complete(self, system: str, user: str, schema: Mapping[str, Any]) -> str:
            raise ProviderError("the provider answered HTTP 503")

    alone, pipeline = reports(record(corpus_cases(EVAL), Down(), version))
    assert alone.detection.as_dict()["recall"] == 0.0
    assert 0.6 < pipeline.detection.as_dict()["recall"] < 0.7  # the rules' own recall (design section 9)
    assert pipeline.hard_failures == 0  # which is why the gate also asks for an answer to every case


def test_a_case_without_an_answer_is_unscored_unless_it_is_an_expected_refusal() -> None:
    version = (EVAL / "VERSION").read_text(encoding="utf-8").strip()
    cases = corpus_cases(EVAL)[:3]
    ids = [case["id"] for case in cases]

    class Mixed:
        model = "mixed"

        def __init__(self) -> None:
            self.answers: list[str | Exception] = [
                ProviderError("the provider answered HTTP 429"),
                NoReading("the model did not finish its answer"),
                json.dumps({"readable": False, "movements": [], "post_flags": []}),
            ]

        def complete(self, system: str, user: str, schema: Mapping[str, Any]) -> str:
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    recording = json.loads(json.dumps(record(cases, Mixed(), version)))
    assert unscored(recording) == sorted(ids[:2])
    assert unscored(recording, frozenset(ids)) == [ids[0]]  # an outage is never expected
    with pytest.raises(NoReading):
        replay(recording, cases[1])
    with pytest.raises(ProviderError) as raised:
        replay(recording, cases[0])
    assert not isinstance(raised.value, NoReading)


def test_a_recording_is_scored_under_a_later_corpus_version_while_every_answer_fits() -> None:
    recording = json.loads(json.dumps(record(corpus_cases(EVAL), _Perfect(), "0.0.1")))
    _, pipeline = reports(recording)
    assert pipeline.hard_failures == 0
    recording["answers"].pop()  # as if a case had been added since
    with pytest.raises(LookupError, match="record again"):
        reports(recording)


def test_only_a_photo_flag_on_a_post_with_a_photo_counts_as_raised() -> None:
    missed = [
        "group-004 movement 0: serial_unconfirmed",  # a photo post: photo_not_read asks instead
        "group-004 movement 0: implied_source",  # not about the photo
        "clear-012 movement 0: quantity_from_photo",  # no photo in that post
    ]
    report = type("Report", (), {"missed_flags": missed})()
    assert missed_by_the_worker(report) == missed[1:]
