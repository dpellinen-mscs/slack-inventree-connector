# SPDX-License-Identifier: MIT
"""A soak run: `stockthread run` for hours against a disposable InvenTree and a fake Slack whose link breaks.

    tests/integration/start-inventree.sh inventree/inventree:1.5.6 > .it.env
    set -a; . ./.it.env; set +a
    .venv/bin/python tests/soak/soak.py --minutes 1440 --report soak-report.json
    tests/integration/start-inventree.sh --stop

The connector runs in this process with writes on, through `run` as the CLI starts it, except that Slack is
local: Socket Mode connects to `tests/support/fake_socket_mode.py`, and the Web API calls the connector makes
are answered here (any other call goes to a closed local port, never to Slack). InvenTree is reached through
`tests/support/faultproxy.py`.

The soak gives itself a part and two locations of its own on the server, then posts a take-out to a van and a
return from it, in turn, every `--post-every` seconds. Both apply with no click (`policy.auto_apply`). Every
`--fault-every` seconds it breaks one thing, in turn: the Slack link is dropped, Slack asks for a refresh, the
link goes dead while open, an InvenTree read is dropped, an InvenTree read answers 503. Writes are never broken
here: unknown outcomes are the integration tests' subject (`test_char_faults`, `test_sent_writes`).

At the end it stops posting, lets the connector finish, stops it, and checks (exit 1 on any failure):
- every envelope was acked, and every post has a reply in its thread;
- no post has more than one request that wrote, and no more posts wrote nothing than InvenTree faults hit
  (a broken read can delay a write until the next post's plan finds the stock changed, and that plan then
  waits for a person, as it should; nothing else may leave a post unwritten);
- the van holds exactly what the verified requests moved, and the soak part's total stock is unchanged;
- the connector stopped by itself with exit 0;
- threads, open files and memory did not keep growing after the warm-up (one full cycle of faults at least).

The audit store and the run log stay in the work directory the report names. The report (JSON) keeps counts,
the resource samples and the failures, never message text or tokens.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import logging
import math
import os
import re
import resource
import sqlite3
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from slack_sdk import WebClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))

from fake_socket_mode import FakeSocketMode
from faultproxy import Fault, FaultProxy, Rule

from stockthread.config import parse_settings
from stockthread.run import run

WORKSPACE, APP, CHANNEL, POSTER = "T0ACME001", "A0ACME001", "C0ACME001", "U0SAM0001"
PART_IPN, PART_NAME = "SOAK-WIDGET", "Soak Widget"
DEPOT, SHELF, VANS, VAN = "Soak Depot", "Soak Shelf", "Soak Vans", "Van 9"
START_QUANTITY = Decimal(1000)
WROTE = ("applied", "verified", "verify_failed", "accepted", "undoing", "undo_failed", "undo_unknown", "undone")
"""Request states that mean the write was sent and landed."""
KEEP_MESSAGES_SECONDS = 2 * 3600
"""The fake Slack forgets older messages: the soak's posts are settled long before."""
CLOSED_PORT = "http://127.0.0.1:9/api/"


@dataclass
class Sample:
    at_seconds: int
    threads: int
    open_files: int
    peak_rss_kb: int
    """The process's peak resident memory so far: it only rises, and keeps rising with a leak."""


@dataclass
class Report:
    minutes: float
    work: str = ""
    """The audit store and the run log (`soak.log`, as `stockthread run` logs at INFO) are kept here."""
    posts: int = 0
    faults: dict[str, int] = field(default_factory=dict)
    reconnections: int = 0
    inventree_requests: int = 0
    inventree_faults_hit: int = 0
    replies: int = 0
    posts_without_write: int = 0
    request_states: dict[str, int] = field(default_factory=dict)
    van_quantity: str = ""
    expected_van_quantity: str = ""
    exit_code: int | None = None
    threads_at_end: dict[str, int] = field(default_factory=dict)
    samples: list[Sample] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


class SlackClient(WebClient):
    """The Web API calls the connector makes, answered locally. Bolt takes only a real WebClient."""

    def __init__(self, endpoint: FakeSocketMode, messages: dict[str, dict[str, Any]]) -> None:
        super().__init__(base_url=CLOSED_PORT)
        self.endpoint = endpoint
        self.messages = messages
        self.lock = threading.Lock()
        self.replies: dict[str, int] = {}
        self.sent = itertools.count(1)

    def auth_test(self, **kwargs: Any) -> Any:
        return {"ok": True, "team_id": WORKSPACE, "bot_id": "B0STOCKBOT", "user_id": "U0STOCKBOT"}

    def apps_connections_open(self, **kwargs: Any) -> Any:
        return {"ok": True, "url": self.endpoint.url}

    def chat_postMessage(self, **kwargs: Any) -> Any:
        with self.lock:
            thread = str(kwargs.get("thread_ts", ""))
            self.replies[thread] = self.replies.get(thread, 0) + 1
            ts = f"{int(time.time())}.{next(self.sent):06d}"
            self.messages[ts] = {
                "ts": ts,
                "thread_ts": thread,
                "bot_id": "B0STOCKBOT",
                "app_id": APP,
                "text": kwargs.get("text", ""),
                "blocks": kwargs.get("blocks") or [],
            }
            return {"ok": True, "channel": CHANNEL, "ts": ts}

    def chat_update(self, **kwargs: Any) -> Any:
        with self.lock:
            ts = str(kwargs.get("ts"))
            if ts in self.messages:
                self.messages[ts] = {**self.messages[ts], "text": kwargs.get("text", ""), "blocks": kwargs["blocks"]}
            return {"ok": True, "channel": CHANNEL, "ts": ts}

    def chat_postEphemeral(self, **kwargs: Any) -> Any:
        return {"ok": True, "message_ts": f"{int(time.time())}.000000"}

    def forget(self, before: float) -> None:
        """Drop the messages posted before `before` (Unix time), so a run of hours keeps what is in play only."""
        with self.lock:
            for ts in [ts for ts in self.messages if float(ts) < before]:
                del self.messages[ts]

    def conversations_replies(self, **kwargs: Any) -> Any:
        """The thread's root, then the replies from `oldest` to `latest`, as the connector asks for them."""
        with self.lock:
            root = str(kwargs.get("ts"))
            oldest, latest = kwargs.get("oldest"), kwargs.get("latest")
            replies = [
                message
                for ts, message in sorted(self.messages.items())
                if message.get("thread_ts") == root
                and (oldest is None or ts >= str(oldest))
                and (latest is None or ts <= str(latest))
            ]
            found = [self.messages[root], *replies] if root in self.messages else replies
            return {"ok": True, "messages": found[: int(kwargs.get("limit") or 100)], "has_more": False}


class Server:
    """Plain HTTP to the disposable server, for the soak's own seeding and checks."""

    def __init__(self, url: str, token: str) -> None:
        self.http = httpx.Client(base_url=url, headers={"Authorization": f"Token {token}"}, timeout=30)

    def rows(self, path: str, **params: Any) -> list[dict[str, Any]]:
        response = self.http.get(path, params=params)
        response.raise_for_status()
        data = response.json()
        return list(data["results"] if isinstance(data, dict) else data)

    def create(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.http.post(path, json=body)
        if response.status_code != 201:
            raise RuntimeError(f"POST {path}: HTTP {response.status_code}")
        data = response.json()
        return dict(data[0] if isinstance(data, list) else data)

    def location(self, name: str, parent: int | None) -> int:
        found = [row for row in self.rows("/api/stock/location/", name=name) if row.get("parent") == parent]
        if found:
            return int(found[0]["pk"])
        return int(self.create("/api/stock/location/", {"name": name, "parent": parent})["pk"])

    def quantity(self, part: int, location: int | None = None) -> Decimal:
        params: dict[str, Any] = {"part": part}
        if location is not None:
            params["location"] = location
        return sum((Decimal(str(row["quantity"])) for row in self.rows("/api/stock/", **params)), Decimal(0))


def seed(server: Server) -> tuple[int, int]:
    """The soak part and the van, and the shelf, with `START_QUANTITY` on the shelf. Run twice, it tops nothing up:
    a soak always starts from the part's current stock."""
    depot = server.location(DEPOT, None)
    shelf = server.location(SHELF, depot)
    van = server.location(VAN, server.location(VANS, None))
    found = [row for row in server.rows("/api/part/", IPN=PART_IPN) if row.get("IPN") == PART_IPN]
    if found:
        part = int(found[0]["pk"])
    else:
        body = {"name": PART_NAME, "IPN": PART_IPN, "description": "soak", "component": True, "active": True}
        part = int(server.create("/api/part/", body)["pk"])
        server.create("/api/stock/", {"part": part, "location": shelf, "quantity": str(START_QUANTITY)})
    return part, van


def thread_names() -> dict[str, int]:
    """Live threads by name, digits dropped: what a growing thread count is made of. Taken before the stop."""
    names = Counter(re.sub(r"[0-9]+", "N", thread.name) for thread in threading.enumerate())
    return dict(names.most_common())


def sample(started: float) -> Sample:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return Sample(
        at_seconds=int(time.monotonic() - started),
        threads=threading.active_count(),
        open_files=len(os.listdir("/dev/fd")),
        peak_rss_kb=peak // 1024 if sys.platform == "darwin" else peak,  # bytes on macOS, kB on Linux
    )


def faults(endpoint: FakeSocketMode, proxy: FaultProxy) -> list[tuple[str, Callable[[], None]]]:
    return [
        ("slack_drop", endpoint.drop),
        ("slack_refresh", endpoint.refresh),
        ("slack_dead_link", endpoint.go_deaf),
        ("inventree_read_dropped", lambda: proxy.inject(Rule("GET", "/api/", Fault.DROP_BEFORE))),
        ("inventree_read_503", lambda: proxy.inject(Rule("GET", "/api/", Fault.FAIL_BEFORE, status=503))),
    ]


def tally(report: Report, proxy: FaultProxy) -> None:
    entries = proxy.drain()
    report.inventree_requests += len(entries)
    report.inventree_faults_hit += sum(1 for entry in entries if entry.fault is not None)


def body(ts: str, text: str) -> dict[str, Any]:
    event = {"type": "message", "channel": CHANNEL, "user": POSTER, "text": text, "ts": ts}
    return {"type": "event_callback", "team_id": WORKSPACE, "api_app_id": APP, "event_id": f"Ev{ts}", "event": event}


IN_FLIGHT = ("approved", "applying", "applied")


def settled(endpoint: FakeSocketMode, client: SlackClient, moved: dict[str, Decimal], database: Path) -> bool:
    """Every envelope acked, every post replied to, and no request between approval and its read-back."""
    with client.lock:
        replied = set(client.replies)
    if endpoint.unacked() or any(ts not in replied for ts in moved):
        return False
    states, _ = wrote_per_post(database)
    return not any(states.get(state) for state in IN_FLIGHT)


def wrote_per_post(database: Path) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Request states by count, and the states of the requests that wrote, by post ts."""
    with contextlib.closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT p.ts, r.state FROM requests r JOIN plans l ON l.id = r.plan_id JOIN posts p ON p.id = l.post_id"
        ).fetchall()
    states: dict[str, int] = {}
    wrote: dict[str, list[str]] = {}
    for ts, state in rows:
        states[state] = states.get(state, 0) + 1
        if state in WROTE:
            wrote.setdefault(ts, []).append(state)
    return states, wrote


def soak(args: argparse.Namespace) -> Report:
    report = Report(minutes=args.minutes)
    token = Path(os.environ["STOCKTHREAD_IT_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    server = Server(os.environ["STOCKTHREAD_IT_URL"], token)
    part, van = seed(server)
    total_before = server.quantity(part)
    van_before = server.quantity(part, van)
    work = Path(tempfile.mkdtemp(prefix="stockthread-soak-"))
    report.work = str(work)
    logging.basicConfig(
        filename=work / "soak.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    messages: dict[str, dict[str, Any]] = {}
    moved: dict[str, Decimal] = {}
    with FakeSocketMode() as endpoint, FaultProxy(os.environ["STOCKTHREAD_IT_URL"]) as proxy:
        data = {
            "slack": {"workspace_id": WORKSPACE, "app_id": APP, "channels": [CHANNEL], "catch_up_hours": 0},
            "inventree": {"url": proxy.url},
            "storage": {"database": str(work / "stockthread.db")},
            "writes": {"enabled": True},
            "policy": {"auto_apply": ["take_to_vehicle", "return"]},
        }
        env = {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic", "INVENTREE_TOKEN": token}
        settings = parse_settings(data, env)
        client = SlackClient(endpoint, messages)
        stop, codes = threading.Event(), []
        connector = threading.Thread(
            target=lambda: codes.append(run(settings, stop, slack_client=client)), name="connector", daemon=True
        )
        connector.start()
        endpoint.wait_connected(timeout=120)
        started = time.monotonic()
        ends = started + args.minutes * 60
        schedule = itertools.cycle(faults(endpoint, proxy))
        next_post = next_fault = next_sample = started
        sequence = itertools.count(1)
        while (now := time.monotonic()) < ends:
            if now >= next_sample:
                tally(report, proxy)
                client.forget(time.time() - KEEP_MESSAGES_SECONDS)
                report.samples.append(sample(started))
                next_sample += args.sample_every
            if now >= next_fault and now > started:
                name, breaks = next(schedule)
                breaks()
                report.faults[name] = report.faults.get(name, 0) + 1
                next_fault += args.fault_every
            elif now >= next_fault:
                next_fault += args.fault_every
            if now >= next_post:
                number = next(sequence)
                quantity = Decimal(1 + (number + 1) // 2 % 3)  # a return gives back the take before it
                ts = f"{int(time.time())}.{number:06d}"
                if number % 2:
                    text = f"took {quantity} {PART_NAME} from {SHELF} to {VAN}"
                    moved[ts] = quantity
                else:
                    text = f"returned {quantity} {PART_NAME} from {VAN} to {SHELF}"
                    moved[ts] = -quantity
                with client.lock:
                    messages[ts] = body(ts, text)["event"]
                endpoint.deliver(body(ts, text))
                report.posts += 1
                next_post += args.post_every
            time.sleep(0.2)
        settle = time.monotonic() + args.settle_seconds
        while time.monotonic() < settle and not settled(endpoint, client, moved, work / "stockthread.db"):
            time.sleep(1)
        tally(report, proxy)
        report.samples.append(sample(started))
        report.threads_at_end = thread_names()
        stop.set()
        connector.join(120)
        report.exit_code = codes[0] if codes else None
        report.reconnections = endpoint.connections - 1
        report.replies = sum(client.replies.values())
        replied = set(client.replies)
        unacked = endpoint.unacked()
    states, wrote = wrote_per_post(work / "stockthread.db")
    report.request_states = states
    expected = van_before + sum((moved[ts] for ts in wrote if ts in moved), Decimal(0))
    van_now = server.quantity(part, van)
    report.van_quantity, report.expected_van_quantity = str(van_now), str(expected)
    failures = report.failures
    if unacked:
        failures.append(f"{unacked} envelopes were never acked")
    if missing := [ts for ts in moved if ts not in replied]:
        failures.append(f"{len(missing)} posts have no reply in their thread")
    if twice := [ts for ts, found in wrote.items() if len(found) > 1]:
        failures.append(f"{len(twice)} posts wrote more than once")
    report.posts_without_write = len([ts for ts in moved if ts not in wrote])
    if report.posts_without_write > report.inventree_faults_hit:
        failures.append(
            f"{report.posts_without_write} posts wrote nothing, more than the {report.inventree_faults_hit}"
            " InvenTree faults that hit could explain"
        )
    if van_now != expected:
        failures.append(f"the van holds {van_now}, the verified requests moved it to {expected}")
    if server.quantity(part) != total_before:
        failures.append("the soak part's total stock changed")
    if report.exit_code != 0:
        failures.append(f"the connector stopped with {report.exit_code}, or did not stop")
    cycle = len(faults(endpoint, proxy)) * args.fault_every
    failures.extend(growth(report.samples, max(args.warm_up_samples, math.ceil(cycle / args.sample_every))))
    return report


def growth(samples: list[Sample], warm_up: int) -> list[str]:
    """A leak keeps growing; a pool or a connection in flight levels off. After the warm-up the samples are cut in
    two halves, and each measure's peak in the second half may pass the first half's only by a little."""
    after = samples[warm_up:]
    if len(after) < 4:
        return ["too few samples after the warm-up to judge growth: run longer"]
    first, second = after[: len(after) // 2], after[len(after) // 2 :]
    found = []
    for name, margin in (("threads", 4), ("open_files", 8)):
        before, later = max(getattr(s, name) for s in first), max(getattr(s, name) for s in second)
        if later > before + margin:
            found.append(f"{name} kept growing: peak {before} in the first half, {later} in the second")
    before, later = max(s.peak_rss_kb for s in first), max(s.peak_rss_kb for s in second)
    if later > before * 1.1 + 10_000:
        found.append(f"memory kept growing: peak {before} kB in the first half, {later} kB in the second")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=float, default=10)
    parser.add_argument("--post-every", type=float, default=30, help="seconds between posts")
    parser.add_argument("--fault-every", type=float, default=300, help="seconds between faults")
    parser.add_argument("--sample-every", type=float, default=60, help="seconds between resource samples")
    parser.add_argument("--warm-up-samples", type=int, default=2, help="at least one full cycle of faults")
    parser.add_argument("--settle-seconds", type=float, default=120)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = soak(args)
    text = json.dumps(asdict(report), indent=2)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    summary = {k: v for k, v in asdict(report).items() if k != "samples"}
    print(json.dumps(summary, indent=2))
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
