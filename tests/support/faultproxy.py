"""A fault proxy for tests: it sits between a client and InvenTree and breaks chosen requests.

The apply protocol treats timeouts, dropped connections, 5xx answers and bodies that do not
come from InvenTree as unknown outcomes (design section 7). This proxy produces each of
them on purpose, before or after the request reaches the server, so tests can check what
the server did and what the connector concludes.

Faults are scripted with `FaultProxy.inject`. A request that no rule matches is forwarded
unchanged. The proxy logs the method, path, fault and upstream status of every request,
never headers or bodies, because those carry the API token and stock data.
"""

from __future__ import annotations

import contextlib
import socket
import socketserver
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

HOP_BY_HOP = {"connection", "content-length", "host", "keep-alive", "transfer-encoding", "content-encoding"}
HOLD_LIMIT_SECONDS = 60.0
"""A held request not released by then is dropped without being forwarded, so a failed test cannot hang."""
PROXY_ERROR_PAGE = b"<html><body><h1>502 Bad Gateway</h1></body></html>"


class Fault(Enum):
    DELAY = "delay"
    """Forward, then wait `seconds` before answering: a client timeout while the server may have committed."""
    HOLD = "hold"
    """Hold the request until `FaultProxy.release`, then forward it: a write that reaches the server after the client
    gave up, and after anything the test runs in between, such as recovery."""
    DROP_BEFORE = "drop_before"
    """Close the connection without forwarding: nothing reaches the server."""
    DROP_AFTER = "drop_after"
    """Forward, then close the connection without an answer: the server may have committed."""
    FAIL_BEFORE = "fail_before"
    """Answer `status` with an HTML error page without forwarding."""
    FAIL_AFTER = "fail_after"
    """Forward, then answer `status` with an HTML error page instead of the server's answer."""
    GARBLE = "garble"
    """Forward, then answer with the server's status and the first half of its body."""


@dataclass
class Rule:
    method: str
    path: str
    """A path prefix, for example `/api/stock/transfer/`."""
    fault: Fault
    seconds: float = 0.0
    status: int = 502
    times: int = 1
    """How many matching requests the rule breaks before it is used up."""


@dataclass(frozen=True)
class Entry:
    method: str
    path: str
    fault: Fault | None
    upstream_status: int | None
    """None when the request never reached the server."""


@dataclass
class _State:
    upstream: httpx.Client
    rules: list[Rule] = field(default_factory=list)
    log: list[Entry] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    released: threading.Event = field(default_factory=threading.Event)
    closing: bool = False
    held: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(0))

    def take(self, method: str, path: str) -> Rule | None:
        with self.lock:
            for rule in self.rules:
                if rule.times > 0 and rule.method == method and path.startswith(rule.path):
                    rule.times -= 1
                    return rule
        return None

    def record(self, entry: Entry) -> None:
        with self.lock:
            self.log.append(entry)


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def do_OPTIONS(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: Any) -> None:
        """Silenced: the default writes every request line to stderr."""

    def _handle(self) -> None:
        state = self.server.state
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = self.path.split("?", 1)[0]
        rule = state.take(self.command, path)
        fault = rule.fault if rule else None
        if rule and rule.fault in (Fault.DROP_BEFORE, Fault.FAIL_BEFORE):
            state.record(Entry(self.command, path, fault, None))
            if rule.fault is Fault.DROP_BEFORE:
                self._drop()
            else:
                self._send(rule.status, {"Content-Type": "text/html"}, PROXY_ERROR_PAGE)
            return
        if rule and rule.fault is Fault.HOLD:
            state.held.release()
            if not state.released.wait(HOLD_LIMIT_SECONDS) or state.closing:
                state.record(Entry(self.command, path, fault, None))
                self._drop()
                return
        headers = {name: value for name, value in self.headers.items() if name.lower() not in HOP_BY_HOP}
        try:
            response = state.upstream.request(self.command, self.path, headers=headers, content=body)
        except httpx.HTTPError:
            state.record(Entry(self.command, path, fault, None))
            self._send(502, {"Content-Type": "text/html"}, PROXY_ERROR_PAGE)
            return
        state.record(Entry(self.command, path, fault, response.status_code))
        answer = {name: value for name, value in response.headers.items() if name.lower() not in HOP_BY_HOP}
        if rule is None or rule.fault is Fault.HOLD:
            self._send(response.status_code, answer, response.content)
        elif rule.fault is Fault.DELAY:
            time.sleep(rule.seconds)
            self._send(response.status_code, answer, response.content)
        elif rule.fault is Fault.DROP_AFTER:
            self._drop()
        elif rule.fault is Fault.FAIL_AFTER:
            self._send(rule.status, {"Content-Type": "text/html"}, PROXY_ERROR_PAGE)
        else:
            self._send(response.status_code, answer, response.content[: len(response.content) // 2])

    def _send(self, status: int, headers: dict[str, str], body: bytes) -> None:
        try:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self.close_connection = True  # the client gave up first, for example after its timeout

    def _drop(self) -> None:
        self.close_connection = True
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)


class LocalServer(ThreadingHTTPServer):
    """A threaded HTTP server that skips the reverse DNS lookup of `HTTPServer.server_bind` (slow on macOS)."""

    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)


class _Server(LocalServer):
    state: _State


class FaultProxy:
    """A local HTTP proxy to `upstream`. Use it as a context manager, and point the client at `url`."""

    def __init__(self, upstream: str) -> None:
        self._state = _State(httpx.Client(base_url=upstream, follow_redirects=False, timeout=60))
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.state = self._state
        self._thread = threading.Thread(target=self._server.serve_forever, args=(0.05,), daemon=True)
        host, port = self._server.server_address[:2]
        self.url = f"http://{host!s}:{port}"

    def __enter__(self) -> FaultProxy:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._state.closing = True  # a request still held is dropped, never forwarded after the test
        self._state.released.set()
        self._server.shutdown()
        self._server.server_close()
        self._state.upstream.close()

    def inject(self, rule: Rule) -> None:
        """Break the next `rule.times` requests that match `rule`. Rules are tried in the order they were added."""
        with self._state.lock:
            self._state.rules.append(rule)

    def wait_held(self, timeout: float = 30.0) -> None:
        """Return once a request matching a `Fault.HOLD` rule has reached the proxy and is being held."""
        if not self._state.held.acquire(timeout=timeout):
            raise TimeoutError("no request was held")

    def release(self) -> None:
        """Forward every held request, and any later one a `Fault.HOLD` rule matches, at once."""
        self._state.released.set()

    def wait_for(self, count: int, timeout: float = 30.0) -> list[Entry]:
        """The log once it holds `count` entries. A client can give up before the server has answered the proxy."""
        deadline = time.monotonic() + timeout
        while len(entries := self.log) < count:
            if time.monotonic() > deadline:
                raise TimeoutError(f"the proxy logged {len(entries)} of {count} requests")
            time.sleep(0.05)
        return entries

    @property
    def log(self) -> list[Entry]:
        with self._state.lock:
            return list(self._state.log)

    def drain(self) -> list[Entry]:
        """The log so far, which is then emptied, and the used-up rules dropped: a run of hours stays small."""
        with self._state.lock:
            entries, self._state.log = self._state.log, []
            self._state.rules = [rule for rule in self._state.rules if rule.times > 0]
            return entries
