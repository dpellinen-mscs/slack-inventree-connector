"""The worker's apply step and receipts: `Worker.work_due` runs the step of the apply protocol each request is due for
(through `applier.work_request` against the fake InvenTree of test_apply_request.py), and `Worker.post_receipts` posts
the receipt each plan is due in its thread and records it."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
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
from stockthread.inventree import DEPLETING_MARKER_FOUND_FROM, ServerInfo, VersionCheck
from stockthread.plan_record import NotRebuilt, plan_of
from stockthread.slack.replies import UNDO_ACTION, MovementView, receipt_replaced
from stockthread.store import DryRunOutcome, LockLost, NewPlan, Store
from stockthread.worker import (
    STUCK_RETRY_SECONDS,
    WORK_RETRY_SECONDS,
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


def settings(max_age_hours: int = 168) -> Settings:
    data = {
        "slack": {"workspace_id": WORKSPACE, "app_id": APP, "channels": [CHANNEL], "approvers": [LUIS]},
        "inventree": {"url": "http://inventree.local:8000"},
        "undo": {"max_age_hours": max_age_hours},
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


@pytest.mark.parametrize("error", [NotRebuilt("no dry-run record"), ValueError("not what it should be")])
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
    clock.now += STUCK_RETRY_SECONDS - 1
    assert running.work_due() == 0
    assert calls == []  # not tried again before then: no error every second
    clock.now += 1
    running.work_due()
    assert calls == [KEY]


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


def test_a_receipt_slack_gave_no_ts_for_is_not_posted_again(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    written(store)
    clock = Clock()
    running = worker(store, None, clock=clock)
    slack(running).ts = ""
    with caplog.at_level(logging.ERROR, logger="stockthread.worker"):
        assert running.post_receipts() == 0
    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    clock.now += STUCK_RETRY_SECONDS - 1
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
