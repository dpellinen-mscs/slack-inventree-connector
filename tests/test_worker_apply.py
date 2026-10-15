"""The worker's apply step and receipts: `Worker.work_due` runs the step of the apply protocol each request is due for
(through `applier.work_request` against the fake InvenTree of test_apply_request.py), and `Worker.post_receipts` posts
the receipt each plan is due in its thread and records it."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from fake_inventree import ITEM, FakeInvenTree
from slack_sdk.errors import SlackApiError
from slack_sdk.web import SlackResponse
from views import DESTINATION, picked, view

from stockthread import worker as worker_module
from stockthread.applier import GateNow, approve_by_policy, work_request
from stockthread.apply import Applied, undo_apply
from stockthread.config import Settings, parse_settings
from stockthread.core.matching import Catalog
from stockthread.core.model import Extraction, MovementType, PostKey, RequestKey
from stockthread.core.requests import RequestState
from stockthread.gate import Policy
from stockthread.inventree import (
    DEPLETING_MARKER_FOUND_FROM,
    InvenTreeError,
    ServerInfo,
    VersionCheck,
    check_version,
)
from stockthread.plan_record import NotRebuilt, plan_of
from stockthread.run import inventree_steps
from stockthread.slack.replies import UNDO_ACTION, MovementView, receipt_replaced
from stockthread.store import DryRunOutcome, LockLost, NewPlan, Store
from stockthread.worker import (
    RECEIPT_TRIES,
    STUCK_RETRY_SECONDS,
    WORK_RETRY_SECONDS,
    PosterBusy,
    PostStillRunning,
    Stepper,
    Undoer,
    Worker,
    _record,
)
from stockthread.writer import Outcome

S = RequestState
OWNER = "worker-1"
WORKSPACE, APP, CHANNEL = "T0ACME001", "A0ACME001", "C0ACME001"
SAM, LUIS = "U0SAM0001", "U0LUIS001"
POST = PostKey(WORKSPACE, CHANNEL, "1700000000.000100")
KEY = RequestKey(POST, 1, 1)
TAKE = view(stock=(picked((ITEM, "2")),))
"""Sam takes all 2 of item 57 from location 10 to his truck, location 11: a clear take-out the policy approves."""
TESTED = VersionCheck(ServerInfo("1.5.6", DEPLETING_MARKER_FOUND_FROM), True, "tested")
ON = GateNow(
    Policy(True, frozenset({MovementType.TAKE_TO_VEHICLE, MovementType.TAKE_TO_SITE}), frozenset({LUIS}), TESTED),
    {SAM: DESTINATION},
)
ENV = {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic", "INVENTREE_TOKEN": "inv-synthetic"}
START = 1700000300


def settings(max_age_hours: int = 168, *, writes: bool = False) -> Settings:
    data = {
        "slack": {"workspace_id": WORKSPACE, "app_id": APP, "channels": [CHANNEL], "approvers": [LUIS]},
        "inventree": {"url": "http://inventree.local:8000", "timeout_seconds": 1},
        "undo": {"max_age_hours": max_age_hours},
        "writes": {"enabled": writes},
    }
    return parse_settings(data, ENV)


class FakeSlack:
    """Posts get ts 1700000400.000001, .000002, ...; `fail` raises on the next posts; `hang`: they are still running."""

    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.fail: list[Exception] = []
        self.hang = False
        self.ts: str | None = None

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail:
            raise self.fail.pop(0)
        if self.hang:
            raise PostStillRunning
        self.posted.append(kwargs)
        ts = self.ts if self.ts is not None else f"1700000400.{len(self.posted):06d}"
        return {"ok": True, "ts": ts}

    def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updated.append(kwargs)
        return {"ok": True}


class Clock:
    def __init__(self) -> None:
        self.now = float(START)

    def __call__(self) -> datetime:
        return datetime.fromtimestamp(self.now, UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store.open(tmp_path / "stockthread.db")
    opened.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    yield opened
    opened.close()


def approved(store: Store, post: PostKey = POST) -> None:
    """The take-out's dry run posted in `post`'s thread, and its plan approved by the policy."""
    store.place_message(OWNER, post, user_id=SAM, thread_ts=None, window_seconds=5)
    extraction = Extraction("rules", (TAKE.movement,))
    result = json.loads(json.dumps(_record(extraction, [TAKE], late=False, poster=SAM, messages=(), max_quantity=500)))
    made = plan_of(result)
    assert made is not None
    store.finish_dry_run(OWNER, post, DryRunOutcome.REPLIED, "1700000100.000100", result=result, plan=NewPlan(*made))
    judged = approve_by_policy(store, OWNER, post, ON)
    assert judged is not None
    assert judged.allowed


def stepper(store: Store, inventree: FakeInvenTree) -> Stepper:
    inventree.lock = inventree.lock or anyio.Lock()

    def run(key: RequestKey) -> Applied:
        async def step() -> Applied:
            assert inventree.lock is not None
            return await work_request(
                key,
                store=store,
                reader=inventree,
                writer=inventree,
                lock=inventree.lock,
                lock_owner=OWNER,
                gate_now=lambda: ON,
                settle=timedelta(0),
            )

        return anyio.run(step)

    return run


def undoer(store: Store, inventree: FakeInvenTree) -> Undoer:
    inventree.lock = inventree.lock or anyio.Lock()

    def run(key: RequestKey, movement: MovementView, user: str) -> Applied:
        async def undo() -> Applied:
            assert inventree.lock is not None
            return await undo_apply(
                key, movement, store=store, reader=inventree, writer=inventree, lock=inventree.lock,
                lock_owner=OWNER, user=user,
            )  # fmt: skip

        return anyio.run(undo)

    return run


def worker(
    store: Store, apply: Stepper | None, undo: Undoer | None = None, *, clock: Clock | None = None, **changed: Any
) -> Worker:
    return Worker(
        changed.get("settings", settings()),
        store,
        OWNER,
        FakeSlack(),  # type: ignore[arg-type]
        lambda: Catalog(parts=(), locations=()),
        clock or Clock(),
        undo=undo,
        apply=apply,
    )


def slack(running: Worker) -> FakeSlack:
    assert isinstance(running.slack, FakeSlack)
    return running.slack


def buttons(body: dict[str, Any]) -> list[str]:
    return [block["accessory"]["value"] for block in body["blocks"] if "accessory" in block]


def rate_limited(seconds: int) -> SlackApiError:
    response = SlackResponse(
        client=None, http_verb="POST", api_url="https://slack.invalid/api/chat.postMessage", req_args={},
        data={"ok": False, "error": "ratelimited"}, headers={"Retry-After": str(seconds)}, status_code=429,
    )  # fmt: skip
    return SlackApiError("ratelimited", response)  # type: ignore[no-untyped-call]


# --- work_due ---


def test_with_writes_off_no_step_runs(store: Store) -> None:
    approved(store)
    inventree = FakeInvenTree()
    assert worker(store, None).work_due() == 0
    assert store.request(KEY).state is S.APPROVED
    assert inventree.sent == []


def test_an_approved_request_is_written_and_read_back_once(store: Store) -> None:
    approved(store)
    inventree = FakeInvenTree()
    running = worker(store, stepper(store, inventree))
    assert running.work_due() == 1
    assert store.request(KEY).state is S.VERIFIED
    assert len(inventree.sent) == 1
    assert inventree.items[ITEM][1] == DESTINATION
    assert running.work_due() == 0  # nothing is due any more: never sent twice
    assert len(inventree.sent) == 1


def test_an_unknown_apply_is_recovered_by_the_worker_and_never_sent_again(store: Store) -> None:
    approved(store)
    inventree = FakeInvenTree()
    inventree.answer, inventree.lands = Outcome.UNKNOWN, True
    running = worker(store, stepper(store, inventree))
    assert running.work_due() == 1
    assert store.request(KEY).state is S.UNKNOWN
    inventree.answer, inventree.lands = Outcome.ACCEPTED, False
    assert running.work_due() == 1
    assert store.request(KEY).state is S.VERIFIED  # the marker was found and read back
    assert len(inventree.sent) == 1


def test_an_unknown_undo_is_recovered_by_the_worker(store: Store) -> None:
    approved(store)
    inventree = FakeInvenTree()
    running = worker(store, stepper(store, inventree))
    assert running.work_due() == 1
    inventree.answer, inventree.lands = Outcome.UNKNOWN, True
    assert undoer(store, inventree)(KEY, TAKE, SAM).state is S.UNDO_UNKNOWN
    inventree.answer, inventree.lands = Outcome.ACCEPTED, False
    assert store.requests_to_work() == [KEY]
    assert running.work_due() == 1
    assert store.request(KEY).state is S.UNDONE
    assert len(inventree.sent) == 2  # the apply and the undo, each once
    assert store.requests_to_work() == []


def deletion(store: Store, post: PostKey) -> None:
    """Sam deletes `post`'s message: the event waits in the queue."""
    event = {"type": "message", "subtype": "message_deleted", "channel": post.channel_id, "ts": "1700000500.000100"}
    event["deleted_ts"] = post.ts
    payload = {"type": "event_callback", "team_id": WORKSPACE, "api_app_id": APP, "event_id": "EvDel", "event": event}
    assert store.enqueue_event(f"EvDel:{post.ts}", "event", json.dumps(payload))


def test_no_stock_is_written_while_an_edit_or_deletion_waits_in_the_queue(store: Store) -> None:
    """Review #135 finding 1: a deletion that came while the step was busy (a slow Slack reply, say) is handled
    before any approval is acted on, and takes it back."""
    approved(store)
    inventree = FakeInvenTree()
    running = worker(store, stepper(store, inventree))
    deletion(store, POST)
    assert running.work_due() == 0
    assert inventree.sent == []
    assert store.request(KEY).state is S.APPROVED
    running.step()  # drains the deletion first
    assert inventree.sent == []
    assert store.request(KEY).state is not S.APPROVED
    assert running.work_due() == 0
    assert inventree.sent == []


def test_a_deletion_that_comes_during_one_apply_is_handled_before_the_next(store: Store) -> None:
    other = PostKey(WORKSPACE, CHANNEL, "1700001000.000100")  # not grouped with POST
    approved(store)
    approved(store, other)
    inventree = FakeInvenTree()
    step = stepper(store, inventree)
    seen: list[RequestKey] = []

    def slow(key: RequestKey) -> Applied:
        seen.append(key)
        if len(seen) == 1:
            deletion(store, other if key.post == POST else POST)
        return step(key)

    running = worker(store, slow)
    assert running.work_due() == 0
    assert len(seen) == 1  # the other request waits for the next step, which drains the deletion first
    assert inventree.sent == []  # any event in the queue holds the write that was being read for back too
    first = seen[0]
    running.step()
    assert len(inventree.sent) == 1
    assert store.request(first).state is S.VERIFIED
    assert all(store.request(key).state is not S.APPROVED for key in (KEY, RequestKey(other, 1, 1)))


def test_a_deletion_that_comes_during_the_reads_before_the_write_stops_the_write(store: Store) -> None:
    """Review #137 finding 1: the queue was checked only before the step, whose reads take time. A deletion that came
    during them is in the queue when the attempt would begin, so nothing is sent, and draining it takes the approval
    back for good."""
    approved(store)
    inventree = FakeInvenTree()
    read = inventree.stock_items

    async def deleted_meanwhile(part: int, location: int) -> list[dict[str, Any]]:
        if not store.events_pending():
            deletion(store, POST)
        return await read(part, location)

    inventree.stock_items = deleted_meanwhile  # type: ignore[method-assign]
    running = worker(store, stepper(store, inventree))
    assert running.work_due() == 0
    assert inventree.sent == []
    assert store.request(KEY).state is S.APPROVED
    assert store.markers(KEY) == []
    running.step()  # drains the deletion first
    assert store.request(KEY).state is S.CANCELLED
    assert running.work_due() == 0
    assert inventree.sent == []


def test_an_outage_changes_nothing_and_pauses_every_step(store: Store) -> None:
    approved(store)
    inventree = FakeInvenTree()
    inventree.fail_reads = True
    clock = Clock()
    running = worker(store, stepper(store, inventree), clock=clock)
    assert running.work_due() == 0
    assert store.request(KEY).state is S.APPROVED
    inventree.fail_reads = False
    clock.now += WORK_RETRY_SECONDS - 1
    assert running.work_due() == 0  # still paused
    assert inventree.sent == []
    clock.now += 1
    assert running.work_due() == 1
    assert store.request(KEY).state is S.VERIFIED


@pytest.mark.parametrize(
    "error", [NotRebuilt("no dry-run record"), ValueError("not what it should be"), KeyError("no plan revision 1")]
)
def test_a_request_that_cannot_be_acted_on_is_an_error_and_others_go_on(
    store: Store, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    other = PostKey(WORKSPACE, CHANNEL, "1700000050.000100")
    approved(store)
    approved(store, other)
    inventree = FakeInvenTree()
    real = stepper(store, inventree)
    calls: list[RequestKey] = []

    def step(key: RequestKey) -> Applied:
        calls.append(key)
        if key.post == POST:
            raise error
        return real(key)

    clock = Clock()
    running = worker(store, step, clock=clock)
    with caplog.at_level(logging.ERROR, logger="stockthread.worker"):
        assert running.work_due() == 1
    assert store.request(RequestKey(other, 1, 1)).state is S.VERIFIED
    assert store.request(KEY).state is S.APPROVED
    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    assert "an operator must look at it" in caplog.records[0].getMessage()
    calls.clear()
    clock.now += WORK_RETRY_SECONDS - 1
    assert running.work_due() == 0
    assert calls == []  # not tried again before then: no error every second
    clock.now += 1
    running.work_due()
    assert calls == [KEY]
    clock.now += 2 * WORK_RETRY_SECONDS - 1  # held twice as long after a second failure
    running.work_due()
    assert calls == [KEY]
    clock.now += 1
    running.work_due()
    assert calls == [KEY, KEY]
    assert store.request(KEY).state is S.APPROVED


def test_an_outage_on_one_request_does_not_hold_the_others_back_for_good(store: Store) -> None:
    """InvenTree refuses every read of one request (an InvenTreeError, as an outage would raise): every step pauses
    for WORK_RETRY_SECONDS, but that request is held back longer each time, so the others get their turn."""
    other = PostKey(WORKSPACE, CHANNEL, "1700000050.000100")
    approved(store)
    approved(store, other)
    real = stepper(store, FakeInvenTree())
    calls: list[RequestKey] = []

    def step(key: RequestKey) -> Applied:
        calls.append(key)
        if key.post == POST:
            raise InvenTreeError("synthetic outage")
        return real(key)

    clock = Clock()
    running = worker(store, step, clock=clock)
    assert running.work_due() == 0
    assert calls == [KEY]  # first by post: the step paused at once
    clock.now += WORK_RETRY_SECONDS
    assert running.work_due() == 0
    assert calls == [KEY, KEY]
    clock.now += WORK_RETRY_SECONDS
    assert running.work_due() == 1  # KEY is held for 60 s now: the other request is written
    assert calls == [KEY, KEY, RequestKey(other, 1, 1)]
    assert store.request(RequestKey(other, 1, 1)).state is S.VERIFIED


def test_a_step_that_raises_after_its_request_moved_on_is_not_held_back(store: Store) -> None:
    approved(store)
    inventree = FakeInvenTree()
    inventree.answer, inventree.lands = Outcome.UNKNOWN, True
    real = stepper(store, inventree)

    def step(key: RequestKey) -> Applied:
        real(key)
        raise InvenTreeError("synthetic: after the write")

    clock = Clock()
    running = worker(store, step, clock=clock)
    assert running.work_due() == 1
    assert store.request(KEY).state is S.UNKNOWN
    inventree.answer, inventree.lands = Outcome.ACCEPTED, False
    running.apply = real
    assert running.work_due() == 1  # no pause, no hold: recovery goes on at once
    assert store.request(KEY).state is S.VERIFIED
    assert len(inventree.sent) == 1


def test_a_stop_request_stops_the_step_between_requests(store: Store) -> None:
    approved(store)
    approved(store, PostKey(WORKSPACE, CHANNEL, "1700000050.000100"))
    calls: list[RequestKey] = []
    running = worker(store, None)
    stop = [False]

    def step(key: RequestKey) -> Applied:
        calls.append(key)
        stop[0] = True
        return Applied(S.APPROVED, "synthetic")

    running.apply, running.stopping = step, lambda: stop[0]
    running.work_due()
    assert len(calls) == 1


def test_a_lost_lock_stops_the_step(store: Store) -> None:
    approved(store)

    def step(key: RequestKey) -> Applied:
        raise LockLost("another instance")

    with pytest.raises(LockLost):
        worker(store, step).work_due()


def test_the_step_runs_once_the_queue_is_drained_and_posts_the_receipt(store: Store) -> None:
    approved(store)
    inventree = FakeInvenTree()
    running = worker(store, stepper(store, inventree), undoer(store, inventree))
    assert running.step() >= 2
    assert store.request(KEY).state is S.VERIFIED
    [posted] = slack(running).posted
    assert (posted["channel"], posted["thread_ts"]) == (CHANNEL, POST.ts)
    assert posted["text"] == "Applied: 1 movement."
    assert store.plan(POST, 1).receipt_ts == "1700000400.000001"


# --- post_receipts ---


def written(store: Store, inventree: FakeInvenTree | None = None) -> FakeInvenTree:
    approved(store)
    inventree = inventree or FakeInvenTree()
    assert stepper(store, inventree)(KEY).state is S.VERIFIED
    return inventree


def test_a_receipt_is_posted_once_with_an_undo_button(store: Store) -> None:
    inventree = written(store)
    running = worker(store, None, undoer(store, inventree))
    assert running.post_receipts() == 1
    [posted] = slack(running).posted
    assert posted["unfurl_links"] is False
    assert buttons(posted) == [store.request(KEY).marker_id]
    [button] = [block["accessory"] for block in posted["blocks"] if "accessory" in block]
    assert button["action_id"] == UNDO_ACTION
    assert store.plan(POST, 1).receipt_ts == "1700000400.000001"
    assert store.receipts_due() == []
    assert running.post_receipts() == 0
    assert len(slack(running).posted) == 1


def test_with_undo_off_a_receipt_has_no_undo_button(store: Store) -> None:
    written(store)
    running = worker(store, None, None)
    assert running.post_receipts() == 1
    assert buttons(slack(running).posted[0]) == []


def test_a_receipt_has_no_undo_button_once_the_store_refuses_an_undo(tmp_path: Path) -> None:
    now = [datetime(2026, 10, 7, 9, 0, tzinfo=UTC)]
    store = Store.open(tmp_path / "clock.db", clock=lambda: now[0])
    store.acquire_lock(OWNER, stale_after=timedelta(days=30))
    inventree = written(store)
    now[0] += timedelta(hours=1)
    running = worker(store, None, undoer(store, inventree), settings=settings(max_age_hours=1))
    assert running.post_receipts() == 1  # written an hour ago: undo it in InvenTree
    assert buttons(slack(running).posted[0]) == []
    store.close()


def test_a_failed_receipt_backs_off_and_is_posted_later(store: Store) -> None:
    written(store)
    clock = Clock()
    running = worker(store, None, clock=clock)
    slack(running).fail = [ConnectionError("synthetic")]
    assert running.post_receipts() == 0
    assert store.plan(POST, 1).receipt_ts is None
    assert running.post_receipts() == 0  # paused: not tried again at once
    clock.now += 2
    assert running.post_receipts() == 1
    assert store.plan(POST, 1).receipt_ts == "1700000400.000001"


def test_a_rate_limited_receipt_waits_as_long_as_slack_asks_for_every_post(store: Store) -> None:
    written(store)
    clock = Clock()
    running = worker(store, None, clock=clock)
    slack(running).fail = [rate_limited(30)]
    assert running.post_receipts() == 0
    assert running._quiet_until == START + 30  # no dry-run reply is posted before then either
    clock.now += 29
    assert running.post_receipts() == 0
    clock.now += 1
    assert running.post_receipts() == 1


def test_retry_after_counts_from_when_slack_answered(store: Store) -> None:
    """Review #135 finding 2: a call that took 20 s and was asked to wait 30 s waits 30 s more, not 10."""
    written(store)
    clock = Clock()
    running = worker(store, None, clock=clock)

    def slow(**kwargs: Any) -> dict[str, Any]:
        clock.now += 20
        raise rate_limited(30)

    slack(running).chat_postMessage = slow  # type: ignore[method-assign]
    assert running.post_receipts() == 0
    del slack(running).chat_postMessage
    assert running._quiet_until == START + 50
    clock.now = START + 49
    assert running.post_receipts() == 0
    clock.now = START + 50
    assert running.post_receipts() == 1


def test_a_receipt_still_on_its_way_is_tried_again_as_it_was(store: Store) -> None:
    """The movement was undone while the receipt's call hung: the same message is tried again (taking that call's
    result), found out of date, edited to say it was replaced, and a new receipt reports the undo."""
    inventree = written(store)
    clock = Clock()
    running = worker(store, None, undoer(store, inventree), clock=clock)
    slack(running).hang = True
    assert running.post_receipts() == 0
    assert store.plan(POST, 1).receipt_ts is None
    assert undoer(store, inventree)(KEY, TAKE, SAM).state is S.UNDONE
    slack(running).hang = False
    clock.now += 5
    assert running.post_receipts() == 0  # posted, out of date, replaced
    assert running.post_receipts() == 1
    first, second = slack(running).posted
    assert first["text"] == "Applied: 1 movement."  # as it was handed to Slack
    assert buttons(first) == [store.request(KEY).marker_id]
    assert slack(running).updated == [
        {"channel": CHANNEL, "ts": "1700000400.000001", **receipt_replaced()},
    ]
    assert second["text"] == "Undone: 1 movement."
    assert buttons(second) == []
    assert store.plan(POST, 1).receipt_ts == "1700000400.000002"


def test_no_dry_run_reply_is_posted_while_a_receipt_is_on_its_way(store: Store) -> None:
    written(store)
    running = worker(store, None)
    slack(running).hang = True
    running.post_receipts()
    store.place_message(OWNER, PostKey(WORKSPACE, CHANNEL, "1700000200.000100"), user_id=SAM, thread_ts=None,
                        window_seconds=5)  # fmt: skip
    slack(running).hang = False
    assert running.reply_due() == 0
    assert slack(running).posted == []


def test_a_receipt_slack_refuses_does_not_hold_the_others_back(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    other = PostKey(WORKSPACE, CHANNEL, "1700000050.000100")
    written(store)
    inventree = FakeInvenTree()
    approved(store, other)
    assert stepper(store, inventree)(RequestKey(other, 1, 1)).state is S.VERIFIED
    clock = Clock()
    running = worker(store, None, clock=clock)
    archived = SlackApiError("synthetic", {"ok": False, "error": "is_archived"})  # type: ignore[no-untyped-call]
    slack(running).fail = [archived]
    assert running.post_receipts() == 1  # POST's failed, the other one's is posted
    assert store.plan(other, 1).receipt_ts == "1700000400.000001"
    assert store.plan(POST, 1).receipt_ts is None
    with caplog.at_level(logging.WARNING, logger="stockthread.worker"):
        for _ in range(RECEIPT_TRIES):
            slack(running).fail = [archived]
            clock.now += 2**RECEIPT_TRIES
            assert running.post_receipts() == 0
    levels = [record.levelno for record in caplog.records]
    assert levels == [logging.WARNING] * RECEIPT_TRIES + [logging.ERROR]  # never given up, an operator told
    assert "is_archived" in caplog.records[-1].getMessage()
    clock.now += STUCK_RETRY_SECONDS
    assert running.post_receipts() == 1
    assert store.plan(POST, 1).receipt_ts == "1700000400.000002"


def test_a_catalog_outage_pauses_receipts_and_never_breaks_the_step(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    written(store)
    clock = Clock()
    running = worker(store, None, clock=clock)
    up = [False]

    def catalog() -> Catalog:
        if not up[0]:
            raise InvenTreeError("synthetic: no catalog has been read yet")
        return Catalog(parts=(), locations=())

    running.catalog = catalog
    assert running.post_receipts() == 0
    assert logging.ERROR not in [record.levelno for record in caplog.records]  # an outage, not an operator's error
    up[0] = True
    clock.now += WORK_RETRY_SECONDS - 1
    assert running.post_receipts() == 0
    clock.now += 1
    assert running.post_receipts() == 1


def test_no_receipt_is_built_or_posted_while_a_dry_run_reply_is_on_its_way(store: Store) -> None:
    written(store)
    running = worker(store, None)
    running._running[POST] = None  # type: ignore[assignment]

    def catalog() -> Catalog:
        raise AssertionError("built while a reply is on its way")

    running.catalog = catalog
    assert running.post_receipts() == 0
    running._running.clear()
    running.catalog = lambda: Catalog(parts=(), locations=())
    assert running.post_receipts() == 1


def test_a_busy_poster_holds_no_receipt_back(store: Store) -> None:
    written(store)
    running = worker(store, None)
    slack(running).fail = [PosterBusy()]
    assert running.post_receipts() == 0
    assert running.post_receipts() == 1  # at once: nothing was tried, so nothing failed


def test_a_replaced_receipt_that_cannot_be_edited_breaks_nothing(store: Store) -> None:
    inventree = written(store)
    clock = Clock()
    running = worker(store, None, clock=clock)
    slack(running).hang = True
    running.post_receipts()
    assert undoer(store, inventree)(KEY, TAKE, SAM).state is S.UNDONE
    slack(running).hang = False

    def update(**kwargs: Any) -> Any:
        raise ConnectionError("synthetic")

    slack(running).chat_update = update  # type: ignore[method-assign]
    clock.now += 5
    assert running.post_receipts() == 0
    assert running.post_receipts() == 1
    assert store.plan(POST, 1).receipt_ts == "1700000400.000002"


def test_a_receipt_slack_gave_no_ts_for_is_not_posted_again(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    written(store)
    clock = Clock()
    running = worker(store, None, clock=clock)
    slack(running).ts = ""
    with caplog.at_level(logging.ERROR, logger="stockthread.worker"):
        assert running.post_receipts() == 0
    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    clock.now += 10 * STUCK_RETRY_SECONDS
    assert running.post_receipts() == 0
    assert len(slack(running).posted) == 1


def test_an_undo_gets_a_new_receipt(store: Store) -> None:
    inventree = written(store)
    running = worker(store, None, undoer(store, inventree))
    assert running.post_receipts() == 1
    assert undoer(store, inventree)(KEY, TAKE, SAM).state is S.UNDONE
    assert running.post_receipts() == 1
    assert [posted["text"] for posted in slack(running).posted] == ["Applied: 1 movement.", "Undone: 1 movement."]
    assert store.plan(POST, 1).receipt_ts == "1700000400.000002"


def test_a_plan_whose_record_cannot_be_rebuilt_gets_no_receipt_and_an_error(
    store: Store, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    written(store)

    def broken(store: Store, plan: Any) -> Any:
        raise NotRebuilt("no dry-run record of the post is the one the plan was made from")

    monkeypatch.setattr(worker_module, "plan_views", broken)
    running = worker(store, None)
    with caplog.at_level(logging.ERROR, logger="stockthread.worker"):
        assert running.post_receipts() == 0
    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    assert slack(running).posted == []
    assert running.post_receipts() == 0
    assert len(caplog.records) == 1  # not logged again every step


# --- the run loop's steps (`run.inventree_steps`) ---

UNTESTED = ServerInfo("9.9.9", 10**6)


class Connected(FakeInvenTree):
    """The fake as `inventree_steps` opens it: its own version, and a send that can take `delay` seconds."""

    def __init__(self) -> None:
        super().__init__()
        self.version = ServerInfo("1.5.6", DEPLETING_MARKER_FOUND_FROM)
        self.delay = 0.0
        self.opened = 0

    async def server_info(self) -> ServerInfo:
        self._read()
        return self.version

    async def send(self, request: Any) -> Any:
        await anyio.sleep(self.delay)
        self.lock = anyio.Lock()  # the step's own lock is not the fake's to see
        async with self.lock:
            return await super().send(request)


def steps(store: Store, fake: Connected, catalog: Any = None, limit: float | None = None) -> tuple[Stepper, Undoer]:
    @asynccontextmanager
    async def connect() -> AsyncIterator[tuple[Any, Any]]:
        fake.opened += 1
        yield fake, fake

    beats.clear()
    vehicles = catalog or (lambda: Catalog(parts=(), locations=(), vehicles={SAM: DESTINATION}))
    return inventree_steps(
        settings(writes=True), vehicles, store, OWNER, beat=lambda: beats.append(1), connect=connect, limit=limit
    )


beats: list[int] = []


def test_the_run_loops_step_writes_once_and_reads_back_with_a_client_of_its_own(store: Store) -> None:
    approved(store)
    fake = Connected()
    step, _ = steps(store, fake)
    assert step(KEY).state is S.VERIFIED
    assert len(fake.sent) == 1
    assert fake.opened == 1
    assert beats == [1]  # after the catalog read, before the step


def test_a_server_no_longer_tested_blocks_the_run_loops_apply_and_undo(store: Store) -> None:
    assert not check_version(UNTESTED).writes_allowed
    approved(store)
    fake = Connected()
    fake.version = UNTESTED
    step, undo = steps(store, fake)
    applied = step(KEY)
    assert applied.state is S.APPROVED
    assert "not tested" in applied.why
    assert fake.sent == []
    fake.version = ServerInfo("1.5.6", DEPLETING_MARKER_FOUND_FROM)
    assert step(KEY).state is S.VERIFIED
    fake.version = UNTESTED
    refused = undo(KEY, TAKE, SAM)
    assert refused.state is S.VERIFIED
    assert refused.why.startswith("no undo: ")
    assert len(fake.sent) == 1


def test_a_catalog_outage_stops_only_the_write_and_never_the_recovery(store: Store) -> None:
    approved(store)
    fake = Connected()

    def down() -> Catalog:
        raise InvenTreeError("synthetic catalog outage")

    step, _ = steps(store, fake, catalog=down)
    with pytest.raises(InvenTreeError):
        step(KEY)
    assert store.request(KEY).state is S.APPROVED
    assert fake.sent == []
    fake.answer, fake.lands = Outcome.UNKNOWN, True
    assert steps(store, fake)[0](KEY).state is S.UNKNOWN
    fake.answer, fake.lands = Outcome.ACCEPTED, False
    step, _ = steps(store, fake, catalog=down)
    recovered = step(KEY)  # recovery runs without the catalog: here it waits for the write to settle
    assert recovered.state is S.UNKNOWN
    assert recovered.why.startswith("still settling")


def test_a_step_with_too_little_time_left_begins_no_write(store: Store) -> None:
    approved(store)
    fake = Connected()
    step, _ = steps(store, fake, limit=1.5)  # less than SEND_MARGIN_TIMEOUTS timeouts of 1 s: no time to send
    applied = step(KEY)
    assert applied.state is S.APPROVED
    assert "too long" in applied.why
    assert fake.sent == []
    assert steps(store, fake)[0](KEY).state is S.VERIFIED
    _, undo = steps(store, fake, limit=1.5)
    refused = undo(KEY, TAKE, SAM)
    assert refused.state is S.VERIFIED
    assert refused.why == "no undo: InvenTree answered too slowly, ask again in a minute"
    assert len(fake.sent) == 1


def test_a_write_the_time_limit_cuts_off_is_unknown_and_never_sent_again(store: Store) -> None:
    approved(store)
    fake = Connected()
    fake.delay = 5
    step, _ = steps(store, fake, limit=2.2)  # 0.2 s to begin the write, which then hangs
    with pytest.raises(InvenTreeError, match="took longer"):
        step(KEY)
    assert store.request(KEY).state is S.UNKNOWN
    assert len(fake.sent) == 0  # cut off inside the send, before the fake recorded it
