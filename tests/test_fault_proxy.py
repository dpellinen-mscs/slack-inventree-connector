# SPDX-License-Identifier: MIT
"""The fault proxy against a stub upstream: each fault happens where it says, before or after forwarding."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from typing import Any

import httpx
import pytest
from faultproxy import PROXY_ERROR_PAGE, Entry, Fault, FaultProxy, LocalServer, Rule


class _Upstream(LocalServer):
    received: list[tuple[str, str, str | None, bytes]]


class _Handler(BaseHTTPRequestHandler):
    server: _Upstream
    protocol_version = "HTTP/1.1"

    def _answer(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.received.append((self.command, self.path, self.headers.get("Authorization"), body))
        payload = json.dumps({"pk": 7, "echo": body.decode()}).encode()
        self.send_response(201 if self.command == "POST" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Upstream", "stub")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _answer

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def upstream() -> Iterator[_Upstream]:
    server = _Upstream(("127.0.0.1", 0), _Handler)
    server.received = []
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def proxy(upstream: _Upstream) -> Iterator[FaultProxy]:
    host, port = upstream.server_address[:2]
    with FaultProxy(f"http://{host!s}:{port}") as running:
        yield running


@pytest.fixture
def client(proxy: FaultProxy) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=proxy.url, headers={"Authorization": "Token test-only"}, timeout=5) as http:
        yield http


TRANSFER = "/api/stock/transfer/"


def test_passes_requests_through_unchanged(upstream: _Upstream, proxy: FaultProxy, client: httpx.Client) -> None:
    response = client.post(TRANSFER + "?x=1", json={"items": []})
    assert response.status_code == 201
    assert response.headers["X-Upstream"] == "stub"
    assert response.json() == {"pk": 7, "echo": '{"items":[]}'}
    assert upstream.received == [("POST", TRANSFER + "?x=1", "Token test-only", b'{"items":[]}')]
    assert proxy.log == [Entry("POST", TRANSFER, None, 201)]


@pytest.mark.parametrize("fault", [Fault.DROP_BEFORE, Fault.DROP_AFTER])
def test_drop_closes_without_an_answer(
    upstream: _Upstream, proxy: FaultProxy, client: httpx.Client, fault: Fault
) -> None:
    proxy.inject(Rule("POST", TRANSFER, fault))
    with pytest.raises(httpx.TransportError):
        client.post(TRANSFER, json={})
    reached = fault is Fault.DROP_AFTER
    assert len(upstream.received) == int(reached)
    assert proxy.log == [Entry("POST", TRANSFER, fault, 201 if reached else None)]


@pytest.mark.parametrize("fault", [Fault.FAIL_BEFORE, Fault.FAIL_AFTER])
def test_fail_answers_with_a_proxy_error_page(
    upstream: _Upstream, proxy: FaultProxy, client: httpx.Client, fault: Fault
) -> None:
    proxy.inject(Rule("POST", TRANSFER, fault, status=504))
    response = client.post(TRANSFER, json={})
    assert (response.status_code, response.content) == (504, PROXY_ERROR_PAGE)
    assert response.headers["Content-Type"] == "text/html"
    assert len(upstream.received) == int(fault is Fault.FAIL_AFTER)


def test_delay_times_out_the_client_after_the_server_answered(
    upstream: _Upstream, proxy: FaultProxy, client: httpx.Client
) -> None:
    proxy.inject(Rule("POST", TRANSFER, Fault.DELAY, seconds=1.0))
    with pytest.raises(httpx.ReadTimeout):
        client.post(TRANSFER, json={}, timeout=0.2)
    assert proxy.wait_for(1) == [Entry("POST", TRANSFER, Fault.DELAY, 201)]
    assert len(upstream.received) == 1


def test_hold_forwards_only_after_release_once_the_client_gave_up(
    upstream: _Upstream, proxy: FaultProxy, client: httpx.Client
) -> None:
    proxy.inject(Rule("POST", TRANSFER, Fault.HOLD))
    with pytest.raises(httpx.ReadTimeout):
        client.post(TRANSFER, json={}, timeout=0.2)
    proxy.wait_held()
    assert upstream.received == []
    assert proxy.log == []
    proxy.release()
    assert proxy.wait_for(1) == [Entry("POST", TRANSFER, Fault.HOLD, 201)]
    assert len(upstream.received) == 1


def test_a_client_still_waiting_gets_the_held_requests_answer_unchanged(
    upstream: _Upstream, proxy: FaultProxy, client: httpx.Client
) -> None:
    proxy.inject(Rule("POST", TRANSFER, Fault.HOLD))

    def release_once_held() -> None:
        proxy.wait_held()
        proxy.release()

    threading.Thread(target=release_once_held, daemon=True).start()
    response = client.post(TRANSFER, json={"notes": "n"})
    assert (response.status_code, response.json()) == (201, {"pk": 7, "echo": '{"notes":"n"}'})
    assert len(upstream.received) == 1


def test_a_request_still_held_when_the_proxy_closes_is_dropped_not_forwarded(upstream: _Upstream) -> None:
    host, port = upstream.server_address[:2]
    with FaultProxy(f"http://{host!s}:{port}") as proxy:
        proxy.inject(Rule("POST", TRANSFER, Fault.HOLD))
        with pytest.raises(httpx.TransportError):
            httpx.post(f"{proxy.url}{TRANSFER}", json={}, timeout=0.2)
        proxy.wait_held()
    assert proxy.wait_for(1) == [Entry("POST", TRANSFER, Fault.HOLD, None)]
    assert upstream.received == []


def test_garble_keeps_the_status_and_cuts_the_body(proxy: FaultProxy, client: httpx.Client) -> None:
    proxy.inject(Rule("POST", TRANSFER, Fault.GARBLE))
    response = client.post(TRANSFER, json={})
    assert response.status_code == 201
    with pytest.raises(json.JSONDecodeError):
        response.json()


def test_rules_match_method_and_path_prefix_and_run_out(proxy: FaultProxy, client: httpx.Client) -> None:
    proxy.inject(Rule("POST", "/api/stock/", Fault.FAIL_BEFORE, times=2))
    assert client.get("/api/stock/").status_code == 200  # other method
    assert client.post("/api/part/", json={}).status_code == 201  # other path
    assert client.post(TRANSFER, json={}).status_code == 502
    assert client.post("/api/stock/add/", json={}).status_code == 502
    assert client.post(TRANSFER, json={}).status_code == 201  # used up
