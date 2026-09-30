"""Record a model's answers to the eval corpus, so they are scored later without a provider.

    LLM_BASE_URL=... LLM_MODEL=... LLM_API_KEY=... python -m stockthread.llm.record eval --out FILE

The corpus is synthetic: recording sends no real post. A recording holds, for every case, the model's raw
answer (or the sentence that says why there was none) and a hash of the request that produced it. When the
prompt, the schema, the masking or the request's parameters change, the hash no longer fits and the case must
be recorded again: running the command on an existing file of the same model keeps the answers that still fit
and asks only for the rest. A file that holds another model's answers is never overwritten.

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
    NoReading,
    Provider,
    ProviderError,
    WithinDeadline,
    user_prompt,
)
from stockthread.llm.openai_compatible import DEFAULT_TIMEOUT_SECONDS, OpenAICompatible, request_body

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


def request_hash(messages: Sequence[MessageInput], model: str) -> str:
    """Of the request body the endpoint is sent for this post: the model's name, the prompt, the post as it is
    sent, the schema, and the parameters the adapter adds. Not the endpoint: which server a model name means
    is for the person who records to keep the same."""
    live = [message for message in messages if not message.deleted]
    sent = request_body(model, SYSTEM_PROMPT, user_prompt(live), OUTPUT_SCHEMA)
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
            kind = NoReading if self._answer.get("no_reading") else ProviderError
            raise kind(str(self._answer.get("error", "no answer was recorded")))
        return output


def record(
    cases: Sequence[Mapping[str, Any]],
    provider: Provider,
    corpus_version: str,
    earlier: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask the model for every case that `earlier` holds no fitting answer for.

    `earlier` must be a recording of the same model in the same format (`continues`)."""
    kept: dict[str, Mapping[str, Any]] = {}
    if earlier:
        kept = {answer["id"]: answer for answer in earlier.get("answers", []) if "output" in answer}
    answers: list[Mapping[str, Any]] = []
    for case in cases:
        messages = corpus_messages(case["post"])
        request = request_hash(messages, provider.model)
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
            if isinstance(error, NoReading):
                answer["no_reading"] = True  # the provider answered: the worker would not wait or ask again
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
    schema, the masking or a parameter changed since, or the case is new), and ProviderError when the
    provider gave none (NoReading when it answered, but with no reading of the post).
    """
    messages = corpus_messages(case["post"])
    answer = next((answer for answer in recording["answers"] if answer["id"] == case["id"]), None)
    if answer is None or answer.get("request_sha256") != request_hash(messages, str(recording["model"])):
        raise LookupError(f"case {case['id']}: no recorded answer fits the request; record again")
    return LLMExtractor(_Recorded(str(recording["model"]), answer)).extract(messages)


def continues(earlier: object, model: str) -> str | None:
    """Why the file at --out cannot be continued with this model, or None when it can."""
    if not isinstance(earlier, dict) or not isinstance(earlier.get("answers"), list):
        return "the file at --out is not a recording; choose another --out"
    if earlier.get("format") != FORMAT:
        return "the file at --out is a recording in another format; choose another --out"
    if earlier.get("model") != model:
        return "the file at --out holds another model's answers; choose another --out"
    if not all(isinstance(answer, dict) and isinstance(answer.get("id"), str) for answer in earlier["answers"]):
        return "the file at --out is not a recording; choose another --out"
    return None


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
    earlier = None
    if args.out.exists():
        try:
            earlier = json.loads(args.out.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            earlier = "unreadable"
        refused = continues(earlier, model)
        if refused:  # never overwritten: it may be a whole recording that was paid for
            endpoint.close()
            print(f"ERROR {refused}", file=sys.stderr)
            return 2
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
