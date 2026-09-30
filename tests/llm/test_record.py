"""Recording a model's answers to the corpus, and reading a recording back."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from stockthread.llm import record as record_module
from stockthread.llm.extract import PROMPT_VERSION, ProviderError
from stockthread.llm.openai_compatible import request_body
from stockthread.llm.record import continues, corpus_cases, main, record, replay

EVAL = Path(__file__).resolve().parents[2] / "eval"
NOTHING = json.dumps({"readable": True, "movements": [], "post_flags": []})
CASES = [
    {"id": "one", "post": {"messages": [{"ts": "1790000000.000100", "text": "gave truck 1 two Disc 40"}]}},
    {"id": "two", "post": {"messages": [{"ts": "1790000001.000100", "text": "lunch?", "files": [{"id": "F1"}]}]}},
    {"id": "bot", "post": {"messages": [{"ts": "1790000002.000100", "text": "Dry run", "is_bot": True}]}},
]


class FakeProvider:
    model = "test-model"

    def __init__(self, *answers: str | ProviderError) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    def complete(self, system: str, user: str, schema: Mapping[str, Any]) -> str:
        self.asked.append(user)
        answer = self.answers.pop(0)
        if isinstance(answer, ProviderError):
            raise answer
        return answer


def test_a_recording_holds_the_raw_answer_of_every_case_and_reads_back_as_the_worker_would() -> None:
    provider = FakeProvider(NOTHING, "not json")
    recording = record(CASES, provider, "9.9.9")
    assert (recording["corpus_version"], recording["prompt_version"], recording["model"]) == (
        "9.9.9",
        PROMPT_VERSION,
        "test-model",
    )
    assert [answer["id"] for answer in recording["answers"]] == ["one", "two", "bot"]
    assert [answer["output"] for answer in recording["answers"]] == [NOTHING, "not json", None]
    assert len(provider.asked) == 2  # a post with only a bot's message is not sent
    assert json.loads(provider.asked[1]) == [{"text": "lunch?", "files": 1}]
    recording = json.loads(json.dumps(recording))
    one, two, bot = (replay(recording, case) for case in CASES)
    assert (one.extractor, one.declined, one.movements) == (f"{PROMPT_VERSION}/test-model", False, ())
    assert two.declined  # output that does not fit the schema
    assert not bot.declined


def test_a_provider_error_is_recorded_as_its_sentence_and_asked_again_on_the_next_run() -> None:
    first = record(CASES, FakeProvider(NOTHING, ProviderError("the provider answered HTTP 503")), "9.9.9")
    assert first["answers"][1] == {
        "id": "two",
        "request_sha256": first["answers"][1]["request_sha256"],
        "error": "the provider answered HTTP 503",
    }
    with pytest.raises(ProviderError, match="HTTP 503"):
        replay(first, CASES[1])
    again = FakeProvider(NOTHING)
    second = record(CASES, again, "9.9.9", earlier=first)
    assert len(again.asked) == 1  # only the case without an answer
    assert [answer.get("output") for answer in second["answers"]] == [NOTHING, NOTHING, None]


def test_an_answer_to_another_request_or_from_another_model_is_not_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    first = record(CASES, FakeProvider(NOTHING, NOTHING), "9.9.9")
    changed = [{**CASES[0], "post": {"messages": [{"ts": "1790000000.000100", "text": "gave truck 1 three"}]}}]
    with pytest.raises(LookupError, match="record again"):
        replay(first, changed[0])
    with pytest.raises(LookupError, match="record again"):
        replay(first, {"id": "unknown", "post": {"messages": []}})
    provider = FakeProvider(NOTHING)
    record(changed, provider, "9.9.9", earlier=first)
    assert len(provider.asked) == 1

    with pytest.raises(LookupError, match="record again"):
        replay({**first, "model": "other-model"}, CASES[0])  # the hash covers the model's name

    def with_a_limit(model: str, system: str, user: str, schema: Mapping[str, Any]) -> dict[str, Any]:
        return {**request_body(model, system, user, schema), "max_tokens": 100}

    with monkeypatch.context() as patched:  # and every parameter the adapter sends
        patched.setattr(record_module, "request_body", with_a_limit)
        with pytest.raises(LookupError, match="record again"):
            replay(first, CASES[0])
    replay(first, CASES[0])

    monkeypatch.setattr(record_module, "SYSTEM_PROMPT", "a new prompt")
    with pytest.raises(LookupError, match="record again"):
        replay(first, CASES[0])


def test_the_command_needs_an_endpoint_and_never_prints_the_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "recording.json"
    assert main([str(EVAL), "--out", str(out)], {}) == 2
    assert "LLM_BASE_URL" in capsys.readouterr().err
    environ = {"LLM_BASE_URL": "http://llm.acme-fiber.example/v1", "LLM_MODEL": "m", "LLM_API_KEY": "key-synthetic"}
    assert main([str(EVAL), "--out", str(out)], environ) == 2
    printed = capsys.readouterr()
    assert "only sent over https" in printed.err
    assert "key-synthetic" not in printed.err + printed.out
    assert not out.exists()


def test_the_command_records_the_whole_corpus_and_continues_a_recording(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = corpus_cases(EVAL)
    assert len(cases) > 100
    built: list[FakeProvider] = []

    class Endpoint(FakeProvider):
        def __init__(self, base_url: str, model: str, key: Any, timeout_seconds: float) -> None:
            down = ProviderError("the provider answered HTTP 429")
            super().__init__(*([NOTHING] * 3), *([down] * len(cases)))
            if built:
                self.answers = [NOTHING] * len(cases)
            self.model = model
            built.append(self)

        def close(self) -> None:
            pass

    monkeypatch.setattr(record_module, "OpenAICompatible", Endpoint)
    out = tmp_path / "recorded" / "recording.json"
    environ = {"LLM_BASE_URL": "http://localhost:11434/v1", "LLM_MODEL": "small-1"}
    assert main([str(EVAL), "--out", str(out)], environ) == 1
    assert "HTTP 429" in capsys.readouterr().out
    assert main([str(EVAL), "--out", str(out)], environ) == 0
    assert len(built[0].asked) == len(built[1].asked) + 3
    recording = json.loads(out.read_text(encoding="utf-8"))
    assert recording["model"] == "small-1"
    assert all(not replay(recording, case).declined for case in cases)


@pytest.mark.parametrize(
    ("held", "why"),
    [
        (json.dumps({"format": 1, "model": "small-1", "answers": [{"id": "kept"}]}), "another model"),
        (json.dumps({"format": 0, "model": "small-2", "answers": []}), "another format"),
        ('{"format": 1, "model": "small-2", "answers": [{"id": "cut', "not a recording"),
        (json.dumps([{"id": "one"}]), "not a recording"),
        (json.dumps({"format": 1, "model": "small-2", "answers": ["one"]}), "not a recording"),
        (json.dumps({"format": 1, "model": "small-2"}), "not a recording"),
    ],
)
def test_a_file_that_is_not_this_models_recording_is_refused_and_left_as_it_is(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, held: str, why: str
) -> None:
    class Endpoint(FakeProvider):
        def __init__(self, base_url: str, model: str, key: Any, timeout_seconds: float) -> None:
            super().__init__()
            self.model = model

        def close(self) -> None:
            pass

    monkeypatch.setattr(record_module, "OpenAICompatible", Endpoint)
    out = tmp_path / "small-1.json"
    out.write_text(held, encoding="utf-8")
    environ = {"LLM_BASE_URL": "http://localhost:11434/v1", "LLM_MODEL": "small-2"}
    assert main([str(EVAL), "--out", str(out)], environ) == 2
    printed = capsys.readouterr()
    assert printed.err.startswith("ERROR ")
    assert why in printed.err
    assert out.read_text(encoding="utf-8") == held


def test_continues_names_no_reason_for_the_same_model() -> None:
    recording = record(CASES, FakeProvider(NOTHING, NOTHING), "9.9.9")
    assert continues(recording, "test-model") is None
    assert continues(recording, "other-model") is not None
