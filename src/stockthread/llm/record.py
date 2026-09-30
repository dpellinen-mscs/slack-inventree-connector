"""Record a model's answers to the eval corpus, so they are scored later without a provider.

    LLM_BASE_URL=... LLM_MODEL=... LLM_API_KEY=... python -m stockthread.llm.record eval --out FILE

The corpus is synthetic: recording sends no real post. A recording holds, for every case, the model's raw
answer (or the sentence that says why there was none) and a hash of the request that produced it. When the
prompt, the schema or the masking changes, the hash no longer fits and the case must be recorded again:
running the command on an existing file keeps the answers that still fit and asks only for the rest.

`replay` reads a recording back through the same `LLMExtractor` the worker uses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from stockthread.core.model import Extraction, MessageInput
from stockthread.llm.extract import (
    OUTPUT_SCHEMA,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    LLMExtractor,
    Provider,
    ProviderError,
    WithinDeadline,
    user_prompt,
)
from stockthread.llm.openai_compatible import DEFAULT_TIMEOUT_SECONDS, OpenAICompatible

FORMAT = 1


def corpus_cases(corpus: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted((corpus / "cases").glob("*.json")):
        cases.extend(json.loads(path.read_text(encoding="utf-8"))["cases"])
    return cases


def corpus_messages(post: Mapping[str, Any]) -> list[MessageInput]:
    """The post as ingest hands it over: bot messages are filtered out before extraction."""
    return [
        MessageInput(ts=m["ts"], text=m.get("text", ""), file_ids=tuple(f["id"] for f in m.get("files", ())))
        for m in post["messages"]
        if not m.get("is_bot")
    ]


def request_hash(messages: Sequence[MessageInput]) -> str:
    """Of everything the model is sent for this post: the prompt, the post as it is sent, and the schema."""
    sent = [SYSTEM_PROMPT, user_prompt([message for message in messages if not message.deleted]), OUTPUT_SCHEMA]
    return hashlib.sha256(json.dumps(sent, sort_keys=True).encode()).hexdigest()


class _Listening:
    """Passes a call through and keeps the raw answer."""

    def __init__(self, provider: Provider) -> None:
        self._provider = provider
        self.model = provider.model
        self.heard: str | None = None

    def complete(self, system: str, user: str, schema: Mapping[str, Any]) -> str:
        self.heard = self._provider.complete(system, user, schema)
        return self.heard


class _Recorded:
    """Answers with what was recorded for one case."""

    def __init__(self, model: str, answer: Mapping[str, Any]) -> None:
        self.model = model
        self._answer = answer

    def complete(self, system: str, user: str, schema: Mapping[str, Any]) -> str:
        output = self._answer.get("output")
        if not isinstance(output, str):
            raise ProviderError(str(self._answer.get("error", "no answer was recorded")))
        return output


def record(
    cases: Sequence[Mapping[str, Any]],
    provider: Provider,
    corpus_version: str,
    earlier: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask the model for every case that `earlier` holds no fitting answer for."""
    kept: dict[str, Mapping[str, Any]] = {}
    if earlier and (earlier.get("model"), earlier.get("format")) == (provider.model, FORMAT):
        kept = {answer["id"]: answer for answer in earlier.get("answers", []) if "output" in answer}
    answers: list[Mapping[str, Any]] = []
    for case in cases:
        messages = corpus_messages(case["post"])
        request = request_hash(messages)
        known = kept.get(case["id"])
        if known is not None and known.get("request_sha256") == request:
            answers.append(known)
            continue
        listening = _Listening(provider)
        answer: dict[str, Any] = {"id": case["id"], "request_sha256": request}
        try:
            LLMExtractor(listening).extract(messages)
            answer["output"] = listening.heard  # None: a post with no message was not sent
        except ProviderError as error:
            answer["error"] = str(error)
        answers.append(answer)
    return {
        "format": FORMAT,
        "corpus_version": corpus_version,
        "prompt_version": PROMPT_VERSION,
        "model": provider.model,
        "answers": answers,
    }


def replay(recording: Mapping[str, Any], case: Mapping[str, Any]) -> Extraction:
    """The extraction the worker would get for this case from the recorded answer.

    Raises LookupError when the recording has no answer for the request as it is sent today (the prompt, the
    schema or the masking changed since), and ProviderError when the provider gave none.
    """
    messages = corpus_messages(case["post"])
    answer = next((answer for answer in recording["answers"] if answer["id"] == case["id"]), None)
    if answer is None or answer.get("request_sha256") != request_hash(messages):
        raise LookupError(f"case {case['id']}: no recorded answer fits the request; record again")
    return LLMExtractor(_Recorded(str(recording["model"]), answer)).extract(messages)


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] = os.environ) -> int:
    parser = argparse.ArgumentParser(prog="python -m stockthread.llm.record", description=__doc__.splitlines()[0])
    parser.add_argument("corpus", type=Path, help="the eval directory (cases/ and VERSION)")
    parser.add_argument("--out", type=Path, required=True, help="the recording; an existing one is continued")
    parser.add_argument("--timeout", type=float, default=2 * DEFAULT_TIMEOUT_SECONDS, help="seconds for one call")
    args = parser.parse_args(argv)
    base_url, model, key = environ.get("LLM_BASE_URL", ""), environ.get("LLM_MODEL", ""), environ.get("LLM_API_KEY")
    if not base_url or not model:
        print("ERROR set LLM_BASE_URL and LLM_MODEL (and LLM_API_KEY if the endpoint needs one)", file=sys.stderr)
        return 2
    try:
        endpoint = OpenAICompatible(base_url, model, SecretStr(key) if key else None, timeout_seconds=args.timeout)
    except ValueError as error:
        print(f"ERROR {error}", file=sys.stderr)
        return 2
    earlier = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else None
    version = (args.corpus / "VERSION").read_text(encoding="utf-8").strip()
    try:
        recording = record(corpus_cases(args.corpus), WithinDeadline(endpoint, args.timeout), version, earlier)
    finally:
        endpoint.close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(recording, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    failed = [answer for answer in recording["answers"] if "error" in answer]
    print(f"{len(recording['answers'])} cases, {len(failed)} without an answer -> {args.out}")
    for sentence in sorted({answer["error"] for answer in failed}):
        print(f"  {sentence}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
