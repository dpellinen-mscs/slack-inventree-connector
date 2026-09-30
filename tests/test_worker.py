"""The worker: stored events become posts, and a post whose window has passed gets one dry-run reply."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from stockthread.config import Settings, parse_settings
from stockthread.core import extract_rules
from stockthread.core.matching import Catalog, LocationRecord, PartRecord
from stockthread.core.model import CandidateMovement, Extraction, LineItem, MessageInput, MovementType, PostKey
from stockthread.llm.extract import ProviderError
from stockthread.slack.ingest import Drop, Revised
from stockthread.slack.listener import Listener
from stockthread.store import DryRunOutcome, LockLost, StateConflict, Store
from stockthread.store import db as store_db
from stockthread.worker import (
    DRAIN_BATCH,
    GRACE_SECONDS,
    MAX_BACKOFF_SECONDS,
    MAX_POSTS_PER_STEP,
    RETRY_FOR_SECONDS,
    ROOT_WAIT_SECONDS,
    Worker,
)

OWNER = "instance-a"
WORKSPACE, APP, CHANNEL = "T0ACME001", "A0ACME001", "C0ACME001"
SAM, LUIS = "U0SAM0001", "U0LUIS0005"
CATALOG = Catalog(
    parts=(PartRecord(1, "DISC-40", "Disc 40"), PartRecord(2, "ONT-100", "ONT 100")),
    locations=(LocationRecord(10, "Main stock"), LocationRecord(11, "Vehicles/Truck 1")),
)
ENV = {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic", "INVENTREE_TOKEN": "inv-synthetic"}


def settings(channels: tuple[str, ...] = (CHANNEL,)) -> Settings:
    data = {
        "slack": {"workspace_id": WORKSPACE, "app_id": APP, "channels": list(channels)},
        "inventree": {"url": "http://inventree.local:8000"},
    }
    return parse_settings(data, ENV)


class FakeSlack:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.failures = 0

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        if self.failures:
            self.failures -= 1
            raise ConnectionError("synthetic outage")
        self.posted.append(kwargs)
        return {"ok": True, "ts": f"1790009999.{len(self.posted):06d}"}


class Clock:
    def __init__(self, ts: str) -> None:
        self.now = datetime.fromtimestamp(float(Decimal(ts)), UTC)

    def __call__(self) -> datetime:
        return self.now

    def at(self, ts: str) -> None:
        self.now = datetime.fromtimestamp(float(Decimal(ts)), UTC)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "stockthread.db"


@pytest.fixture
def store(path: Path) -> Iterator[Store]:
    opened = Store.open(path)
    opened.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    yield opened
    opened.close()


@pytest.fixture
def slack() -> FakeSlack:
    return FakeSlack()


@pytest.fixture
def clock() -> Clock:
    return Clock("1790000000.000000")


@pytest.fixture
def worker(store: Store, slack: FakeSlack, clock: Clock) -> Worker:
    return Worker(settings(), store, OWNER, slack, lambda: CATALOG, clock)


def message(
    ts: str,
    text: str,
    *,
    user: str = SAM,
    channel: str = CHANNEL,
    thread_ts: str | None = None,
    subtype: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {"type": "message", "channel": channel, "user": user, "text": text, "ts": ts}
    if thread_ts:
        event["thread_ts"] = thread_ts
    if subtype:
        event["subtype"] = subtype
    return {"type": "event_callback", "team_id": WORKSPACE, "api_app_id": APP, "event_id": f"Ev{ts}", "event": event}


def enqueue(store: Store, payload: dict[str, Any], kind: str = "event") -> None:
    assert store.enqueue_event(f"{payload.get('event_id', 'click')}:{kind}", kind, json.dumps(payload))  # type: ignore[arg-type]


def audit(path: Path) -> list[str]:
    with sqlite3.connect(path) as raw:
        return [row[0] for row in raw.execute("SELECT event FROM audit_log WHERE event LIKE 'dry_run_%' ORDER BY id")]


def statuses(path: Path) -> list[tuple[str, str | None]]:
    with sqlite3.connect(path) as raw:
        return [(row[0], row[1]) for row in raw.execute("SELECT status, outcome FROM slack_events ORDER BY id")]


def replies(path: Path) -> list[tuple[str, str | None, bool]]:
    with sqlite3.connect(path) as raw:
        return [
            (row[0], row[1], row[2] is not None)
            for row in raw.execute("SELECT ts, reply_ts, dry_run_at FROM posts ORDER BY id")
        ]


def test_a_clear_post_gets_one_dry_run_reply_in_its_thread(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message("1790000000.000100", "took 2 Disc 40 from main stock to truck 1"))
    clock.at("1790000001.000000")
    assert worker.step() == 1  # stored and grouped; a sibling may still come
    assert slack.posted == []
    assert statuses(path) == [("done", None)]

    assert GRACE_SECONDS == 10
    clock.at("1790000015.000100")
    worker.step()
    assert slack.posted == []  # the window is 5 seconds plus 10 of grace, and not yet over

    clock.at("1790000015.000101")
    worker.step()
    assert len(slack.posted) == 1
    reply = slack.posted[0]
    assert reply["channel"] == CHANNEL
    assert reply["thread_ts"] == "1790000000.000100"
    assert reply["unfurl_links"] is False
    assert reply["unfurl_media"] is False
    shown = json.dumps(reply["blocks"])
    assert "Dry run" in shown
    assert "*Disc 40*" in shown
    assert "*Vehicles/Truck 1*" in shown
    assert replies(path) == [("1790000000.000100", "1790009999.000001", True)]

    clock.at("1790000100.000000")
    assert worker.step() == 0
    assert len(slack.posted) == 1  # once only


@pytest.mark.parametrize(
    "text",
    [
        "anyone want lunch?",
        "2 guys on truck 1 today",  # declined: chatter in the shape of a movement
        "can someone bring 2 Disc 40 to truck 1?",  # a request, not a movement
    ],
)
def test_a_post_that_is_not_read_as_a_movement_gets_no_reply(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path, text: str
) -> None:
    enqueue(store, message("1790000000.000100", text))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    assert slack.posted == []
    assert replies(path) == [("1790000000.000100", None, True)]


def test_siblings_within_the_window_are_one_post_and_wait_for_the_last(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message("1790000000.000100", "took 2 Disc 40 from main stock"))
    enqueue(store, message("1790000003.000100", "to truck 1"))
    worker.step()
    clock.at("1790000016.000000")  # past the first message's window, inside the second's
    worker.step()
    assert replies(path) == [("1790000000.000100", None, False)]
    clock.at("1790000019.000000")
    worker.step()
    # One post of two messages. The rule-based extractor declines several messages, so no reply yet.
    assert replies(path) == [("1790000000.000100", None, True)]
    assert slack.posted == []


def test_a_sibling_after_the_reply_starts_its_own_post(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message("1790000000.000100", "took 2 Disc 40 from main stock to truck 1"))
    worker.step()
    clock.at("1790000015.500000")
    worker.step()
    assert len(slack.posted) == 1
    enqueue(store, message("1790000004.000100", "took 1 ONT 100 from main stock to truck 1"))  # very late
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    assert [row[0] for row in replies(path)] == ["1790000000.000100", "1790000004.000100"]
    assert [reply["thread_ts"] for reply in slack.posted] == ["1790000000.000100", "1790000004.000100"]


def test_a_thread_reply_after_the_dry_run_gets_no_second_reply(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message("1790000000.000100", "took 2 Disc 40 from main stock to truck 1"))
    worker.step()
    clock.at("1790000016.000000")
    worker.step()
    enqueue(
        store, message("1790000020.000100", "took 1 ONT 100 from main stock to truck 1", thread_ts="1790000000.000100")
    )
    worker.step()
    clock.at("1790000050.000000")
    worker.step()
    assert len(slack.posted) == 1  # a revision is M2
    assert statuses(path) == [("done", None), ("done", None)]


def test_edits_deletions_and_clicks_are_finished_without_a_reply(
    worker: Worker, store: Store, slack: FakeSlack, path: Path
) -> None:
    edit = message("1790000000.000200", "", subtype="message_changed")
    edit["event"]["message"] = {"type": "message", "user": SAM, "text": "took 3 Disc 40", "ts": "1790000000.000100"}
    edit["event"]["previous_message"] = {"type": "message", "user": SAM, "text": "took 2", "ts": "1790000000.000100"}
    edit["event"]["message"]["edited"] = {"user": SAM, "ts": "1790000000.000200"}
    enqueue(store, edit)
    deletion = message("1790000000.000300", "", subtype="message_deleted")
    deletion["event"]["deleted_ts"] = "1790000000.000100"
    enqueue(store, deletion)
    enqueue(store, {"type": "block_actions"}, kind="interactive")
    worker.step()
    assert statuses(path) == [
        ("dropped", "revisions_not_handled_yet"),
        ("dropped", "revisions_not_handled_yet"),
        ("dropped", "clicks_not_handled_yet"),
    ]
    assert slack.posted == []
    assert replies(path) == []


def test_the_ingest_checks_run_again_with_the_current_settings(
    store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message("1790000000.000100", "took 2 Disc 40 from main stock to truck 1"))
    worker = Worker(settings(channels=("C0OTHER01",)), store, OWNER, slack, lambda: CATALOG, clock)
    worker.step()
    assert statuses(path) == [("dropped", "channel_not_allowed")]
    assert replies(path) == []


def test_a_post_with_a_message_that_cannot_be_read_again_gets_no_reply(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message("1790000000.000100", "took 2 Disc 40 from main stock to truck 1"))
    worker.step()
    with sqlite3.connect(path) as raw:
        raw.execute("UPDATE slack_events SET body = NULL")  # retention, on a finished event
    clock.at("1790000030.000000")
    worker.step()
    assert slack.posted == []
    assert replies(path) == [("1790000000.000100", None, True)]


def test_a_failed_reply_is_retried_with_backoff_then_given_up(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message("1790000000.000100", "took 2 Disc 40 from main stock to truck 1"))
    worker.step()
    clock.at("1790000030.000000")
    slack.failures = 1
    worker.step()
    assert replies(path) == [("1790000000.000100", None, False)]  # still waiting
    worker.step()
    assert slack.posted == []  # backing off: 2 seconds
    clock.at("1790000032.000000")
    worker.step()
    assert len(slack.posted) == 1
    assert worker._failures == {}  # a reply that got through leaves no retry state behind

    enqueue(store, message("1790000100.000100", "took 1 ONT 100 from main stock to truck 1"))
    clock.at("1790000130.000000")
    slack.failures = 10_000
    for second in range(0, RETRY_FOR_SECONDS - 1, 10):  # a 15-minute outage, stepping every 10 seconds
        clock.at(f"{1790000130 + second}.000000")
        worker.step()
    assert replies(path)[1] == ("1790000100.000100", None, False)  # a short outage never loses the reply
    clock.at(f"{1790000130 + RETRY_FOR_SECONDS + MAX_BACKOFF_SECONDS}.000000")  # the next try after 15 minutes gives up
    worker.step()
    assert replies(path)[1] == ("1790000100.000100", None, True)
    assert audit(path)[-1] == "dry_run_gave_up"
    assert len(slack.posted) == 1


def test_one_post_that_breaks_the_pipeline_does_not_stop_the_next(
    worker: Worker,
    store: Store,
    slack: FakeSlack,
    clock: Clock,
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    real = extract_rules.extract
    calls: list[int] = []

    def extract(messages: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise KeyError("took 2 Disc 40")  # a message that quotes the post must never reach the log
        return real(messages)

    monkeypatch.setattr(extract_rules, "extract", extract)
    enqueue(store, message("1790000000.000100", "took 2 Disc 40 from main stock to truck 1"))
    enqueue(store, message("1790000000.000200", "took 1 ONT 100 from main stock to truck 1", user=LUIS))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    assert replies(path) == [("1790000000.000100", None, True), ("1790000000.000200", "1790009999.000001", True)]
    assert audit(path) == ["dry_run_failed", "dry_run_replied"]
    assert "Disc" not in caplog.text
    assert "KeyError" in caplog.text


def test_a_catalog_outage_makes_posts_wait_and_logs_no_text(
    store: Store, slack: FakeSlack, clock: Clock, path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    up = False

    def catalog() -> Catalog:
        if not up:
            raise ConnectionError("synthetic InvenTree outage")
        return CATALOG

    worker = Worker(settings(), store, OWNER, slack, catalog, clock)
    enqueue(store, message("1790000000.000100", "took 2 Disc 40 from main stock to truck 1"))
    worker.step()
    clock.at("1790000030.000000")
    with pytest.raises(ConnectionError):
        worker.step()
    assert replies(path) == [("1790000000.000100", None, False)]
    up = True
    worker.step()
    assert len(slack.posted) == 1
    assert "Disc" not in caplog.text


def test_a_lost_lock_stops_the_worker(worker: Worker, store: Store, clock: Clock, path: Path) -> None:
    enqueue(store, message("1790000000.000100", "took 2 Disc 40 from main stock to truck 1"))
    worker.step()
    with sqlite3.connect(path) as raw:
        raw.execute("UPDATE instance_lock SET owner = 'instance-b'")
    clock.at("1790000030.000000")
    with pytest.raises(LockLost):
        worker.step()


def test_a_dry_run_outcome_is_recorded_once(store: Store, path: Path) -> None:
    key = PostKey(WORKSPACE, CHANNEL, "1790000000.000100")
    store.place_message(OWNER, key, user_id=SAM, thread_ts=None, window_seconds=5)
    with pytest.raises(ValueError, match="only a reply"):
        store.finish_dry_run(OWNER, key, DryRunOutcome.REPLIED, "soon")
    with pytest.raises(ValueError, match="only a reply"):
        store.finish_dry_run(OWNER, key, DryRunOutcome.SILENT, "1790000001.000100")
    store.finish_dry_run(OWNER, key, DryRunOutcome.REPLIED, "1790000001.000100")
    with pytest.raises(StateConflict):
        store.finish_dry_run(OWNER, key, DryRunOutcome.SILENT)
    with pytest.raises(sqlite3.IntegrityError), sqlite3.connect(path) as raw:
        raw.execute("UPDATE posts SET reply_ts = '1790000002.000100'")


def test_posts_from_before_the_worker_count_as_handled(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = store_db._migrations()
    monkeypatch.setattr(store_db, "_migrations", lambda: real[:3])
    old = Store.open(path)
    post_id = old.create_post(PostKey(WORKSPACE, CHANNEL, "1.000000"))
    old.close()
    with sqlite3.connect(path) as raw:  # this code's place_message needs the new column
        raw.execute(
            "INSERT INTO post_messages (post_id, workspace_id, channel_id, ts, user_id, added_at)"
            " VALUES (?, ?, ?, '1.000000', ?, 'then')",
            (post_id, WORKSPACE, CHANNEL, SAM),
        )
    monkeypatch.setattr(store_db, "_migrations", lambda: real)
    upgraded = Store.open(path)
    assert upgraded.posts_waiting_for_dry_run() == []
    assert upgraded.post_bodies(PostKey(WORKSPACE, CHANNEL, "1.000000")) == [("1.000000", None)]
    upgraded.close()


def test_a_thread_root_delivered_after_a_reply_still_gets_its_dry_run(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    root = "1790000000.000100"
    enqueue(store, message("1790000003.000100", "thanks", user=LUIS, thread_ts=root))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    assert replies(path) == [(root, None, False)]  # waiting for the root, which Slack retries
    enqueue(store, message(root, "took 2 Disc 40 from main stock to truck 1"))  # Slack's retry, a minute later
    clock.at("1790000070.000000")
    worker.step()
    # One post of two messages: the rule-based extractor declines it, but it was read whole, root first.
    assert replies(path) == [(root, None, True)]
    assert audit(path) == ["dry_run_silent"]


def test_a_thread_root_that_never_comes_is_left_out_after_a_wait(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    root = "1790000000.000100"
    enqueue(store, message("1790000003.000100", "took 2 Disc 40 from main stock to truck 1", thread_ts=root))
    worker.step()
    clock.at(f"{1790000000 + ROOT_WAIT_SECONDS}.000100")
    worker.step()
    assert slack.posted == []
    clock.at(f"{1790000000 + ROOT_WAIT_SECONDS}.000101")
    worker.step()
    assert [reply["thread_ts"] for reply in slack.posted] == [root]


def test_a_backlog_is_drained_before_any_post_is_due(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(DRAIN_BATCH - 1):
        enqueue(store, message(f"1789999000.{n:06d}", "anyone want lunch?", user=f"U0OTHER{n:03d}"))
    enqueue(store, message("1790000001.000000", "took 2 Disc 40 from main stock"))
    enqueue(store, message("1790000002.000000", "to truck 1"))
    clock.at("1790000100.000000")
    monkeypatch.setattr("stockthread.worker.MAX_DRAIN_BATCHES", 1)
    worker.step()  # one batch, events still pending: no post is due yet
    assert audit(path) == []
    worker.step()
    posts = [row[0] for row in replies(path) if row[0].startswith("1790000")]
    assert posts == ["1790000001.000000"]  # the siblings stayed one post


def test_an_event_that_raises_fails_alone(
    worker: Worker, store: Store, clock: Clock, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = store.place_message
    calls: list[int] = []

    def place(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("synthetic")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "place_message", place)
    enqueue(store, message("1790000000.000100", "took 2 Disc 40 from main stock to truck 1"))
    enqueue(store, message("1790000000.000200", "took 1 ONT 100 from main stock to truck 1", user=LUIS))
    worker.step()
    assert statuses(path) == [("failed", "error_operational_error"), ("done", None)]


def test_an_old_post_is_withheld_and_a_late_one_says_so(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message("1780000000.000100", "took 2 Disc 40 from main stock to truck 1"))  # months ago
    enqueue(store, message("1790000000.000100", "took 1 ONT 100 from main stock to truck 1"))
    clock.at("1790001000.000000")  # about 17 minutes after the second
    worker.step()
    assert audit(path) == ["dry_run_withheld", "dry_run_replied"]
    assert [reply["thread_ts"] for reply in slack.posted] == ["1790000000.000100"]
    assert "arrived while the connector was down" in json.dumps(slack.posted[0]["blocks"])


def test_the_clock_must_be_aware(store: Store, slack: FakeSlack) -> None:
    worker = Worker(settings(), store, OWNER, slack, lambda: CATALOG, lambda: datetime(2026, 9, 29))
    with pytest.raises(ValueError, match="aware"):
        worker.reply_due()


def test_an_edit_before_the_dry_run_withholds_the_post(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message("1790000000.000100", "took 2 Disc 40 from main stock to truck 1"))
    edit = message("1790000002.000100", "", subtype="message_changed")
    edited = {"type": "message", "user": SAM, "text": "took 3 Disc 40 from main stock to truck 1"}
    edit["event"]["message"] = {**edited, "ts": "1790000000.000100", "edited": {"user": SAM, "ts": "1790000002.000100"}}
    edit["event"]["previous_message"] = {
        **edited,
        "text": "took 2 Disc 40 from main stock to truck 1",
        "ts": "1790000000.000100",
    }
    enqueue(store, edit)
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    assert statuses(path) == [("done", None), ("done", "withheld_changed_post")]
    assert audit(path) == ["dry_run_withheld"]
    assert slack.posted == []


def test_a_step_answers_a_bounded_number_of_posts_and_stops_when_asked(
    store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    stopping = [False]
    worker = Worker(settings(), store, OWNER, slack, lambda: CATALOG, clock, stopping=lambda: stopping[0])
    for n in range(MAX_POSTS_PER_STEP + 2):
        enqueue(store, message(f"1790000000.{n:06d}", "took 2 Disc 40 from main stock to truck 1", user=f"U0T{n:04d}"))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    assert len(slack.posted) == MAX_POSTS_PER_STEP
    stopping[0] = True
    worker.step()
    assert len(slack.posted) == MAX_POSTS_PER_STEP
    stopping[0] = False
    worker.step()
    assert len(slack.posted) == MAX_POSTS_PER_STEP + 2


TAKE = "took 2 Disc 40 from main stock to truck 1"
ROOT = "1790000000.000100"


def edit_of(ts: str, at: str, before: str, after: str) -> dict[str, Any]:
    edit = message(at, "", subtype="message_changed")
    base = {"type": "message", "user": SAM, "ts": ts}
    edit["event"]["message"] = {**base, "text": after, "edited": {"user": SAM, "ts": at}}
    edit["event"]["previous_message"] = {**base, "text": before}
    return edit


def deletion_of(ts: str, at: str) -> dict[str, Any]:
    deletion = message(at, "", subtype="message_deleted")
    deletion["event"]["deleted_ts"] = ts
    return deletion


@pytest.mark.parametrize("kind", ["edit", "deletion"])
def test_an_edit_or_deletion_that_arrives_before_its_message_still_withholds_the_post(
    kind: str, worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    after = "took 3 Disc 40 from main stock to truck 1"
    first = (
        edit_of(ROOT, "1790000002.000100", TAKE, after) if kind == "edit" else deletion_of(ROOT, "1790000002.000100")
    )
    enqueue(store, first)
    worker.step()
    assert statuses(path) == [("dropped", "revisions_not_handled_yet")]  # noted: no post holds the message yet

    enqueue(store, message(ROOT, TAKE))  # Slack delivered the original late
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    assert slack.posted == []  # never a dry run for 2 when the post now says 3, or is gone
    assert audit(path) == ["dry_run_withheld"]
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert record is not None
    assert (record.outcome, record.result) == (DryRunOutcome.WITHHELD, {"withheld": "changed"})


def test_an_early_edit_of_a_sibling_withholds_the_whole_post(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, edit_of("1790000001.000100", "1790000002.000100", "and 1 ONT 100", "and 4 ONT 100"))
    enqueue(store, message(ROOT, TAKE))
    enqueue(store, message("1790000001.000100", "and 1 ONT 100"))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    assert slack.posted == []
    assert audit(path) == ["dry_run_withheld"]


def test_a_revision_is_noted_once_and_only_by_the_lock_holder(store: Store, path: Path) -> None:
    key = PostKey(WORKSPACE, CHANNEL, ROOT)
    store.note_revision(OWNER, key)
    store.note_revision(OWNER, key)
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT count(*) FROM revised_messages").fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError, match="never"):
            raw.execute("DELETE FROM revised_messages")
    with pytest.raises(LockLost):
        store.note_revision("someone-else", key)


def test_a_reply_keeps_what_was_decided_and_why(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message(ROOT, "took 2 Disc 40 and 1 widget from main stock to truck 1"))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    assert len(slack.posted) == 1
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert record is not None
    assert record.outcome is DryRunOutcome.REPLIED
    assert record.extractor == extract_rules.EXTRACTOR
    assert record.result is not None
    (movement,) = record.result["movements"]
    assert movement["type"] == "take_to_vehicle"
    assert movement["source"] == {"status": "matched", "rule": "normalized", "record": 10, "candidates": []}
    assert movement["destination"]["record"] == 11
    disc, widget = movement["items"]
    assert (disc["item_text"], disc["quantity"], disc["unit"]) == ("Disc 40", "2", None)
    assert (disc["part"]["status"], disc["part"]["record"]) == ("matched", 1)
    assert disc["part"]["rule"] in ("exact", "normalized")
    assert widget["part"] == {"status": "unmatched", "rule": None, "record": None, "candidates": []}
    assert ["part_unmatched", "line 2"] in movement["gate"]
    assert record.result["late"] is False
    assert (record.result["poster"], record.result["messages"], record.result["max_quantity"]) == (SAM, [ROOT], 100)


def test_every_outcome_keeps_a_record_and_a_record_never_changes(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message(ROOT, "lunch anyone"))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert record is not None
    assert record.outcome is DryRunOutcome.SILENT
    assert record.extractor == extract_rules.EXTRACTOR
    assert record.result is not None
    assert record.result["movements"] == []
    with sqlite3.connect(path) as raw:
        for change in (
            "UPDATE dry_runs SET result = '{}'",
            "UPDATE dry_runs SET outcome = 'replied'",
            "UPDATE dry_runs SET rowid = 2",  # the rowid is the post
            "DELETE FROM dry_runs",
            "INSERT OR REPLACE INTO dry_runs (post_id, outcome, created_at) VALUES (1, 'replied', 'now')",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(change)
        raw.execute("UPDATE dry_runs SET result = NULL")  # retention may expire the quoted words
    expired = store.dry_run(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert expired is not None
    assert expired.result is None


def test_a_reply_that_is_given_up_on_keeps_its_record(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message(ROOT, TAKE))
    worker.step()
    slack.failures = 10_000
    clock.at("1790000030.000000")
    worker.step()
    clock.at(f"{1790000030 + RETRY_FOR_SECONDS}.000000")
    worker.step()
    assert audit(path) == ["dry_run_gave_up"]
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert record is not None
    assert record.outcome is DryRunOutcome.GAVE_UP
    assert record.result is not None
    assert record.result["movements"][0]["items"][0]["quantity"] == "2"


def test_a_post_the_pipeline_breaks_on_records_the_error_class(
    worker: Worker, store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_: object) -> None:
        raise RuntimeError("synthetic: quoted text must not be stored")

    monkeypatch.setattr(extract_rules, "extract", broken)
    enqueue(store, message(ROOT, TAKE))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert record is not None
    assert (record.outcome, record.result) == (DryRunOutcome.FAILED, {"error": "error_runtime_error"})


class RateLimited(Exception):
    """Shaped like slack_sdk's SlackApiError: a response with headers."""

    def __init__(self, headers: dict[str, Any]) -> None:
        super().__init__("ratelimited")
        self.response = type("Response", (), {"headers": headers})()


@pytest.mark.parametrize(
    ("headers", "waits"),
    [({"Retry-After": "120"}, 120), ({"retry-after": 45}, 45), ({"Retry-After": "soon"}, 2), ({}, 2)],
)
def test_a_rate_limited_reply_waits_between_steps_for_as_long_as_slack_asks(
    headers: dict[str, Any], waits: int, worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    calls: list[int] = []
    post = slack.chat_postMessage

    def limited(**kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 1:
            raise RateLimited(headers)  # returns at once: nothing sleeps inside the call
        return post(**kwargs)

    slack.chat_postMessage = limited  # type: ignore[method-assign]
    enqueue(store, message(ROOT, TAKE))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    clock.at(f"{1790000030 + waits - 1}.000000")
    worker.step()
    assert len(calls) == 1  # not before the time Slack named
    clock.at(f"{1790000030 + waits}.000000")
    worker.step()
    assert len(slack.posted) == 1
    assert audit(path) == ["dry_run_replied"]


def test_an_instance_that_lost_the_lock_while_it_was_blocked_does_not_post(
    store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    def catalog() -> Catalog:
        with sqlite3.connect(path) as raw:  # the read took so long that another instance took the lock over
            raw.execute("UPDATE instance_lock SET owner = 'instance-b'")
        return CATALOG

    worker = Worker(settings(), store, OWNER, slack, catalog, clock)
    enqueue(store, message(ROOT, TAKE))
    worker.step()
    clock.at("1790000030.000000")
    worker.beat(now=True)  # a fresh beat: the throttled one would not look again for 10 seconds
    with pytest.raises(LockLost):
        worker.step()
    assert slack.posted == []  # the new holder posts the one reply
    assert replies(path) == [(ROOT, None, False)]


def tombstone(ts: str, at: str) -> dict[str, Any]:
    """What Slack sends when a thread root that has replies is deleted."""
    event = edit_of(ts, at, TAKE, "This message was deleted.")
    event["event"]["message"]["subtype"] = "tombstone"
    return event


def unstamped(ts: str, at: str) -> dict[str, Any]:
    event = edit_of(ts, at, TAKE, "took 5 Disc 40 from main stock to truck 1")
    del event["event"]["message"]["edited"]
    return event


def without_user(ts: str, at: str) -> dict[str, Any]:
    event = edit_of(ts, at, TAKE, "took 5 Disc 40 from main stock to truck 1")
    del event["event"]["message"]["user"]
    return event


@pytest.mark.parametrize("revision", [tombstone, unstamped, without_user])
@pytest.mark.parametrize("edit_first", [False, True])
def test_an_edit_the_ingest_checks_drop_still_withholds_the_post(
    revision: Any, edit_first: bool, worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    events = [message(ROOT, TAKE), revision(ROOT, "1790000002.000100")]
    for event in reversed(events) if edit_first else events:
        enqueue(store, event)
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    assert slack.posted == []
    assert audit(path) == ["dry_run_withheld"]


@pytest.mark.parametrize("revision", [tombstone, unstamped, without_user])
@pytest.mark.parametrize("edit_first", [False, True])
def test_an_edit_the_strict_checks_cannot_read_gets_from_the_listener_to_the_worker(
    revision: Any, edit_first: bool, worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    listener = Listener(worker.settings.slack, lambda: store)
    events = [message(ROOT, TAKE), revision(ROOT, "1790000002.000100")]
    for event in reversed(events) if edit_first else events:
        listener.on_event(event)
    assert isinstance(listener.on_event(events[1]), Drop)  # Slack's retry of it is stored once
    assert len(statuses(path)) == 2
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    assert slack.posted == []
    assert audit(path) == ["dry_run_withheld"]
    assert ("dropped", "revisions_not_handled_yet") in statuses(path) or ("done", "withheld_changed_post") in statuses(
        path
    )


def test_the_listener_does_not_keep_a_link_preview_or_an_edit_from_elsewhere(store: Store, path: Path) -> None:
    listener = Listener(settings().slack, lambda: store)
    preview = edit_of(ROOT, "1790000002.000100", TAKE, TAKE)
    del preview["event"]["message"]["edited"]
    other_app = {**tombstone(ROOT, "1790000003.000100"), "api_app_id": "A0OTHER01"}
    other_workspace = {**tombstone(ROOT, "1790000004.000100"), "team_id": "T0OTHER01"}
    other_channel = tombstone(ROOT, "1790000005.000100")
    other_channel["event"]["channel"] = "C0OTHER01"
    from_a_bot = tombstone(ROOT, "1790000006.000100")
    from_a_bot["event"]["previous_message"]["bot_id"] = "B0OTHER01"
    no_event_id = tombstone(ROOT, "1790000007.000100")
    del no_event_id["event_id"]
    for event in (preview, other_app, other_workspace, other_channel, from_a_bot, no_event_id):
        assert isinstance(listener.on_event(event), Drop)
    assert statuses(path) == []
    assert listener.on_event(tombstone(ROOT, "1790000008.000100")) == Revised("Ev1790000008.000100", CHANNEL, ROOT)


def test_a_link_preview_or_a_new_thread_reply_is_not_an_edit(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message(ROOT, TAKE))
    preview = edit_of(ROOT, "1790000002.000100", TAKE, TAKE)
    del preview["event"]["message"]["edited"]
    preview["event"]["message"]["attachments"] = [{"title": "synthetic preview"}]
    enqueue(store, preview)
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    assert len(slack.posted) == 1


def test_a_reply_under_a_root_that_was_deleted_and_never_arrived_gets_no_dry_run(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, deletion_of(ROOT, "1790000002.000100"))
    enqueue(store, message("1790000003.000100", "took 1 ONT 100 from main stock to truck 1", thread_ts=ROOT))
    worker.step()
    clock.at(f"{1790000000 + ROOT_WAIT_SECONDS + 60}.000000")
    worker.step()
    assert slack.posted == []
    assert audit(path) == ["dry_run_withheld"]


def test_the_heartbeat_is_fresh_when_a_reply_is_posted_and_a_rate_limit_quiets_every_post(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    def heartbeat() -> str:
        with sqlite3.connect(path) as raw:
            return str(raw.execute("SELECT heartbeat_at FROM instance_lock").fetchone()[0])

    seen: list[str] = []
    calls: list[int] = []
    post = slack.chat_postMessage

    def limited(**kwargs: Any) -> dict[str, Any]:
        seen.append(heartbeat())
        calls.append(1)
        if len(calls) == 1:
            raise RateLimited({"Retry-After": "120"})
        return post(**kwargs)

    slack.chat_postMessage = limited  # type: ignore[method-assign]
    enqueue(store, message(ROOT, TAKE))
    enqueue(store, message("1790000000.000200", "took 1 ONT 100 from main stock to truck 1", user=LUIS))
    worker.step()
    clock.at("1790000030.000000")
    worker.beat(now=True)  # the throttled beat would not come again for 10 seconds
    before = heartbeat()
    worker.step()
    assert seen[0] > before  # beaten right before the post, so the post has the whole stale window
    assert len(calls) == 1  # the second post did not even try: Slack asked everyone to wait
    clock.at("1790000149.000000")
    worker.step()
    assert len(calls) == 1
    clock.at("1790000150.000000")
    worker.step()
    assert len(slack.posted) == 2


def test_a_long_retry_after_near_the_end_still_gets_its_last_try_on_time(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    def limited(**kwargs: Any) -> dict[str, Any]:
        raise RateLimited({"Retry-After": "99999"})

    slack.chat_postMessage = limited  # type: ignore[method-assign]
    enqueue(store, message(ROOT, TAKE))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    clock.at(f"{1790000030 + RETRY_FOR_SECONDS}.000000")  # the wait is capped at the retry period
    worker.step()
    assert audit(path) == ["dry_run_gave_up"]


# The LLM extractor: only what the rules decline goes to the model, and an outage waits.

OTHER = "1790000001.000100"
LLM_KEY = PostKey(WORKSPACE, CHANNEL, ROOT)
GAVE = "gave truck 1 two Disc 40 from main stock"  # the rules do not know "gave": declined
MODEL = "llm-0.1/test-model"


def read(quantity: int = 2, item: str = "Disc 40") -> Extraction:
    movement = CandidateMovement(
        MovementType.TAKE_TO_VEHICLE, (LineItem(item, Decimal(quantity)),), "main stock", "truck 1"
    )
    return Extraction(MODEL, (movement,))


class FakeLLM:
    def __init__(self, *answers: Extraction | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[list[str]] = []

    def extract(self, messages: Sequence[MessageInput]) -> Extraction:
        self.calls.append([message.text for message in messages])
        answer = self.answers.pop(0) if self.answers else ProviderError("the provider answered HTTP 503")
        if isinstance(answer, Exception):
            raise answer
        return answer


def worker_with(llm: FakeLLM, store: Store, slack: FakeSlack, clock: Clock) -> Worker:
    return Worker(settings(), store, OWNER, slack, lambda: CATALOG, clock, llm=llm)


def due(worker: Worker, clock: Clock, at: int = 1790000030) -> None:
    worker.step()
    clock.at(f"{at}.000000")
    worker.step()


def test_a_post_the_rules_decline_is_read_by_the_model_and_gets_its_dry_run(
    store: Store, slack: FakeSlack, clock: Clock
) -> None:
    llm = FakeLLM(read())
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    due(worker, clock)
    assert llm.calls == [[GAVE]]
    assert [reply["thread_ts"] for reply in slack.posted] == [ROOT]
    record = store.dry_run(LLM_KEY)
    assert record is not None
    assert record.outcome is DryRunOutcome.REPLIED
    assert record.extractor == MODEL
    assert record.result is not None
    assert record.result["llm_unavailable"] is False
    assert record.result["movements"][0]["items"][0]["part"]["record"] == 1
    clock.at("1790000100.000000")
    worker.step()
    assert len(llm.calls) == 1
    assert len(slack.posted) == 1


@pytest.mark.parametrize(
    "text",
    [
        "took 2 Disc 40 from main stock to truck 1",  # the rules read it
        "can someone bring 2 Disc 40 to truck 1?",  # the rules say: a request, not a movement
        "anyone want lunch?",
    ],
)
def test_a_post_the_rules_can_read_never_goes_to_the_model(
    store: Store, slack: FakeSlack, clock: Clock, text: str
) -> None:
    llm = FakeLLM(read(quantity=50))
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, text))
    due(worker, clock)
    assert llm.calls == []
    record = store.dry_run(LLM_KEY)
    assert record is not None
    assert record.extractor == extract_rules.EXTRACTOR


def test_siblings_are_sent_as_one_post(store: Store, slack: FakeSlack, clock: Clock) -> None:
    llm = FakeLLM(read())
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, "took 2 Disc 40 from main stock"))
    enqueue(store, message("1790000003.000100", "to truck 1"))
    due(worker, clock)
    assert llm.calls == [["took 2 Disc 40 from main stock", "to truck 1"]]
    assert len(slack.posted) == 1


def test_what_the_model_reads_is_still_checked_against_the_text(store: Store, slack: FakeSlack, clock: Clock) -> None:
    llm = FakeLLM(read(quantity=5))  # the post says two
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    due(worker, clock)
    record = store.dry_run(LLM_KEY)
    assert record is not None
    assert record.result is not None
    movement = record.result["movements"][0]
    assert [reason["source"] for reason in movement["reasons"]] != []
    assert movement["gate"] != []  # it could not apply without a person


def test_a_post_the_model_declines_gets_no_reply(store: Store, slack: FakeSlack, clock: Clock, path: Path) -> None:
    llm = FakeLLM(Extraction(MODEL, (), declined=True))
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    due(worker, clock)
    assert slack.posted == []
    assert replies(path) == [(ROOT, None, True)]
    record = store.dry_run(LLM_KEY)
    assert record is not None
    assert (record.outcome, record.extractor) == (DryRunOutcome.SILENT, MODEL)
    clock.at("1790000100.000000")
    worker.step()
    assert len(llm.calls) == 1


def test_a_provider_outage_makes_the_post_wait_and_it_is_tried_again_with_backoff(
    store: Store, slack: FakeSlack, clock: Clock, path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    down = ProviderError("the provider answered HTTP 503")
    llm = FakeLLM(down, down, read())
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    due(worker, clock)
    assert len(llm.calls) == 1
    assert replies(path) == [(ROOT, None, False)]  # still waiting
    clock.at("1790000031.000000")
    worker.step()
    assert len(llm.calls) == 1  # 2 s after the first failure, not before
    clock.at("1790000032.000000")
    worker.step()
    assert len(llm.calls) == 2
    clock.at("1790000035.000000")
    worker.step()
    assert len(llm.calls) == 2  # 4 s after the second
    clock.at("1790000036.000000")
    worker.step()
    assert len(llm.calls) == 3
    assert len(slack.posted) == 1
    assert "HTTP 503" in caplog.text
    assert GAVE not in caplog.text
    assert "Disc" not in caplog.text


def test_after_a_long_outage_the_post_is_finished_without_a_reply_and_says_why(
    store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    llm = FakeLLM()  # always down
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    due(worker, clock)
    for passed in range(1, RETRY_FOR_SECONDS):
        clock.at(f"{1790000030 + passed}.000000")
        worker.step()
    assert replies(path) == [(ROOT, None, False)]
    assert len(llm.calls) <= 12 + RETRY_FOR_SECONDS // MAX_BACKOFF_SECONDS  # backoff, capped at a minute
    clock.at(f"{1790000030 + RETRY_FOR_SECONDS}.000000")
    worker.step()
    assert replies(path) == [(ROOT, None, True)]
    assert slack.posted == []
    record = store.dry_run(LLM_KEY)
    assert record is not None
    assert record.result is not None
    assert (record.outcome, record.extractor) == (DryRunOutcome.SILENT, extract_rules.EXTRACTOR)
    assert record.result["declined"] is True
    assert record.result["llm_unavailable"] is True
    calls = len(llm.calls)
    clock.at(f"{1790000090 + RETRY_FOR_SECONDS}.000000")
    worker.step()
    assert len(llm.calls) == calls


def test_an_outage_costs_one_call_per_interval_however_many_posts_wait(
    store: Store, slack: FakeSlack, clock: Clock
) -> None:
    llm = FakeLLM()
    worker = worker_with(llm, store, slack, clock)
    for number in range(5):
        enqueue(store, message(f"179000000{number}.000100", GAVE, user=f"U0TECH000{number}"))
    due(worker, clock)
    assert len(llm.calls) == 1
    clock.at("1790000031.000000")
    worker.step()
    assert len(llm.calls) == 1
    clock.at("1790000032.000000")
    worker.step()
    assert len(llm.calls) == 2
    # The backoff grows with the outage, not with one post's tries: the first post is withheld (edited), and
    # the next one still waits 4 s, then 8 s.
    store.note_revision(OWNER, PostKey(WORKSPACE, CHANNEL, "1790000000.000100"), None)
    clock.at("1790000035.000000")
    worker.step()
    assert len(llm.calls) == 2
    clock.at("1790000036.000000")
    worker.step()
    assert len(llm.calls) == 3
    clock.at("1790000043.000000")
    worker.step()
    assert len(llm.calls) == 3
    clock.at("1790000044.000000")
    worker.step()
    assert len(llm.calls) == 4
    assert PostKey(WORKSPACE, CHANNEL, "1790000000.000100") not in worker._failures  # finished: nothing is kept


def test_the_queue_is_drained_between_two_model_calls(store: Store, slack: FakeSlack, clock: Clock, path: Path) -> None:
    llm = FakeLLM(read(), read())
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    enqueue(store, message(OTHER, GAVE, user="U0LUIS0005"))
    due(worker, clock)
    assert len(llm.calls) == 1  # one call in a step
    edit = message("1790000020.000100", "", subtype="message_changed")
    edit["event"]["message"] = {"type": "message", "user": "U0LUIS0005", "text": "gave truck 1 three", "ts": OTHER}
    edit["event"].pop("user")
    enqueue(store, edit)
    worker.step()
    assert len(llm.calls) == 1  # the edit came first: the changed post is withheld, not read
    withheld = store.dry_run(PostKey(WORKSPACE, CHANNEL, OTHER))
    assert withheld is not None
    assert withheld.outcome is DryRunOutcome.WITHHELD


def test_a_reply_that_is_tried_again_does_not_ask_the_model_again(store: Store, slack: FakeSlack, clock: Clock) -> None:
    llm = FakeLLM(read(), read(quantity=9))
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    slack.failures = 1
    due(worker, clock)
    assert slack.posted == []
    clock.at("1790000040.000000")
    worker.step()
    assert len(slack.posted) == 1
    assert len(llm.calls) == 1
    record = store.dry_run(LLM_KEY)
    assert record is not None
    assert record.result is not None
    assert record.result["movements"][0]["items"][0]["quantity"] == "2"
    assert worker._llm_read == {}
    assert worker._failures == {}


def test_a_reply_that_fails_after_an_outage_gets_its_own_retry_window(
    store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    llm = FakeLLM(ProviderError("down"), read())
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    due(worker, clock)
    slack.failures = 10_000
    start = 1790000030 + RETRY_FOR_SECONDS - 60
    clock.at(f"{start}.000000")  # the model answers just before the outage limit
    worker.step()
    assert len(llm.calls) == 2
    clock.at(f"{start + 120}.000000")
    worker.step()
    assert replies(path) == [(ROOT, None, False)]  # the reply is still being tried
    clock.at(f"{start + RETRY_FOR_SECONDS}.000000")
    worker.step()
    record = store.dry_run(LLM_KEY)
    assert record is not None
    assert (record.outcome, record.extractor) == (DryRunOutcome.GAVE_UP, MODEL)
    assert len(llm.calls) == 2


def test_an_error_inside_the_extractor_fails_the_post_and_not_the_worker(
    store: Store, slack: FakeSlack, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    llm = FakeLLM(RuntimeError(GAVE), read())
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    enqueue(store, message(OTHER, GAVE, user="U0LUIS0005"))
    due(worker, clock)
    worker.step()
    first = store.dry_run(LLM_KEY)
    assert first is not None
    assert first.outcome is DryRunOutcome.FAILED
    assert [reply["thread_ts"] for reply in slack.posted] == [OTHER]
    assert GAVE not in caplog.text


def test_the_lock_is_beaten_right_before_the_model_is_asked_and_a_lost_lock_stops(
    store: Store, slack: FakeSlack, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLM(read())
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    worker.step()
    clock.at("1790000030.000000")
    beats: list[int] = []
    heartbeat = store.heartbeat

    def counted(owner: str) -> None:
        beats.append(len(llm.calls))
        heartbeat(owner)

    monkeypatch.setattr(store, "heartbeat", counted)
    worker._beat_at = time.monotonic()  # a fresh beat: only `now=True` beats again
    worker.step()
    assert 0 in beats
    assert len(llm.calls) == 1

    def lost(owner: str) -> None:
        raise LockLost

    enqueue(store, message("1790000040.000100", GAVE))
    clock.at("1790000090.000000")
    monkeypatch.setattr(store, "heartbeat", lost)
    with pytest.raises(LockLost):
        worker.step()
    assert len(llm.calls) == 1


class SlowLLM(FakeLLM):
    """Every call takes `seconds` on the worker's clock before it answers or fails."""

    def __init__(self, clock: Clock, seconds: int, *answers: Extraction | Exception) -> None:
        super().__init__(*answers)
        self.clock, self.seconds = clock, seconds
        self.at: list[float] = []

    def extract(self, messages: Sequence[MessageInput]) -> Extraction:
        self.at.append(self.clock.now.timestamp())
        self.clock.now += timedelta(seconds=self.seconds)
        return super().extract(messages)


def poll(worker: Worker, clock: Clock, seconds: int) -> None:
    end = clock.now + timedelta(seconds=seconds)
    while clock.now < end:
        clock.now += timedelta(seconds=1)
        worker.step()


@pytest.mark.parametrize("seconds", [30, 61, 120])
def test_a_provider_that_is_slow_to_fail_is_still_backed_off_from_and_holds_no_other_post_back(
    store: Store, slack: FakeSlack, clock: Clock, seconds: int
) -> None:
    llm = SlowLLM(clock, seconds)  # always down, after a long wait
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    enqueue(store, message(OTHER, TAKE, user=LUIS))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()  # the declined post is first: one slow call
    assert len(llm.calls) == 1
    assert slack.posted == []
    poll(worker, clock, 1)
    assert [reply["thread_ts"] for reply in slack.posted] == [OTHER]  # on the very next step
    assert len(llm.calls) == 1
    poll(worker, clock, 600)
    ended = [start + seconds for start in llm.at]
    waits = [start - end for start, end in zip(llm.at[1:], ended, strict=False)]
    assert len(waits) >= 5
    # Counted from the end of the call that failed, and capped at a minute.
    assert waits == ([2, 4, 8, 16, 32] + [MAX_BACKOFF_SECONDS] * len(waits))[: len(waits)]


def test_a_post_that_gains_a_message_after_the_model_read_it_is_read_again(
    store: Store, slack: FakeSlack, clock: Clock
) -> None:
    llm = FakeLLM(read(), read(quantity=3))
    worker = worker_with(llm, store, slack, clock)
    catalogs: list[Catalog | Exception] = [ConnectionError("synthetic outage"), CATALOG]
    worker.catalog = lambda: _next(catalogs)
    enqueue(store, message(ROOT, GAVE))
    worker.step()
    clock.at("1790000030.000000")
    with pytest.raises(ConnectionError):
        worker.step()  # read by the model, then the catalog is down: the post stays open
    assert len(llm.calls) == 1
    enqueue(store, message("1790000031.000100", "sorry, three Disc 40 not two", thread_ts=ROOT))
    clock.at("1790000032.000000")
    worker.step()
    clock.at("1790000060.000000")
    worker.step()
    assert llm.calls == [[GAVE], [GAVE, "sorry, three Disc 40 not two"]]
    record = store.dry_run(LLM_KEY)
    assert record is not None
    assert record.result is not None
    assert record.result["movements"][0]["items"][0]["quantity"] == "3"
    assert worker._llm_read == {}


def _next(catalogs: list[Catalog | Exception]) -> Catalog:
    found = catalogs.pop(0) if len(catalogs) > 1 else catalogs[0]
    if isinstance(found, Exception):
        raise found
    return found


def test_a_post_behind_the_first_is_not_given_up_on_without_a_call_of_its_own(
    store: Store, slack: FakeSlack, clock: Clock
) -> None:
    llm = FakeLLM()
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    enqueue(store, message(OTHER, "gave truck 1 two ONT 100 from main stock", user=LUIS))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    poll(worker, clock, RETRY_FOR_SECONDS - 2)
    assert all(call == [GAVE] for call in llm.calls)  # the first post took every call so far
    llm.answers = [ProviderError("down"), read(item="ONT 100")]  # the provider is back for the second post
    poll(worker, clock, 3)
    first, second = store.dry_run(LLM_KEY), store.dry_run(PostKey(WORKSPACE, CHANNEL, OTHER))
    assert first is not None
    assert first.outcome is DryRunOutcome.SILENT
    assert second is not None
    assert second.outcome is DryRunOutcome.REPLIED
    assert llm.calls[-1] == ["gave truck 1 two ONT 100 from main stock"]


def test_a_finished_post_keeps_nothing_of_what_the_model_read(store: Store, slack: FakeSlack, clock: Clock) -> None:
    llm = FakeLLM(Extraction(MODEL, (), declined=True))
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    due(worker, clock)
    assert worker._llm_read == {}
    assert worker._failures == {}
