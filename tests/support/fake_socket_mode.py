"""A fake Slack Socket Mode endpoint for tests: a local WebSocket server that slack_sdk's own client connects to.

It speaks just enough of RFC 6455 and Slack's envelope protocol for the connector's real `SocketModeHandler`: the
handshake, a `hello`, events as `events_api` envelopes, acks, ping and pong, and close. It can break the connection
the ways Slack and networks do: an abrupt drop (the socket is shut, no close frame), Slack's own `disconnect`
message asking the client to move to a new connection, and a dead link that is still open but answers nothing, so
only the client's ping check notices. An envelope that was never acked is delivered again on the next connection,
with a higher `retry_attempt`, as Slack does.

`FakeSocketMode.url` is what `apps.connections.open` should answer. The server keeps no message text in its log.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import socket
import struct
import threading
from dataclasses import dataclass, field
from typing import Any

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TEXT, CLOSE, PING, PONG = 0x1, 0x8, 0x9, 0xA


@dataclass
class _Envelope:
    envelope_id: str
    payload: dict[str, Any]
    retry_attempt: int = 0


@dataclass
class _Link:
    sock: socket.socket
    send_lock: threading.Lock = field(default_factory=threading.Lock)
    deaf: bool = False
    """A dead link: frames from the client are read and dropped, nothing is answered or sent."""


class FakeSocketMode:
    def __init__(self) -> None:
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self._lock = threading.Condition()
        self._link: _Link | None = None
        self._unacked: dict[str, _Envelope] = {}
        self._next = 0
        self.connections = 0
        """Handshakes completed, so reconnections are `connections - 1`."""
        self.acked: list[str] = []
        self.pongs = 0
        self._closed = False
        self._accepting = threading.Thread(target=self._accept, name="fake-socket-mode", daemon=True)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/link/?ticket=synthetic"

    def __enter__(self) -> FakeSocketMode:
        self._accepting.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._closed = True
        self._server.close()
        with self._lock:
            link, self._link = self._link, None
            self._lock.notify_all()
        if link is not None:
            _shut(link.sock)

    def wait_connected(self, connections: int = 1, timeout: float = 30.0) -> None:
        """Until `connections` handshakes have been made and that link is live."""
        with self._lock:
            if not self._lock.wait_for(lambda: self.connections >= connections and self._link is not None, timeout):
                raise TimeoutError(f"{self.connections} connections, waited for {connections}")

    def wait_ponged(self, timeout: float = 30.0) -> None:
        """Until the current link has answered a ping from the client."""
        with self._lock:
            start = self.pongs
            if not self._lock.wait_for(lambda: self.pongs > start, timeout):
                raise TimeoutError("no ping was answered")

    def deliver(self, body: dict[str, Any]) -> str:
        """Send an Events API body (`event_callback`) as an envelope; returns its envelope id. On a dead link or
        with no link it waits, unacked, for the next connection."""
        with self._lock:
            self._next += 1
            envelope = _Envelope(f"env-{self._next:06d}", body)
            self._unacked[envelope.envelope_id] = envelope
            link = self._link
        if link is not None and not link.deaf:
            self._send_envelope(link, envelope)
        return envelope.envelope_id

    def wait_acked(self, envelope_id: str, timeout: float = 30.0) -> None:
        with self._lock:
            if not self._lock.wait_for(lambda: envelope_id in self.acked, timeout):
                raise TimeoutError(f"{envelope_id} was not acked")

    def drop(self) -> None:
        """Shut the socket at once, with no close frame: a crashed proxy or a reset connection."""
        with self._lock:
            link, self._link = self._link, None
        if link is not None:
            _shut(link.sock)

    def refresh(self) -> None:
        """Slack's own `disconnect` message (`refresh_requested`): the client opens a new connection by itself."""
        with self._lock:
            link = self._link
        if link is not None:
            message = {"type": "disconnect", "reason": "refresh_requested", "debug_info": {"host": "fake"}}
            self._send(link, TEXT, json.dumps(message).encode())

    def go_deaf(self) -> None:
        """Keep the link open but answer nothing, not even a ping: a half-open connection after a network change."""
        with self._lock:
            if self._link is not None:
                self._link.deaf = True

    def _accept(self) -> None:
        while not self._closed:
            try:
                sock, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), name="fake-socket-mode-link", daemon=True).start()

    def _serve(self, sock: socket.socket) -> None:
        try:
            reader = sock.makefile("rb")
            if not _handshake(sock, reader):
                return
            link = _Link(sock)
            with self._lock:
                old, self._link = self._link, link
                self.connections += 1
                pending = list(self._unacked.values())
                for envelope in pending:
                    envelope.retry_attempt += 1
                self._lock.notify_all()
            if old is not None:
                _shut(old.sock)
            self._send(link, TEXT, json.dumps({"type": "hello", "num_connections": 1}).encode())
            for envelope in pending:
                self._send_envelope(link, envelope)
            self._read(link, reader)
        except OSError:
            pass
        finally:
            with self._lock:
                if self._link is not None and self._link.sock is sock:
                    self._link = None
            _shut(sock)

    def _read(self, link: _Link, reader: Any) -> None:
        while True:
            frame = _read_frame(reader)
            if frame is None:
                return
            opcode, payload = frame
            if link.deaf:
                continue
            if opcode == PING:
                self._send(link, PONG, payload)
                with self._lock:
                    self.pongs += 1
                    self._lock.notify_all()
            elif opcode == CLOSE:
                with contextlib.suppress(OSError):
                    self._send(link, CLOSE, payload[:2])
                return
            elif opcode == TEXT:
                message = json.loads(payload)
                envelope_id = message.get("envelope_id")
                with self._lock:
                    if envelope_id in self._unacked:
                        del self._unacked[envelope_id]
                        self.acked.append(envelope_id)
                        self._lock.notify_all()

    def _send_envelope(self, link: _Link, envelope: _Envelope) -> None:
        message = {
            "envelope_id": envelope.envelope_id,
            "type": "events_api",
            "accepts_response_payload": False,
            "retry_attempt": envelope.retry_attempt,
            "retry_reason": "timeout" if envelope.retry_attempt else "",
            "payload": envelope.payload,
        }
        with contextlib.suppress(OSError):
            self._send(link, TEXT, json.dumps(message).encode())

    @staticmethod
    def _send(link: _Link, opcode: int, payload: bytes) -> None:
        size = len(payload)
        if size < 126:
            head = struct.pack("!BB", 0x80 | opcode, size)
        elif size < 1 << 16:
            head = struct.pack("!BBH", 0x80 | opcode, 126, size)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 127, size)
        with link.send_lock:
            link.sock.sendall(head + payload)


def _handshake(sock: socket.socket, reader: Any) -> bool:
    headers: dict[str, str] = {}
    if not reader.readline().startswith(b"GET "):
        return False
    while (line := reader.readline().strip()) != b"":
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    key = headers.get("sec-websocket-key")
    if key is None:
        return False
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode(), usedforsecurity=False).digest()).decode()
    sock.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
    )
    return True


def _read_frame(reader: Any) -> tuple[int, bytes] | None:
    """One frame from the client (always masked), or None once the connection has ended. Fragments are not
    joined: slack_sdk sends none."""
    head = reader.read(2)
    if len(head) < 2:
        return None
    opcode, size = head[0] & 0x0F, head[1] & 0x7F
    if size == 126:
        size = struct.unpack("!H", reader.read(2))[0]
    elif size == 127:
        size = struct.unpack("!Q", reader.read(8))[0]
    mask = reader.read(4) if head[1] & 0x80 else b"\0\0\0\0"
    data = reader.read(size)
    if len(data) < size:
        return None
    return opcode, bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))


def _shut(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()
