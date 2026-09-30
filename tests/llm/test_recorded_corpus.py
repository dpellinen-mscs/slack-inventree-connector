"""Recorded model answers against the eval corpus, scored by the reference scorer (eval/README.md).

Every file in `eval/recorded/` is one model's answers to one prompt version. Each is scored twice: the model
reading every post, and the pipeline as the worker runs it, where the model reads only what the rules
decline (ADR 0011). Zero hard failures is the bar for the pipeline. `pytest tests/llm/test_recorded_corpus.py -s`
prints the reports. With no recording the scores are skipped; the mechanism is tested with perfect answers.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from stockthread.core import extract_rules
from stockthread.core.model import Extraction
from stockthread.llm.extract import PROMPT_VERSION, ProviderError, user_prompt
from stockthread.llm.record import corpus_cases, corpus_messages, record, replay

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "eval"
RECORDINGS = sorted((EVAL / "recorded").glob("*.json"))


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _model_alone(recording: Mapping[str, Any], case: Mapping[str, Any]) -> Extraction:
    try:
        return replay(recording, case)
    except ProviderError:  # no answer: the post is left unread, with the flags found by rule, as after an outage
        by_rule = extract_rules.extract(corpus_messages(case["post"]))
        return Extraction(f"{PROMPT_VERSION}/{recording['model']}", (), by_rule.post_flags, declined=True)


def _as_the_worker(recording: Mapping[str, Any], case: Mapping[str, Any]) -> Extraction:
    by_rule = extract_rules.extract(corpus_messages(case["post"]))
    return _model_alone(recording, case) if by_rule.declined else by_rule


def reports(recording: Mapping[str, Any]) -> tuple[Any, Any]:
    """The scorer's reports: the model alone, and the rules with the model behind them."""
    helpers = _module("rules_corpus", ROOT / "tests/core/test_extract_rules_corpus.py")
    scorer = helpers._scorer()
    found = []
    for name, read in (("", _model_alone), ("rules first, then ", _as_the_worker)):
        predictions = {
            "corpus_version": recording["corpus_version"],
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
    _, pipeline = reports(recording)
    assert pipeline.false_movements == []
    assert pipeline.missed_flags == []
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
    assert alone.case_exact.as_dict() == pipeline.case_exact.as_dict()
    assert alone.detection.as_dict()["recall"] == 1.0
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
    assert pipeline.hard_failures == 0
