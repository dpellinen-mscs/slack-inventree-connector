# SPDX-License-Identifier: MIT
"""The Undo button on a receipt, clicked: the worker's checks against the stored request (the button's value, the
newest receipt of the plan, the poster or an approver, `applier.undo_refusal`), then `apply.undo_apply` against the
fake InvenTree of test_apply_request.py, and what the clicker alone is told."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
import pytest
from fake_inventree import ITEM, FakeInvenTree
from views import DESTINATION, SOURCE, picked, view

from stockthread.applier import GateNow, approve_by_policy, work_request
from stockthread.apply import Applied, undo_apply
from stockthread.config import Settings, parse_settings
from stockthread.core.matching import Catalog
from stockthread.core.model import Extraction, MovementType, PostKey, RequestKey
from stockthread.core.requests import RequestState
from stockthread.dry_run import dry_run_record
from stockthread.gate import Policy
from stockthread.inventree import DEPLETING_MARKER_FOUND_FROM, ServerInfo, VersionCheck
from stockthread.plan_record import plan_of
from stockthread.slack.replies import CLICK_FEEDBACK, MovementView, receipt
from stockthread.store import DryRunOutcome, NewPlan, Store
from stockthread.worker import UNDO_ALREADY, UNDO_ERROR, UNDO_FEEDBACK, UNDO_WRITES_OFF, Undoer, Worker

S = RequestState
OWNER = "worker-1"
WORKSPACE, APP, CHANNEL = "T0ACME001", "A0ACME001", "C0ACME001"
SAM, LUIS, ANA = "U0SAM0001", "U0LUIS001", "U0ANA0001"
POST = PostKey(WORKSPACE, CHANNEL, "1700000000.000100")
KEY = RequestKey(POST, 1, 1)
RECEIPT_TS = "1700000200.000100"
TAKE = view(stock=(picked((ITEM, "2")),))
"""Sam takes all 2 of item 57 from location 10 to his truck, location 11: a clear take-out the policy approves."""
TESTED = VersionCheck(ServerInfo("1.5.6", DEPLETING_MARKER_FOUND_FROM), True, "tested")
ON = GateNow(
    Policy(True, frozenset({MovementType.TAKE_TO_VEHICLE, MovementType.TAKE_TO_SITE}), frozenset({LUIS}), TESTED),
    {SAM: DESTINATION},
)
ENV = {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic", "INVENTREE_TOKEN": "inv-synthetic"}


def settings(max_age_hours: int = 168, channels: tuple[str, ...] = (CHANNEL,)) -> Settings:
    data = {
        "slack": {"workspace_id": WORKSPACE, "app_id": APP, "channels": list(channels), "approvers": [LUIS]},
        "inventree": {"url": "http://inventree.local:8000"},
        "undo": {"max_age_hours": max_age_hours},
    }
    return parse_settings(data, ENV)


class FakeSlack:
    def __init__(self) -> None:
        self.notes: list[dict[str, Any]] = []

    def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:
        self.notes.append(kwargs)
        return {"ok": True}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store.open(tmp_path / "stockthread.db")
    opened.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    opened.place_message(OWNER, POST, user_id=SAM, thread_ts=None, window_seconds=5)
    yield opened
    opened.close()


@pytest.fixture
def fake(store: Store) -> FakeInvenTree:
    """The fake after the take-out was approved by the policy, written, read back and its receipt posted."""
    extraction = Extraction("rules", (TAKE.movement,))
    result = json.loads(
        json.dumps(dry_run_record(extraction, [TAKE], late=False, poster=SAM, messages=(), max_quantity=500))
    )
    made = plan_of(result)
    assert made is not None
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, "1700000100.000100", result=result, plan=NewPlan(*made))
    approve_by_policy(store, OWNER, POST, ON)
    inventree = FakeInvenTree()
    inventree.lock = anyio.Lock()

    async def write() -> Applied:
        assert inventree.lock is not None
        return await work_request(
            KEY,
            store=store,
            reader=inventree,
            writer=inventree,
            lock=inventree.lock,
            lock_owner=OWNER,
            gate_now=lambda: ON,
            expired=lambda _: False,
            settle=timedelta(seconds=120),
        )

    assert anyio.run(write).state is S.VERIFIED
    store.receipt_posted(OWNER, POST, 1, RECEIPT_TS, [S.VERIFIED])
    return inventree


def undoer(store: Store, inventree: FakeInvenTree) -> Undoer:
    def run(key: RequestKey, movement: MovementView, user: str) -> Applied:
        async def undo() -> Applied:
            assert inventree.lock is not None
            return await undo_apply(
                key, movement, store=store, reader=inventree, writer=inventree, lock=inventree.lock,
                lock_owner=OWNER, user=user,
            )  # fmt: skip

        return anyio.run(undo)

    return run


def worker(store: Store, undo: Undoer | None, **changed: Any) -> Worker:
    return Worker(
        changed.get("settings", settings()),
        store,
        OWNER,
        FakeSlack(),  # type: ignore[arg-type]
        lambda: Catalog(parts=(), locations=()),
        lambda: datetime.fromtimestamp(1700000300, UTC),
        undo=undo,
    )


def receipt_body(store: Store) -> dict[str, Any]:
    marker_id = store.request(KEY).marker_id
    return receipt(
        [TAKE], [S.VERIFIED], [marker_id], {}, {}, inventree_url="http://inventree.local:8000", approved_by=None,
        undo=[marker_id],
    )  # fmt: skip


def click(
    store: Store,
    user: str = SAM,
    *,
    ts: str = RECEIPT_TS,
    value: str | None = None,
    event: str = "1",
    channel: str = CHANNEL,
) -> None:
    """Enqueue the payload Slack sends when `user` clicks the Undo button of the receipt posted at `ts`."""
    [section] = [block for block in receipt_body(store)["blocks"] if "accessory" in block]
    button = dict(section["accessory"])
    if value is not None:
        button["value"] = value
    payload = {
        "type": "block_actions",
        "api_app_id": APP,
        "team": {"id": WORKSPACE},
        "user": {"id": user, "team_id": WORKSPACE},
        "channel": {"id": channel},
        "container": {"type": "message", "message_ts": ts, "channel_id": channel, "is_ephemeral": False},
        "message": {"type": "message", "ts": ts, "thread_ts": POST.ts, "text": "Applied: 1 movement."},
        "actions": [{**button, "action_ts": "1700000300.000100"}],
    }
    assert store.enqueue_event(f"click:{event}", "interactive", json.dumps(payload))


def finished(store: Store) -> list[tuple[str, str | None]]:
    return [(row[0], row[1]) for row in store._db.execute("SELECT status, outcome FROM slack_events ORDER BY id")]


def told(handled: Worker) -> list[tuple[str, str]]:
    notes = handled.slack.notes  # type: ignore[attr-defined]
    assert all((note["channel"], note["thread_ts"]) == (CHANNEL, POST.ts) for note in notes)
    return [(note["user"], note["text"]) for note in notes]


def at_source(inventree: FakeInvenTree) -> bool:
    return bool(inventree.items[ITEM][1] == SOURCE)


def test_the_poster_undoes_a_movement_by_its_receipt_button(store: Store, fake: FakeInvenTree) -> None:
    assert fake.items[ITEM][1] == DESTINATION
    handled = worker(store, undoer(store, fake))
    click(store)
    handled.step()
    assert store.request(KEY).state is S.UNDONE
    assert at_source(fake)
    assert finished(store) == [("done", "undone")]
    assert told(handled) == [(SAM, UNDO_FEEDBACK[S.UNDONE])]
    assert str(fake.sent[-1].body["notes"]).startswith(f"[sic:{store.request(KEY).marker_id}:u01]")


def test_two_undo_clicks_move_stock_once(store: Store, fake: FakeInvenTree) -> None:
    handled = worker(store, undoer(store, fake))
    click(store, event="1")
    click(store, LUIS, event="2")
    handled.step()
    assert len(fake.sent) == 2  # the apply, then one undo
    assert finished(store) == [("done", "undone"), ("dropped", "undo_refused")]
    assert told(handled)[1] == (LUIS, UNDO_ALREADY)


def test_a_click_handled_again_after_the_undo_started_never_undoes_twice(store: Store, fake: FakeInvenTree) -> None:
    handled = worker(store, undoer(store, fake))
    click(store, event="1")
    handled.step()
    fake.items[ITEM][1] = DESTINATION  # as if the stock had moved back by hand: the same click must still not undo
    # A crash after the undo and before the event was finished: the claimed event is handled again.
    ((event_id, body),) = store._db.execute("SELECT id, body FROM slack_events").fetchall()
    assert handled._handle_event(event_id, "interactive", body) == ("dropped", "undo_refused")
    assert len(fake.sent) == 2


def test_an_approver_may_undo_and_anyone_else_may_not(store: Store, fake: FakeInvenTree) -> None:
    handled = worker(store, undoer(store, fake))
    click(store, ANA, event="1")
    handled.step()
    assert finished(store) == [("dropped", "undo_refused")]
    assert told(handled) == [(ANA, "Not undone: only the poster or an approver may undo this movement.")]
    assert len(fake.sent) == 1
    click(store, LUIS, event="2")
    handled.step()
    assert store.request(KEY).state is S.UNDONE


@pytest.mark.parametrize(
    ("change", "outcome"),
    [
        ({"value": "0" * 22}, "unknown_button"),  # a marker id no request has
        ({"value": "not a marker id"}, "unknown_button"),
        ({"ts": "1700000150.000100"}, "other_message"),  # an older receipt of the plan, or any other message
    ],
)
def test_a_click_that_does_not_fit_the_newest_receipt_undoes_nothing(
    store: Store, fake: FakeInvenTree, change: dict[str, str], outcome: str
) -> None:
    handled = worker(store, undoer(store, fake))
    click(store, **change)
    handled.step()
    assert finished(store) == [("dropped", outcome)]
    assert told(handled) == [(SAM, CLICK_FEEDBACK[outcome])]
    assert len(fake.sent) == 1
    assert store.request(KEY).state is S.VERIFIED


def test_a_click_on_a_receipt_in_another_channel_undoes_nothing(store: Store, fake: FakeInvenTree) -> None:
    other = "C0ACME002"
    handled = worker(store, undoer(store, fake), settings=settings(channels=(CHANNEL, other)))
    click(store, channel=other)
    handled.step()
    assert finished(store) == [("dropped", "other_message")]
    assert len(fake.sent) == 1


def test_with_writes_off_no_undo_click_writes(store: Store, fake: FakeInvenTree) -> None:
    handled = worker(store, None)
    click(store)
    handled.step()
    assert finished(store) == [("dropped", "writes_off")]
    assert told(handled) == [(SAM, UNDO_WRITES_OFF)]
    assert store.request(KEY).state is S.VERIFIED


def test_an_undo_asked_for_after_max_age_is_refused(store: Store, fake: FakeInvenTree) -> None:
    store._clock_now = lambda: datetime.now(UTC) + timedelta(hours=2)  # type: ignore[method-assign]
    handled = worker(store, undoer(store, fake), settings=settings(max_age_hours=1))
    click(store)
    handled.step()
    assert finished(store) == [("dropped", "undo_refused")]
    assert "more than 1 hours ago" in told(handled)[0][1]
    assert len(fake.sent) == 1


def test_an_undo_inventree_refused_says_so(store: Store, fake: FakeInvenTree) -> None:
    from stockthread.writer import Outcome

    fake.answer = Outcome.REFUSED
    handled = worker(store, undoer(store, fake))
    click(store)
    handled.step()
    assert store.request(KEY).state is S.UNDO_FAILED
    assert finished(store) == [("done", "undo_failed")]
    assert told(handled) == [(SAM, UNDO_FEEDBACK[S.UNDO_FAILED])]


def test_an_undo_whose_read_back_fails_is_unknown_and_said_so(store: Store, fake: FakeInvenTree) -> None:
    fake.fail_reads_after_write = True
    fake.sent.clear()  # reads fail once the undo is sent
    handled = worker(store, undoer(store, fake))
    click(store)
    handled.step()
    assert store.request(KEY).state is S.UNDO_UNKNOWN
    assert finished(store) == [("done", "undo_unknown")]
    assert told(handled) == [(SAM, UNDO_FEEDBACK[S.UNDO_UNKNOWN])]


def test_a_read_error_before_anything_was_stored_tells_the_clicker_and_changes_nothing(
    store: Store, fake: FakeInvenTree
) -> None:
    fake.fail_reads = True
    handled = worker(store, undoer(store, fake))
    click(store)
    handled.step()
    assert store.request(KEY).state is S.VERIFIED
    assert finished(store) == [("failed", "error_inven_tree_error")]
    assert told(handled) == [(SAM, UNDO_ERROR)]
    assert len(fake.sent) == 1


def test_a_network_error_before_anything_was_stored_tries_the_click_again_later(
    store: Store, fake: FakeInvenTree
) -> None:
    calls = []

    def flaky(key: RequestKey, movement: MovementView, user: str) -> Applied:
        calls.append(key)
        if len(calls) == 1:
            raise ConnectionError("synthetic outage")
        return undoer(store, fake)(key, movement, user)

    handled = worker(store, flaky)
    click(store)
    handled.step()
    assert store.request(KEY).state is S.VERIFIED
    assert finished(store) == [("pending", None)]
    assert told(handled) == []  # told only once it is no longer tried again
    later = datetime.now(UTC) + timedelta(hours=1)
    store._clock_now = lambda: later  # type: ignore[method-assign]
    handled.step()
    assert len(calls) == 2
    assert finished(store) == [("done", "undone")]
    assert told(handled) == [(SAM, UNDO_FEEDBACK[S.UNDONE])]


def test_the_quantity_is_back_exactly(store: Store, fake: FakeInvenTree) -> None:
    handled = worker(store, undoer(store, fake))
    click(store)
    handled.step()
    assert fake.items[ITEM] == [1, SOURCE, Decimal("2")]


def test_an_undo_that_ends_in_the_state_it_started_from_is_reported_as_its_outcome(
    store: Store, fake: FakeInvenTree
) -> None:
    from stockthread.writer import Outcome

    fake.answer = Outcome.REFUSED
    handled = worker(store, undoer(store, fake))
    click(store, event="1")
    click(store, event="2")  # undo_failed may be undone again: refused again, it ends where it started
    handled.step()
    assert len(fake.sent) == 3
    assert store.request(KEY).state is S.UNDO_FAILED
    assert finished(store) == [("done", "undo_failed"), ("done", "undo_failed")]
    assert told(handled) == [(SAM, UNDO_FEEDBACK[S.UNDO_FAILED])] * 2
