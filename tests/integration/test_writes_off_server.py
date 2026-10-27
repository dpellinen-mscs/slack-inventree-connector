# SPDX-License-Identifier: MIT
"""M3 DoD against a real server: with writes off, `stockthread run` sends InvenTree nothing but GET requests.

Every message of the eval corpus is queued and answered with its dry run by `run.run`, as `stockthread run` starts
it, with the catalog, the stock and the version check read from InvenTree through a recording proxy. A request
approved by an earlier run with writes on is in the store too: it waits, and is never sent. Only Slack is faked."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from corpus import cases
from faultproxy import FaultProxy
from slack_sdk import WebClient
from views import picked, view

from stockthread import run as run_module
from stockthread.config import Settings, parse_settings
from stockthread.core.model import Extraction, Match, MatchRule, MatchStatus, PostKey, RequestKey
from stockthread.core.requests import Actor, RequestState
from stockthread.plan_record import plan_of
from stockthread.run import run
from stockthread.store import DryRunOutcome, NewPlan, Store
from stockthread.worker import _record

if TYPE_CHECKING:
    from conftest import Catalog, RawApi, Scratch

WORKSPACE, CHANNEL, APP = "T0ACME001", "C0ACME001", "A0ACME001"
SAM = "U0SAM0001"
EARLIER = PostKey(WORKSPACE, CHANNEL, "1789990000.000100")
"""A post an earlier run with writes on approved: older than every corpus post."""


class FakeSlack(WebClient):
    """Bolt takes only a real WebClient: the calls the connector makes are answered here."""

    def __init__(self) -> None:
        super().__init__()
        self.posted: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def auth_test(self, **kwargs: Any) -> Any:
        return {"team_id": WORKSPACE, "bot_id": "B0STOCKBOT", "user_id": "U0STOCKBOT"}

    def chat_postMessage(self, **kwargs: Any) -> Any:
        with self.lock:
            self.posted.append(kwargs)
            return {"ok": True, "ts": f"1799000000.{len(self.posted):06d}"}

    def chat_update(self, **kwargs: Any) -> Any:
        return {"ok": True, "ts": kwargs["ts"]}

    def chat_postEphemeral(self, **kwargs: Any) -> Any:
        return {"ok": True}

    def conversations_replies(self, **kwargs: Any) -> Any:
        return {"ok": True, "messages": [], "has_more": False}


class FakeConnection:
    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def proxy() -> Iterator[FaultProxy]:
    with FaultProxy(os.environ["STOCKTHREAD_IT_URL"]) as running:
        yield running


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_module, "POLL_SECONDS", 0.01)


def writes_off(database: Path, url: str) -> Settings:
    token = Path(os.environ["STOCKTHREAD_IT_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    data = {
        "slack": {"workspace_id": WORKSPACE, "app_id": APP, "channels": [CHANNEL], "catch_up_hours": 0},
        "inventree": {"url": url, "timeout_seconds": 10},
        "storage": {"database": str(database)},
        "vehicles": {SAM: "Vehicles/Truck 1"},
        "writes": {"enabled": False},
    }
    env = {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic", "INVENTREE_TOKEN": token}
    return parse_settings(data, env)


def stocked(api: RawApi, catalog: Catalog) -> None:
    """50 of every catalog part in main stock, once per server: the corpus's take-outs find stock to pick."""
    main = catalog.locations["Warehouse/Main Stock"]
    for part in catalog.parts.values():
        if not api.items(part, main):
            api.create("/api/stock/", {"part": part, "location": main, "quantity": 50})


def queue_corpus(store: Store) -> list[str]:
    """Every corpus message as a Slack event, in Slack time order. Returns the posts' ts."""
    messages = sorted(
        (message for case in cases() for message in case["post"]["messages"]), key=lambda m: Decimal(m["ts"])
    )
    for message in messages:
        event = {"type": "message", "channel": CHANNEL, "user": message["user"], "text": message.get("text", "")}
        event["ts"] = message["ts"]
        if message.get("thread_ts"):
            event["thread_ts"] = message["thread_ts"]
        body = {"type": "event_callback", "team_id": WORKSPACE, "api_app_id": APP, "event_id": f"Ev{message['ts']}"}
        store.enqueue_event(f"Ev{message['ts']}", "event", json.dumps({**body, "event": event}))
    return [message["ts"] for message in messages]


def approved_earlier(store: Store, scratch: Scratch) -> RequestKey:
    """A take-out of 2 that an earlier run, with writes on, planned and approved, and never sent."""
    owner = "earlier-run"
    store.acquire_lock(owner, stale_after=timedelta(seconds=60))
    store.place_message(owner, EARLIER, user_id=SAM, thread_ts=None, window_seconds=5)
    item = scratch.stock(5)
    movement = view(
        lines=(("Disc 40", "2"),),
        stock=(picked((item["pk"], "2")),),
        items=(Match(MatchStatus.MATCHED, MatchRule.EXACT, scratch.part),),
        source=Match(MatchStatus.MATCHED, MatchRule.EXACT, scratch.parent),
        destination=Match(MatchStatus.MATCHED, MatchRule.EXACT, scratch.other),
    )
    stored = _record(Extraction("rules", (movement.movement,)), [movement], late=False, poster=SAM, messages=(),
                     max_quantity=500)  # fmt: skip
    result: dict[str, Any] = json.loads(json.dumps(stored))
    made = plan_of(result)
    assert made is not None
    store.finish_dry_run(owner, EARLIER, DryRunOutcome.REPLIED, "1789990001.000100", result=result, plan=NewPlan(*made))
    key = RequestKey(EARLIER, 1, 1)
    store.transition(key, expected=RequestState.PLANNED, target=RequestState.APPROVED, actor=Actor.HUMAN)
    store.release_lock(owner)
    return key


def test_with_writes_off_every_request_to_inventree_is_a_get(
    tmp_path: Path, api: RawApi, catalog: Catalog, scratch: Scratch, proxy: FaultProxy
) -> None:
    stocked(api, catalog)
    database = tmp_path / "stockthread.db"
    store = Store.open(database)
    key = approved_earlier(store, scratch)
    queued = queue_corpus(store)
    store.close()
    newest = max(Decimal(ts) for ts in queued)
    slack = FakeSlack()
    stop = threading.Event()

    def watch() -> None:
        """Stop once every event is handled and every post has its dry run, or after two minutes."""
        watcher = Store.open(database)
        try:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline and not stop.is_set():
                if slack.posted and not watcher.events_pending() and not watcher.posts_waiting_for_dry_run():
                    break
                stop.wait(0.2)
        finally:
            watcher.close()
            stop.set()

    watching = threading.Thread(target=watch, name="watch")
    watching.start()
    code = run(
        writes_off(database, proxy.url),
        stop,
        slack_client=slack,
        connect=lambda app, settings: FakeConnection(),
        clock=lambda: datetime.fromtimestamp(float(newest) + 120, UTC),
    )
    watching.join()
    assert code == 0
    log = proxy.log
    assert {entry.method for entry in log} == {"GET"}, sorted({(entry.method, entry.path) for entry in log})
    assert any(entry.path.startswith("/api/stock/") for entry in log)  # the dry runs read the stock
    after = Store.open(database)
    try:
        assert not after.events_pending()
        assert not after.posts_waiting_for_dry_run()
        assert after.request(key).state is RequestState.APPROVED
        assert after.recorded_requests(EARLIER)[0].attempts == ()
    finally:
        after.close()
    assert len({reply["thread_ts"] for reply in slack.posted}) >= len(cases()) // 2
