"""The worker: stored events become posts, and a post whose window has passed gets one dry-run reply."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from stockthread.config import Settings, parse_settings
from stockthread.core import extract_rules
from stockthread.core.matching import Catalog, LocationRecord, PartRecord
from stockthread.core.model import PostKey
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
