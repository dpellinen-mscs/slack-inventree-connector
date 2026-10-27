# SPDX-License-Identifier: MIT
"""The OpenAI-compatible adapter against a fake server: the request it sends, and every answer it refuses."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from stockthread.llm.extract import MAX_OUTPUT_CHARS, OUTPUT_SCHEMA, NoReading, ProviderError
from stockthread.llm.openai_compatible import MAX_RESPONSE_BYTES, OpenAICompatible

KEY = "sk-synthetic-key-for-tests"


def completion(content: Any = '{"ok": true}', finish: str | None = "stop") -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}]}


def provider(handler: Any, base_url: str = "https://llm.example.test/v1", key: str | None = KEY) -> OpenAICompatible:
    return OpenAICompatible(
        base_url, "test-model", SecretStr(key) if key else None, transport=httpx.MockTransport(handler)
    )


def test_the_request_asks_for_structured_output_at_temperature_zero() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=completion())

    assert provider(handler, "https://llm.example.test/v1/").complete("system words", "user words", OUTPUT_SCHEMA)
    (request,) = seen
    assert str(request.url) == "https://llm.example.test/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body == {
        "model": "test-model",
        "temperature": 0,
        "messages": [{"role": "system", "content": "system words"}, {"role": "user", "content": "user words"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "extraction", "strict": True, "schema": OUTPUT_SCHEMA},
        },
    }


def test_a_local_server_needs_no_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(200, json=completion("{}"))

    assert provider(handler, "http://llm:11434/v1", key=None).complete("s", "u", {}) == "{}"


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(401, json={"error": {"message": "bad key sk-synthetic-key-for-tests"}}),
        httpx.Response(429, text="slow down"),
        httpx.Response(500, text="the post said: 2 Disc 40"),
        httpx.Response(302, headers={"Location": "https://elsewhere.example.test/"}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": None}]}),
        httpx.Response(200, json=completion(None)),  # a refusal has no content
        httpx.Response(200, json=completion("")),
        httpx.Response(200, json=completion(["parts"])),
        httpx.Response(200, json=completion(finish="length")),
        httpx.Response(200, json=completion(finish="content_filter")),
        httpx.Response(200, json=completion("x" * (MAX_OUTPUT_CHARS + 1))),
        httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)),
    ],
)
def test_anything_but_a_finished_answer_is_a_provider_error_that_quotes_nothing(answer: httpx.Response) -> None:
    with pytest.raises(ProviderError) as raised:
        provider(lambda request: answer).complete("system", "2 Disc 40 on my truck", {})
    message = str(raised.value)
    assert KEY not in message
    assert "Disc" not in message
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


REFUSAL = {
    "choices": [
        {"message": {"role": "assistant", "content": None, "refusal": "I cannot read that."}, "finish_reason": "stop"}
    ]
}


@pytest.mark.parametrize(
    ("answer", "about_this_post"),
    [
        (httpx.Response(200, json=REFUSAL), True),
        (httpx.Response(200, json=completion(finish="length")), True),
        (httpx.Response(200, json=completion(finish="content_filter")), True),
        (httpx.Response(200, json=completion("x" * (MAX_OUTPUT_CHARS + 1))), True),
        (httpx.Response(429, text="slow down"), False),
        (httpx.Response(400, json=completion()), False),
        (httpx.Response(200, text="not json"), False),
        (httpx.Response(200, json={"choices": []}), False),
        (httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)), False),
        # 200 with nothing in it, or an ending nobody asked for: a server in trouble, not an answer about the post.
        (httpx.Response(200, json=completion(None)), False),
        (httpx.Response(200, json=completion("")), False),
        (httpx.Response(200, json=completion(["parts"])), False),
        (httpx.Response(200, json=completion(finish="tool_calls")), False),
        (httpx.Response(200, json=completion(finish="error")), False),
    ],
)
def test_an_answer_that_is_no_reading_of_the_post_is_told_apart_from_an_outage(
    answer: httpx.Response, about_this_post: bool
) -> None:
    with pytest.raises(ProviderError) as raised:
        provider(lambda request: answer).complete("system", "2 Disc 40 on my truck", {})
    assert isinstance(raised.value, NoReading) is about_this_post
    assert "cannot read" not in str(raised.value)  # the model's own words may quote the post


def test_an_error_status_is_refused_even_with_a_well_formed_body() -> None:
    with pytest.raises(ProviderError, match="HTTP 503"):
        provider(lambda request: httpx.Response(503, json=completion())).complete("s", "u", {})


def test_a_redirect_is_not_followed() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://elsewhere.example.test/v1/chat/completions"})

    with pytest.raises(ProviderError, match="HTTP 307"):
        provider(handler).complete("s", "u", {})
    assert seen == ["https://llm.example.test/v1/chat/completions"]


@pytest.mark.parametrize("body", [b"[" * 200_000, b'{"choices": ' + b"[" * 200_000])
def test_a_deeply_nested_answer_is_a_provider_error(body: bytes) -> None:
    with pytest.raises(ProviderError):
        provider(lambda request: httpx.Response(200, content=body)).complete("s", "u", {})


def test_text_that_cannot_be_encoded_is_a_provider_error_that_keeps_nothing() -> None:
    with pytest.raises(ProviderError) as raised:
        provider(lambda request: httpx.Response(200, json=completion())).complete("s", "took \ud83d 2 Disc 40", {})
    assert raised.value.__context__ is None
    assert "Disc" not in str(raised.value)


def test_the_key_is_not_kept_in_plain_text_and_the_environment_names_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example.test:3128")
    made = provider(lambda request: httpx.Response(200, json=completion()))
    assert KEY not in repr(vars(made))
    assert made._client.trust_env is False


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_a_network_failure_is_a_provider_error(error: type[httpx.HTTPError]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error("synthetic 2 Disc 40")

    with pytest.raises(ProviderError) as raised:
        provider(handler).complete("system", "user", {})
    assert str(raised.value) == f"{error.__name__} reaching the provider"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://llm.example.test/v1",  # a key over plain http to another machine
        "http://10.0.0.5:8000/v1",
        "ftp://llm.example.test/v1",
        "https://user:pass@llm.example.test/v1",
        "llm.example.test/v1",
        "https:///v1",
        "http://localhost.example.test/v1",
        "http://127.0.0.1.example.test/v1",
        "https://llm.example.test/v1?api-version=1",
        "https://llm.example.test/v1#part",
        "http://local\thost/v1",
        "https://llm.example.test/v 1",
    ],
)
def test_a_url_that_would_expose_the_key_is_refused(base_url: str) -> None:
    with pytest.raises(ValueError, match=r"http"):
        provider(lambda request: httpx.Response(200), base_url)


@pytest.mark.parametrize("base_url", ["http://localhost:11434/v1", "http://127.0.0.1:8080/v1", "http://[::1]:8080/v1"])
def test_a_key_may_go_over_http_to_this_machine(base_url: str) -> None:
    assert provider(lambda request: httpx.Response(200, json=completion("{}")), base_url).complete("s", "u", {})


@pytest.mark.parametrize("model", ["", "model\n"])
def test_a_model_name_must_be_printable(model: str) -> None:
    with pytest.raises(ValueError, match="model"):
        OpenAICompatible("https://llm.example.test/v1", model)


@pytest.mark.parametrize("key", ["sk-clé-synthetic", "sk synthetic", "sk-synthetic\n"])
def test_a_key_that_cannot_be_a_header_is_refused_without_quoting_it(key: str) -> None:
    with pytest.raises(ValueError, match="API key") as raised:
        OpenAICompatible("https://llm.example.test/v1", "test-model", SecretStr(key))
    assert key not in str(raised.value)
