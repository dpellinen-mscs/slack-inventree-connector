# SPDX-License-Identifier: MIT
"""HTTP serving for `stockthread run` (design section 4, "Transport"; ADR 0013).

Bolt's WSGI adapter, served by waitress: pure Python, one process with a few threads, so the instance lock,
the store and the worker stay in the one process that `run` starts. The signature of every POST is checked
here, on the raw bytes, before Bolt parses or logs anything: Bolt logs the whole body of a request it refuses,
and a body it cannot parse becomes a traceback. Bolt checks the signature again, and answers Slack's URL
verification. Only POST to the configured path reaches Bolt; anything else is a 404.

Waitress serves on its own thread. `close` stops that thread's loop and closes every socket on the thread
itself: closing them from another thread races the loop's `select`.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from slack_bolt import App
from slack_bolt.adapter.wsgi import SlackRequestHandler
from slack_sdk.signature import SignatureVerifier
from waitress import wasyncore
from waitress.server import create_server
from waitress.task import ThreadedTaskDispatcher

from stockthread.config import SlackHttpSettings

if TYPE_CHECKING:
    from wsgiref.types import StartResponse, WSGIApplication, WSGIEnvironment

log = logging.getLogger(__name__)

THREADS = 4
"""Requests handled at once. Each one stores a payload and acknowledges it: quick work."""
MAX_BODY_BYTES = 4 * 1024 * 1024
"""Larger requests are refused before they are read. A Slack payload is far smaller: a message is at most
40,000 characters, and a click carries one message of at most 50 blocks."""
CHANNEL_TIMEOUT_SECONDS = 15
"""A connection that sends nothing for this long is closed. Slack sends small bodies at once; a client that
trickles must not hold one of waitress's connections for minutes."""
LOOP_SECONDS = 0.2
"""How often the serving thread looks whether it should stop."""
DRAIN_SECONDS = 3.0
"""On stop, requests already being handled get this long to send their answer."""
SHUTDOWN_SECONDS = 5
JOIN_SECONDS = DRAIN_SECONDS + SHUTDOWN_SECONDS + 2 * LOOP_SECONDS + 1
"""`close` waits this long for the serving thread: longer than it can take to end on its own."""

# Bolt logs the body of every request whose signature it refuses. `signed` refuses those first; this keeps
# the log quiet if a request ever gets past it.
logging.getLogger("slack_bolt.RequestVerification").setLevel(logging.WARNING)


class ServeError(Exception):
    """The address cannot be listened on. The message names the address and the error, nothing else."""


def signed(application: WSGIApplication, signing_secret: str, path: str) -> WSGIApplication:
    """Refuse a POST to `path` with a missing, wrong or stale signature (401), before `application` sees it.

    The body is read once, checked as bytes, and handed on unchanged. Nothing about a refused request is
    logged: it may be anyone's, and a genuine one refused for a skewed clock still holds a Slack post.
    """
    verifier = SignatureVerifier(signing_secret)

    def check(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        if environ.get("REQUEST_METHOD") != "POST" or environ.get("PATH_INFO") != path:
            return application(environ, start_response)
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        body = environ["wsgi.input"].read(length) if length > 0 else b""
        timestamp, signature = environ.get("HTTP_X_SLACK_REQUEST_TIMESTAMP"), environ.get("HTTP_X_SLACK_SIGNATURE")
        if not verifier.is_valid(body, timestamp, signature):
            start_response("401 Unauthorized", [("Content-Type", "text/plain"), ("Content-Length", "0")])
            return [b""]
        environ["wsgi.input"] = io.BytesIO(body)
        environ["CONTENT_LENGTH"] = str(len(body))
        return application(environ, start_response)

    return check


class HttpServer:
    """A `run.Connection` for HTTP mode: `connect` listens and serves on a thread, `close` stops both, and
    `alive` says whether it still serves."""

    def __init__(self, app: App, settings: SlackHttpSettings, signing_secret: str) -> None:
        self._application = signed(SlackRequestHandler(app, path=settings.path), signing_secret, settings.path)
        self._settings = settings
        self._map: dict[int, Any] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.port: int | None = None
        """The port listened on, once connected: the configured one, or the one the system chose for 0."""

    def connect(self) -> None:
        host, port = self._settings.host, self._settings.port
        dispatcher = ThreadedTaskDispatcher()
        dispatcher.set_thread_count(THREADS)
        try:
            server = create_server(
                self._application,
                map=self._map,
                _dispatcher=dispatcher,
                host=host,
                port=port,
                max_request_body_size=MAX_BODY_BYTES,
                channel_timeout=CHANNEL_TIMEOUT_SECONDS,
                clear_untrusted_proxy_headers=True,
            )
        except (OSError, ValueError) as error:
            # ValueError: a host name that does not resolve. Nothing may stay open: the threads, or a socket
            # bound for one address family before the other failed.
            wasyncore.close_all(self._map)
            dispatcher.shutdown(timeout=SHUTDOWN_SECONDS)
            raise ServeError(f"cannot listen on {host}:{port} ({type(error).__name__}, {_errno(error)})") from None
        self.port = int(getattr(server, "effective_port", port))  # waitress gives a str
        self._thread = threading.Thread(target=self._serve, args=(dispatcher,), name="http", daemon=True)
        self._thread.start()
        shown = f"[{host}]" if ":" in host else host
        log.info("listening for Slack on http://%s:%s%s", shown, self.port, self._settings.path)

    def alive(self) -> bool:
        """False once serving has stopped by itself: Slack's deliveries then fail, and `run` stops."""
        return self._thread is None or self._thread.is_alive()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(JOIN_SECONDS)

    def _serve(self, dispatcher: ThreadedTaskDispatcher) -> None:
        try:
            while not self._stop.is_set():
                wasyncore.loop(timeout=LOOP_SECONDS, map=self._map, count=1)
            # No new connections; the answers to requests already taken are still sent.
            for channel in list(self._map.values()):
                if getattr(channel, "accepting", False):
                    channel.close()
            ends = time.monotonic() + DRAIN_SECONDS
            while time.monotonic() < ends and any(_busy(channel) for channel in self._map.values()):
                wasyncore.loop(timeout=LOOP_SECONDS / 4, map=self._map, count=1)
        except Exception as error:
            # The class only, as everywhere. `alive` turns false, and `run` stops.
            log.error("HTTP serving stopped (%s)", type(error).__name__)
        finally:
            wasyncore.close_all(self._map)
            dispatcher.shutdown(timeout=SHUTDOWN_SECONDS)


def _busy(channel: Any) -> bool:
    """A connection with a request in hand or an answer not yet sent."""
    return bool(getattr(channel, "requests", None)) or bool(getattr(channel, "total_outbufs_len", 0))


def _errno(error: Exception) -> str:
    number = getattr(error, "errno", None)
    return f"errno {number}" if number is not None else "not a valid address, or it does not resolve"
