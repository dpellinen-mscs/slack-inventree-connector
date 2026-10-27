# SPDX-License-Identifier: MIT
"""Alias learning end to end against a real server (ROADMAP M3 DoD): the worker as `stockthread run` builds it with
writes on, the catalog, stock, policy, Apply click, write and read-back all from InvenTree, and Slack faked.

A take-out whose part was a whole-word match is asked about, waits for Apply, and teaches its text once verified;
the same words posted again apply without a click; a new part that fits the words makes them a question again."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from stockthread.catalog import with_aliases
from stockthread.config import Settings, parse_settings
from stockthread.core.matching import Alias
from stockthread.core.model import PostKey, RequestKey
from stockthread.core.requests import RequestState
from stockthread.run import (
    CATALOG_REFRESH_SECONDS,
    CatalogCache,
    click_approver,
    inventree_catalog,
    inventree_steps,
    inventree_stock,
    policy_approver,
)
from stockthread.slack.replies import APPLY_BLOCK
from stockthread.store import AliasKind, Store
from stockthread.worker import Worker

if TYPE_CHECKING:
    from conftest import Scratch

OWNER = "learn-it"
WORKSPACE, APP, CHANNEL = "T0ACME001", "A0ACME001", "C0ACME001"
SAM, LUIS = "U0SAM0001", "U0LUIS001"


class FakeSlack:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.notes: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posted.append(kwargs)
        return {"ok": True, "ts": self.ts(len(self.posted) - 1)}

    def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:
        self.notes.append(kwargs)
        return {"ok": True}

    def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updated.append(kwargs)
        return {"ok": True, "ts": kwargs["ts"]}

    @staticmethod
    def ts(index: int) -> str:
        return f"1999999999.{index + 1:06d}"


class Clock:
    """Real time at the start, moved on by the test: plans expire a day after their post, as the worker counts it."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.seconds = 0.0

    def __call__(self) -> datetime:
        return self.now

    def ts(self) -> str:
        return f"{int(self.now.timestamp())}.000100"

    def wait(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.seconds += seconds


def letters(tag: str) -> str:
    """A hex tag with letters for digits: no number in a post but its quantity."""
    return tag.translate(str.maketrans("0123456789", "ghijklmnop"))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store.open(tmp_path / "stockthread.db")
    opened.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    yield opened
    opened.close()


def settings(truck: str) -> Settings:
    token = Path(os.environ["STOCKTHREAD_IT_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    data = {
        "slack": {"workspace_id": WORKSPACE, "app_id": APP, "channels": [CHANNEL], "approvers": [LUIS]},
        "inventree": {"url": os.environ["STOCKTHREAD_IT_URL"], "timeout_seconds": 10},
        "writes": {"enabled": True},
        "apply": {"settle_seconds": 60},
        "vehicles": {SAM: truck},
    }
    env = {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic", "INVENTREE_TOKEN": token}
    return parse_settings(data, env)


def connector(store: Store, clock: Clock, configured: Settings) -> Worker:
    """The worker as `run.run` wires it once writes are on and the server's version is tested."""
    cache = CatalogCache(inventree_catalog(configured), clock=lambda: clock.seconds)
    catalog = with_aliases(cache, store)
    worker = Worker(configured, store, OWNER, FakeSlack(), catalog, clock, stock=inventree_stock(configured))  # type: ignore[arg-type]
    worker.apply, worker.undo = inventree_steps(configured, cache, store, OWNER, beat=lambda: None, clock=clock)
    worker.approve = policy_approver(configured, cache, store, OWNER, beat=lambda: None)
    worker.approve_click = click_approver(configured, cache, store, OWNER, beat=lambda: None)
    return worker


def slack(worker: Worker) -> FakeSlack:
    assert isinstance(worker.slack, FakeSlack)
    return worker.slack


def post(store: Store, clock: Clock, text: str) -> PostKey:
    ts = clock.ts()
    event = {"type": "message", "channel": CHANNEL, "user": SAM, "text": text, "ts": ts}
    payload = {"type": "event_callback", "team_id": WORKSPACE, "api_app_id": APP, "event_id": f"Ev{ts}", "event": event}
    assert store.enqueue_event(f"Ev{ts}:event", "event", json.dumps(payload))
    return PostKey(WORKSPACE, CHANNEL, ts)


def click(store: Store, clock: Clock, worker: Worker, root: PostKey, user: str, button: dict[str, Any]) -> None:
    """Enqueue what Slack sends when `user` clicks `button` on the newest reply."""
    index = len(slack(worker).posted) - 1
    ts = FakeSlack.ts(index)
    payload = {
        "type": "block_actions",
        "api_app_id": APP,
        "team": {"id": WORKSPACE},
        "user": {"id": user, "team_id": WORKSPACE},
        "channel": {"id": CHANNEL},
        "container": {"type": "message", "message_ts": ts, "channel_id": CHANNEL, "is_ephemeral": False},
        "message": {"type": "message", "ts": ts, "thread_ts": root.ts, "text": slack(worker).posted[index]["text"]},
        "actions": [{**button, "action_ts": f"{int(clock.now.timestamp())}.000200"}],
    }
    assert store.enqueue_event(f"click:{uuid.uuid4().hex}", "interactive", json.dumps(payload))


def run(worker: Worker, clock: Clock, seconds: float = 30) -> None:
    """Let the worker reply, judge, write, read back and post receipts, as its loop would over `seconds`."""
    clock.wait(seconds)
    while worker.step():
        pass


def buttons(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [element for block in body["blocks"] if block["type"] == "actions" for element in block["elements"]]


def apply_button(body: dict[str, Any]) -> dict[str, Any] | None:
    found = [block for block in body["blocks"] if block.get("block_id") == APPLY_BLOCK]
    return dict(found[0]["elements"][0]) if found else None


def on_truck(scratch: Scratch, part: int, truck: int) -> Decimal:
    return sum((Decimal(str(row["quantity"])) for row in scratch.api.items(part, truck)), Decimal(0))


@dataclass(frozen=True)
class Kestrel:
    """A fresh part that only "kestrel <tag>" names by whole words, ten on a shelf, and Sam's truck."""

    tag: str
    part: int
    shelf: int
    truck: int

    @property
    def said(self) -> str:
        return f"took 2 kestrel {self.tag} from shelf {self.tag} to my truck"


@pytest.fixture
def kestrel(scratch: Scratch) -> Kestrel:
    tag = letters(uuid.uuid4().hex[:8])
    api = scratch.api
    root = int(api.get("/api/stock/location/", name="Characterization")[0]["pk"])
    shelf = int(api.create("/api/stock/location/", {"name": f"Shelf {tag}", "parent": root})["pk"])
    truck = int(api.create("/api/stock/location/", {"name": f"Truck {tag}", "parent": root})["pk"])
    part = int(api.create("/api/part/", {"name": f"Kestrel {tag} Access Point", "description": "test",
                                          "component": True})["pk"])  # fmt: skip
    api.create("/api/stock/", {"part": part, "location": shelf, "quantity": 10})
    return Kestrel(tag, part, shelf, truck)


def answered(store: Store, clock: Clock, worker: Worker, said: str) -> PostKey:
    """Sam's post, the thread's question about its whole-word match, and Sam's Yes: the post's plan, waiting."""
    first = post(store, clock, said)
    run(worker, clock)
    assert store.current_plan(first) is None
    [asked] = slack(worker).posted
    [yes] = [button for button in buttons(asked) if button["text"]["text"] == "Yes"]
    click(store, clock, worker, first, SAM, yes)
    run(worker, clock)
    plan = store.current_plan(first)
    assert plan is not None, slack(worker).posted[-1]["text"]
    assert [row.state for row in plan.requests] == [RequestState.PLANNED]  # answered: the policy does not approve it
    return first


def test_a_whole_word_take_out_waits_for_apply_once_then_applies_alone_until_a_new_part_fits_its_words(
    store: Store, scratch: Scratch, kestrel: Kestrel
) -> None:
    clock = Clock()
    worker = connector(store, clock, settings(f"Characterization/Truck {kestrel.tag}"))
    first = answered(store, clock, worker, kestrel.said)
    plan = store.current_plan(first)
    assert plan is not None
    key = RequestKey(first, plan.revision, 1)
    button = apply_button(slack(worker).posted[-1])
    assert button is not None
    click(store, clock, worker, first, LUIS, button)
    run(worker, clock)
    assert store.request(key).state is RequestState.VERIFIED
    assert on_truck(scratch, kestrel.part, kestrel.truck) == Decimal(2)
    learned = [alias for alias in store.aliases(AliasKind.PART) if alias.record == kestrel.part]
    assert learned == [Alias(f"kestrel {kestrel.tag}", kestrel.part, None, frozenset({kestrel.part}))]
    assert "Learned from this Apply" in json.dumps(slack(worker).posted[-1]["blocks"])

    # Second post, same words: an alias match, applied by the policy with no question and no click.
    replies = len(slack(worker).posted)
    second = post(store, clock, kestrel.said)
    run(worker, clock)
    again = store.current_plan(second)
    assert again is not None
    assert again.approved_by is None
    assert store.request(RequestKey(second, again.revision, 1)).state is RequestState.VERIFIED
    assert on_truck(scratch, kestrel.part, kestrel.truck) == Decimal(4)
    assert len(slack(worker).posted) == replies + 2  # the dry run and its receipt, no question
    assert "Learned" not in json.dumps(slack(worker).posted[-1]["blocks"])

    # A new part that fits the words: once the catalog is read again, the alias is a question again.
    scratch.api.create("/api/part/", {"name": f"Kestrel {kestrel.tag} Wall Bracket", "description": "test",
                                      "component": True})  # fmt: skip
    clock.wait(CATALOG_REFRESH_SECONDS)
    replies = len(slack(worker).posted)
    third = post(store, clock, kestrel.said)
    run(worker, clock)
    assert store.current_plan(third) is None
    assert len(slack(worker).posted) == replies + 1
    assert on_truck(scratch, kestrel.part, kestrel.truck) == Decimal(4)


@pytest.mark.parametrize("why", ["writes off", "expired"])
def test_a_confirmed_dry_run_or_an_expired_plan_teaches_nothing(
    store: Store, scratch: Scratch, kestrel: Kestrel, why: str
) -> None:
    clock = Clock()
    configured = settings(f"Characterization/Truck {kestrel.tag}")
    worker = connector(store, clock, configured)
    if why == "writes off":
        worker.apply = worker.undo = worker.approve = worker.approve_click = None
    first = answered(store, clock, worker, kestrel.said)
    button = apply_button(slack(worker).posted[-1]) or {"type": "button", "action_id": "apply", "value": "2"}
    if why == "expired":
        clock.wait(timedelta(hours=configured.slack.plan_expiry_hours, minutes=1).total_seconds())
    click(store, clock, worker, first, LUIS, button)
    run(worker, clock)
    told = {"writes off": "Not applied: writes are turned off in the connector's settings.",
            "expired": "Not applied: the plan expired."}  # fmt: skip
    assert slack(worker).notes[-1]["text"] == told[why]
    assert store.stored_aliases() == ()
    assert on_truck(scratch, kestrel.part, kestrel.truck) == Decimal(0)
    # The next post with the same words is asked about again.
    second = post(store, clock, kestrel.said)
    run(worker, clock)
    assert store.current_plan(second) is None
