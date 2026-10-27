# SPDX-License-Identifier: MIT
"""The provider adapter for OpenAI-compatible endpoints: hosted providers, and local servers such as Ollama,
llama.cpp and vLLM, which serve the same `POST {base_url}/chat/completions`.

The request asks for structured output (`response_format` with the JSON schema), at temperature 0. A server
that ignores the schema still gets its answer parsed strictly by `extract.parse_output`.

Errors never quote the request or the response: both hold the post.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from stockthread.llm.extract import MAX_OUTPUT_CHARS, NoReading, ProviderError

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 1_000_000


class OpenAICompatible:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: SecretStr | None = None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url = check_endpoint(base_url, model, api_key)
        self._model = model
        self._api_key = api_key
        # trust_env off: a proxy from the environment would see the post, and the key of a local server.
        self._client = httpx.Client(
            timeout=timeout_seconds, transport=transport, follow_redirects=False, trust_env=False
        )

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        self._client.close()

    def complete(self, system: str, user: str, schema: Mapping[str, Any]) -> str:
        result = self._post(request_body(self._model, system, user, schema))
        if isinstance(result, str):
            # Raised here, outside every handler: an exception kept as context would hold the request, with
            # the post and the key.
            raise ProviderError(result)
        return _content(result)

    def _post(self, body: Mapping[str, Any]) -> bytes | str:
        """The response body, or what went wrong as a sentence that quotes nothing."""
        headers = {}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key.get_secret_value()}"
        try:
            with self._client.stream("POST", self._url, json=body, headers=headers) as response:
                if response.status_code != 200:
                    return f"the provider answered HTTP {response.status_code}"
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw += chunk
                    if len(raw) > MAX_RESPONSE_BYTES:
                        return "the provider's answer is too long"
                return bytes(raw)
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError, ValueError, TypeError) as error:
            return f"{type(error).__name__} reaching the provider"


def request_body(model: str, system: str, user: str, schema: Mapping[str, Any]) -> dict[str, Any]:
    """Everything the endpoint is sent for one call. A recorded answer is an answer to exactly this
    (`llm.record.request_hash`)."""
    return {
        "model": model,
        "temperature": 0,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "extraction", "strict": True, "schema": schema},
        },
    }


def check_endpoint(base_url: str, model: str, api_key: SecretStr | None) -> httpx.URL:
    """The URL that is called, or a ValueError that names what is wrong and never quotes a value."""
    try:
        url = urlsplit(base_url)
    except ValueError:  # its message can quote the URL
        url = urlsplit("")
    if url.scheme not in ("http", "https") or not url.hostname or "@" in url.netloc:
        raise ValueError("the provider URL must be http(s)://host[:port][/path], without credentials")
    try:
        port = url.port
    except ValueError:
        port = 0
    if port == 0:
        raise ValueError("the provider URL has an invalid port")
    if url.path.rstrip("/").endswith("/chat/completions"):
        raise ValueError("the provider URL is the base URL, without /chat/completions")
    if url.query or url.fragment or not base_url.isascii() or not base_url.isprintable() or " " in base_url:
        raise ValueError("the provider URL must be http(s)://host[:port][/path], without a query")
    if url.scheme == "http" and api_key is not None and not is_local(url.hostname):
        raise ValueError("an API key is only sent over https, or to a server on this machine")
    if not model.strip() or not model.isprintable():
        raise ValueError("the model name is empty or not printable")
    key = None if api_key is None else api_key.get_secret_value()
    if key is not None and not (key.isascii() and key.isprintable() and " " not in key):
        raise ValueError("the API key must be printable ASCII without spaces")
    try:
        return httpx.URL(base_url.rstrip("/") + "/chat/completions")
    except httpx.InvalidURL:
        raise ValueError("the provider URL must be http(s)://host[:port][/path]") from None


def _content(raw: bytes) -> str:
    """The text of the first choice.

    A NoReading when the answer is about this one post and would be the same again: the model refused it, or
    its answer was cut off by a limit, filtered, or too long. Any other shape is a ProviderError, an outage
    that is tried again: a server under load can answer 200 with nothing in it.
    """
    content: Any = None
    refusal: Any = None
    finish: Any = "unread"
    try:
        answer = json.loads(raw)
        choice = answer["choices"][0]
        content = choice["message"]["content"]
        refusal = choice["message"].get("refusal")
        finish = choice.get("finish_reason")
    except (ValueError, LookupError, TypeError, AttributeError, RecursionError):
        pass
    if finish == "unread":
        raise ProviderError("the provider's answer is not a chat completion")
    if finish in ("length", "content_filter"):
        raise NoReading("the model did not finish its answer")  # cut off by a limit, or filtered
    if content is None and isinstance(refusal, str) and refusal:
        raise NoReading("the model refused the post")
    if finish not in (None, "stop"):
        raise ProviderError("the model did not finish its answer")
    if not isinstance(content, str) or not content:
        raise ProviderError("the model gave no answer")
    if len(content) > MAX_OUTPUT_CHARS:
        raise NoReading("the model's answer is too long")
    return content


def is_local(host: str) -> bool:
    """`host` is this machine: "localhost" or a loopback address."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
