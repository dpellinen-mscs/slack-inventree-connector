"""The worker: stored events become posts, and a post whose window has passed gets one dry-run reply."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError

from stockthread.config import Settings, parse_settings
from stockthread.core import extract_rules
from stockthread.core.matching import Catalog, LocationRecord, PartRecord
from stockthread.core.model import (
    CandidateMovement,
    Extraction,
    LineItem,
    MessageInput,
    MovementType,
    PostKey,
    ReasonCode,
)
from stockthread.llm.extract import NoReading, ProviderError
from stockthread.slack.ingest import Drop, Revised
from stockthread.slack.listener import Listener
from stockthread.slack.replies import APPROVERS_ONLY, CLICK_FEEDBACK, NO_APPROVERS
from stockthread.store import DryRunOutcome, LockLost, NewQuestion, StateConflict, Store
from stockthread.store import db as store_db
from stockthread.worker import (
    DRAIN_BATCH,
    GRACE_SECONDS,
    LATE_AFTER_SECONDS,
    MAX_BACKOFF_SECONDS,
    MAX_POSTS_PER_STEP,
    NOTE_QUIET_SECONDS,
    RETRY_FOR_SECONDS,
    ROOT_WAIT_SECONDS,
    TAKE_DOWN_BACKOFF_SECONDS,
    TAKE_DOWN_SPACING_SECONDS,
    TAKE_DOWN_TRIES,
    PosterBusy,
    PostStillRunning,
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
        self.notes: list[dict[str, Any]] = []
        self.note_failures = 0
        self.updates: list[dict[str, Any]] = []
        self.update_failures = 0
        self.edited: dict[str, list[dict[str, Any]]] = {}
        """Blocks of a reply as it stands after an edit, by its ts."""

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        if self.failures:
            self.failures -= 1
            raise ConnectionError("synthetic outage")
        self.posted.append(kwargs)
        return {"ok": True, "ts": f"1790009999.{len(self.posted):06d}"}

    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        """The thread's root (a person's post), then the replies the connector posted within the bounds."""
        assert kwargs["inclusive"] is True
        root = {"type": "message", "user": "U0SAM0001", "text": "a post", "ts": kwargs["ts"]}
        listed = [root]
        for n, reply in enumerate(self.posted, start=1):
            ts = f"1790009999.{n:06d}"
            if reply["thread_ts"] == kwargs["ts"] and kwargs["oldest"] <= ts <= kwargs["latest"]:
                blocks = self.edited.get(ts, reply["blocks"])
                listed.append({"type": "message", "bot_id": "B0STOCKBOT", "app_id": APP, "ts": ts,
                               "thread_ts": kwargs["ts"], "text": reply["text"], "blocks": blocks})  # fmt: skip
        return {"ok": True, "messages": listed[: kwargs["limit"]], "has_more": False}

    def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        if self.update_failures:
            self.update_failures -= 1
            raise ConnectionError("synthetic outage")
        self.updates.append(kwargs)
        self.edited[kwargs["ts"]] = list(kwargs["blocks"])
        return {"ok": True, "ts": kwargs["ts"]}

    def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:
        if self.note_failures:
            self.note_failures -= 1
            raise ConnectionError("synthetic outage")
        self.notes.append(kwargs)
        return {"ok": True, "message_ts": f"1790008888.{len(self.notes):06d}"}


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


def test_a_posted_dry_run_is_logged_without_the_text_of_the_post(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="stockthread.worker")
    enqueue(store, message("1790000000.000100", "took 2 Disc 40 from main stock to truck 1"))
    worker.step()
    assert "dry run posted" not in caplog.text
    clock.at("1790000030.000000")
    worker.step()
    assert len(slack.posted) == 1
    assert "post 1790000000.000100: dry run posted" in caplog.text
    assert "Disc" not in caplog.text


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


def revisions(path: Path) -> list[tuple[int, str, bool]]:
    with sqlite3.connect(path) as raw:
        return [
            (row[0], row[1], row[2] is not None)
            for row in raw.execute("SELECT revision, outcome, reply_ts FROM dry_run_revisions ORDER BY id")
        ]


def test_a_thread_reply_the_rules_cannot_read_takes_the_standing_reply_back_once(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = "1790000000.000100"
    enqueue(store, message(root, "took 2 Disc 40 from main stock to truck 1"))
    worker.step()
    clock.at("1790000016.000000")
    worker.step()
    enqueue(store, message("1790000020.000100", "actually make that 3", thread_ts=root))
    worker.step()
    clock.at("1790000030.000100")
    worker.step()
    assert len(slack.posted) == 1  # the window of the new message has not passed
    caplog.set_level(logging.INFO, logger="stockthread.worker")
    clock.at("1790000050.000000")
    worker.step()
    # Two messages: the rules decline the post, so the dry run above no longer stands, and the thread is told.
    assert [reply["thread_ts"] for reply in slack.posted] == [root, root]
    assert "no longer stands" in slack.posted[1]["text"]
    assert "can no longer read this thread" in json.dumps(slack.posted[1]["blocks"])
    assert revisions(path) == [(2, "retracted", True)]
    assert "revision 2 notice posted" in caplog.text
    assert "make that" not in caplog.text
    history = store.dry_run_history(PostKey(WORKSPACE, CHANNEL, root))
    assert [(record.revision, record.outcome) for record in history] == [
        (1, DryRunOutcome.REPLIED),
        (2, DryRunOutcome.RETRACTED),
    ]
    assert history[1].result is not None
    assert (history[1].result["retracted"], history[1].result["instead_of"]) == ("silent", "silent")
    assert history[1].result["messages"] == [root, "1790000020.000100"]
    enqueue(store, message("1790000060.000100", "thanks", user=LUIS, thread_ts=root))
    worker.step()
    clock.at("1790000090.000000")
    worker.step()
    assert len(slack.posted) == 2  # nothing stands any more: nothing to take back
    assert revisions(path) == [(2, "retracted", True), (3, "silent", False)]
    assert audit(path) == ["dry_run_replied"]
    assert statuses(path) == [("done", None)] * 3


def test_a_post_without_a_standing_reply_is_read_again_without_a_notice(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message(ROOT, "lunch anyone"))
    due(worker, clock)
    enqueue(store, message("1790000040.000100", "took 2 Disc 40 from main stock to truck 1", thread_ts=ROOT))
    due(worker, clock, 1790000060)
    assert slack.posted == []  # the rules decline a thread; nothing was said before, so nothing is said now
    assert revisions(path) == [(2, "silent", False)]


def test_a_thread_root_found_after_its_reply_was_answered_alone_makes_a_revision(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    root = "1790000000.000100"
    enqueue(store, message("1790000003.000100", "took 2 Disc 40 from main stock to truck 1", thread_ts=root))
    worker.step()
    clock.at(f"{1790000000 + ROOT_WAIT_SECONDS + 1}.000000")
    worker.step()
    assert len(slack.posted) == 1  # answered alone, after waiting for the root
    enqueue(store, message(root, "heading out"))  # catch-up or a very late retry brings the root
    worker.step()
    clock.at(f"{1790000000 + ROOT_WAIT_SECONDS + 30}.000000")
    worker.step()
    assert len(slack.posted) == 2
    assert "no longer stands" in slack.posted[1]["text"]
    assert revisions(path) == [(2, "retracted", True)]


def quantities(record: Any) -> list[str]:
    """The quantity of every line item a dry-run record decided."""
    assert record is not None
    assert record.result is not None
    return [item["quantity"] for movement in record.result["movements"] for item in movement["items"]]


def test_an_edit_alone_after_the_dry_run_is_read_and_posts_a_revised_dry_run(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    key = PostKey(WORKSPACE, CHANNEL, ROOT)
    enqueue(store, message(ROOT, TAKE))
    due(worker, clock)
    assert quantities(store.dry_run(key)) == ["2"]
    three = "took 3 Disc 40 from main stock to truck 1"
    enqueue(store, edit_of(ROOT, "1790000040.000100", TAKE, three))
    worker.step()  # at once: the post's window has long passed, and an edit is not a sibling that may follow
    assert len(slack.posted) == 2
    assert slack.posted[1]["thread_ts"] == ROOT
    assert slack.posted[1]["text"] == "Revised dry run: 1 movement, nothing changed."
    record = store.dry_run(key)
    assert record is not None
    assert (record.revision, record.outcome) == (2, DryRunOutcome.REPLIED)
    assert quantities(record) == ["3"]  # the edited text, not the original
    assert statuses(path) == [("done", None), ("done", "revision_noted")]

    # A second edit of the same message is noted and read too.
    enqueue(store, edit_of(ROOT, "1790000041.000100", three, "took 4 Disc 40 from main stock to truck 1"))
    worker.step()
    assert len(slack.posted) == 3
    assert quantities(store.dry_run(key)) == ["4"]

    # Slack does not keep event order: an edit from before the newest one, delivered after it, is noted (it
    # makes a revision) and the post still reads as its newest edit shows it.
    stale = edit_of(ROOT, "1790000042.000100", TAKE, three)
    stale["event"]["message"]["edited"]["ts"] = "1790000040.000100"
    enqueue(store, stale)
    worker.step()
    assert len(slack.posted) == 3
    assert revisions(path) == [(2, "replied", True), (3, "replied", True), (4, "unchanged", False)]
    assert quantities(store.dry_run(key)) == ["4"]


def test_a_stored_edit_stamp_not_in_slacks_form_withholds_rather_than_stalls(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    odd = message(ROOT, TAKE)
    odd["event"]["edited"] = {"user": SAM, "ts": "yesterday"}
    enqueue(store, odd)
    enqueue(store, edit_of(ROOT, "1790000002.000100", TAKE, "took 3 Disc 40 from main stock to truck 1"))
    enqueue(store, message("1790000003.000100", TAKE, user=LUIS))  # another post, behind it
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert record is not None
    assert record.result == {"withheld": "changed"}
    assert len(slack.posted) == 1  # the other post is answered


def test_only_the_edit_that_is_read_can_make_a_post_late(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message(ROOT, TAKE))
    from_history = edit_of(ROOT, "1790000001.000100", TAKE, "took 3 Disc 40 from main stock to truck 1")
    from_history["event_id"] = f"history:{CHANNEL}:{ROOT}:changed:1790000001.000100"
    enqueue(store, from_history)
    enqueue(store, edit_of(ROOT, "1790000002.000100", TAKE, "took 4 Disc 40 from main stock to truck 1"))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert quantities(record) == ["4"]
    assert record is not None
    assert record.result is not None
    assert record.result["late"] is False  # the history's edit was superseded by one Slack delivered


def test_an_edit_that_reads_the_same_posts_nothing(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message(ROOT, TAKE))
    due(worker, clock)
    enqueue(store, edit_of(ROOT, "1790000040.000100", TAKE, "took 2 Disc 40 from main stock to truck 1 "))
    worker.step()
    assert len(slack.posted) == 1  # a typo fixed, a space added: the same movement
    assert revisions(path) == [(2, "unchanged", False)]


def test_a_deletion_alone_after_the_dry_run_takes_the_reply_back(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message(ROOT, TAKE))
    due(worker, clock)
    enqueue(store, deletion_of(ROOT, "1790000040.000100"))
    worker.step()
    assert len(slack.posted) == 2
    assert slack.posted[1]["thread_ts"] == ROOT
    assert "was deleted" in json.dumps(slack.posted[1]["blocks"])
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert record is not None
    assert (record.revision, record.outcome) == (2, DryRunOutcome.RETRACTED)
    assert record.result == {"withheld": "changed", "retracted": "changed", "instead_of": "withheld"}
    assert statuses(path) == [("done", None), ("done", "revision_noted")]

    # Nothing stands any more, and a deleted message is never read again, whatever comes after it.
    enqueue(store, edit_of(ROOT, "1790000041.000100", TAKE, "took 4 Disc 40 from main stock to truck 1"))
    worker.step()
    assert len(slack.posted) == 2
    assert revisions(path) == [(2, "retracted", True), (3, "withheld", False)]


def test_a_deleted_thread_reply_leaves_the_rest_of_the_post_to_read(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message(ROOT, TAKE))
    due(worker, clock)
    reply = "1790000040.000100"
    enqueue(store, message(reply, "on my way", thread_ts=ROOT))
    due(worker, clock, 1790000070)
    assert revisions(path) == [(2, "retracted", True)]  # the rules decline a thread of two messages
    enqueue(store, deletion_of(reply, "1790000080.000100"))
    worker.step()
    # Without the deleted reply the post is the root alone again, and nothing stood above to compare with.
    assert revisions(path) == [(2, "retracted", True), (3, "replied", True)]
    assert slack.posted[2]["text"] == "Dry run: 1 movement, nothing changed."
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert record is not None
    assert record.result is not None
    assert record.result["messages"] == [ROOT, reply]  # read through the reply, which reads as deleted


def test_an_edit_of_a_post_without_a_standing_reply_reads_the_edited_text(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message(ROOT, "lunch anyone"))
    due(worker, clock)
    enqueue(store, edit_of(ROOT, "1790000040.000100", "lunch anyone", TAKE))
    worker.step()
    assert [reply["text"] for reply in slack.posted] == ["Dry run: 1 movement, nothing changed."]
    assert revisions(path) == [(2, "replied", True)]
    worker.step()
    assert revisions(path) == [(2, "replied", True)]  # once


def test_an_edit_of_a_thread_reply_after_its_revision_starts_the_next_one(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message(ROOT, TAKE))
    due(worker, clock)
    reply = "1790000040.000100"
    enqueue(store, message(reply, "on my way", thread_ts=ROOT))
    due(worker, clock, 1790000070)
    assert revisions(path) == [(2, "retracted", True)]
    enqueue(store, edit_of(reply, "1790000080.000100", "on my way", "took 5 Disc 40 from main stock to truck 1"))
    worker.step()
    # Read again with the edited reply: still a thread of two the rules decline, and nothing stands.
    assert revisions(path) == [(2, "retracted", True), (3, "silent", False)]
    assert len(slack.posted) == 2


def test_an_edit_of_an_expired_post_starts_no_revision(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message(ROOT, TAKE))
    due(worker, clock)
    later = 1790000000 + 24 * 3600 + 60
    clock.at(f"{later}.000000")
    enqueue(store, edit_of(ROOT, f"{later}.000100", TAKE, "took 3 Disc 40 from main stock to truck 1"))
    worker.step()
    assert len(slack.posted) == 1  # the reply above has expired with its plan: nothing to take back
    assert revisions(path) == []


class SlowSlack(FakeSlack):
    """The first call is still running when the worker checks on it; it has sent by the next check."""

    def __init__(self) -> None:
        super().__init__()
        self.slow = 1

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        if self.slow:
            self.slow -= 1
            raise PostStillRunning
        return super().chat_postMessage(**kwargs)


def test_an_edit_that_arrives_while_the_reply_is_on_its_way_makes_the_next_revision(
    store: Store, clock: Clock, path: Path
) -> None:
    slack = SlowSlack()
    worker = Worker(settings(), store, OWNER, slack, lambda: CATALOG, clock)
    enqueue(store, message(ROOT, TAKE))
    due(worker, clock)
    assert slack.posted == []  # decided, and its call has not returned
    enqueue(store, edit_of(ROOT, "1790000031.000100", TAKE, "took 3 Disc 40 from main stock to truck 1"))
    clock.at("1790000040.000000")
    worker.step()  # the edit is noted; the call returns, and the reply it sent is recorded as decided
    assert len(slack.posted) == 1
    history = store.dry_run_history(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert [(record.revision, record.outcome) for record in history] == [(1, DryRunOutcome.REPLIED)]
    worker.step()  # the reply it sent was decided before the edit: the edited text is read now
    assert len(slack.posted) == 2
    assert slack.posted[1]["text"] == "Revised dry run: 1 movement, nothing changed."
    assert revisions(path) == [(2, "replied", True)]
    assert quantities(store.dry_run(PostKey(WORKSPACE, CHANNEL, ROOT))) == ["3"]


def test_a_message_that_joins_an_expired_post_starts_no_revision(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message(ROOT, TAKE))
    due(worker, clock)
    later = 1790000000 + 24 * 3600 + 60
    enqueue(store, message(f"{later}.000100", "did this ever happen?", thread_ts=ROOT))
    worker.step()
    clock.at(f"{later + 30}.000000")
    worker.step()
    # Measured from the post's first message, not the new reply. The reply above has expired with its plan: the
    # message is kept in the post, and starts no revision.
    assert len(slack.posted) == 1
    assert revisions(path) == []
    assert store.post_messages(PostKey(WORKSPACE, CHANNEL, ROOT)) == [ROOT, f"{later}.000100"]
    assert store.posts_waiting_for_dry_run(revisable_since=Decimal(later - 24 * 3600)) == []
    (waiting,) = store.posts_waiting_for_dry_run()  # unbounded, it would be due
    assert waiting.revision == 2


def test_a_revision_whose_notice_cannot_be_posted_is_given_up_on(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message(ROOT, TAKE))
    due(worker, clock)
    enqueue(store, message("1790000040.000100", "one more", thread_ts=ROOT))
    slack.failures = 10_000
    due(worker, clock, 1790000060)
    assert revisions(path) == []  # retried with backoff
    clock.at(f"{1790000060 + RETRY_FOR_SECONDS}.000000")
    worker.step()
    assert revisions(path) == [(2, "gave_up", False)]
    history = store.dry_run_history(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert history[1].result is not None
    assert history[1].result["retracted"] == "silent"


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
        ("done", "revision_noted"),
        ("done", "revision_noted"),
        ("dropped", "malformed"),  # a click is read by the ingest checks again, and this one has nothing in it
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


def test_revisions_are_recorded_in_order_and_each_reads_further(store: Store, path: Path) -> None:
    key = PostKey(WORKSPACE, CHANNEL, "1790000000.000100")
    store.place_message(OWNER, key, user_id=SAM, thread_ts=None, window_seconds=5)
    with pytest.raises(StateConflict):  # no first dry run yet
        store.finish_dry_run(OWNER, key, DryRunOutcome.SILENT, revision=2)
    with pytest.raises(ValueError, match="only a revision"):
        store.finish_dry_run(OWNER, key, DryRunOutcome.UNCHANGED)
    store.finish_dry_run(OWNER, key, DryRunOutcome.REPLIED, "1790000001.000100", read=[key.ts])
    assert [post.revision for post in store.posts_waiting_for_dry_run()] == []
    reply = PostKey(WORKSPACE, CHANNEL, "1790000009.000100")
    store.place_message(OWNER, reply, user_id=LUIS, thread_ts=key.ts, window_seconds=5)
    (waiting,) = store.posts_waiting_for_dry_run()
    assert (waiting.key, waiting.newest_ts, waiting.revision) == (key, reply.ts, 2)
    with pytest.raises(StateConflict):  # reads nothing new
        store.finish_dry_run(OWNER, key, DryRunOutcome.SILENT, revision=2, read=[key.ts])
    with pytest.raises(StateConflict):  # skips a revision
        store.finish_dry_run(OWNER, key, DryRunOutcome.SILENT, revision=3)
    with pytest.raises(ValueError, match="its own post"):
        store.finish_dry_run(OWNER, key, DryRunOutcome.SILENT, revision=2, read=[key.ts, "1790000099.000100"])
    with pytest.raises(LockLost):
        store.finish_dry_run("instance-b", key, DryRunOutcome.SILENT, revision=2)
    store.finish_dry_run(OWNER, key, DryRunOutcome.RETRACTED, "1790000010.000100", revision=2, result={"a": 1})
    assert store.posts_waiting_for_dry_run() == []
    with pytest.raises(StateConflict):  # once
        store.finish_dry_run(OWNER, key, DryRunOutcome.SILENT, revision=2)
    history = store.dry_run_history(key)
    assert [(r.revision, r.outcome, r.reply_ts) for r in history] == [
        (1, DryRunOutcome.REPLIED, "1790000001.000100"),
        (2, DryRunOutcome.RETRACTED, "1790000010.000100"),
    ]
    assert store.dry_run(key) == history[-1]
    with sqlite3.connect(path) as raw:
        for change in (
            "UPDATE dry_run_revisions SET outcome = 'replied'",
            "UPDATE dry_run_revisions SET read_through = 1",
            "UPDATE dry_run_revisions SET reply_ts = NULL",
            "UPDATE dry_run_revisions SET result = '{}'",
            "UPDATE dry_run_revisions SET rowid = 9",
            "UPDATE dry_runs SET read_through = 2",
            "DELETE FROM dry_run_revisions",
            # Out of order, reading nothing new, or a message of another post:
            "INSERT INTO dry_run_revisions (post_id, revision, read_through, changes_through, outcome, created_at)"
            " VALUES (1, 4, 3, 0, 'silent', 'now')",
            "INSERT INTO dry_run_revisions (post_id, revision, read_through, changes_through, outcome, created_at)"
            " VALUES (1, 3, 2, 0, 'silent', 'now')",
            "INSERT INTO dry_run_revisions (post_id, revision, read_through, changes_through, outcome, created_at)"
            " VALUES (1, 3, 99, 0, 'silent', 'now')",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(change)
        raw.execute("UPDATE dry_run_revisions SET result = NULL")  # retention may expire the quoted words
    assert store.dry_run_history(key)[1].result is None


def test_a_change_noted_after_a_dry_run_makes_a_revision_that_reads_no_further(store: Store, path: Path) -> None:
    key = PostKey(WORKSPACE, CHANNEL, "1790000000.000100")
    store.place_message(OWNER, key, user_id=SAM, thread_ts=None, window_seconds=5)
    assert store.post_changes_through(key) == 0
    store.finish_dry_run(OWNER, key, DryRunOutcome.REPLIED, "1790000001.000100", read=[key.ts])
    store.note_revision(OWNER, PostKey(WORKSPACE, CHANNEL, "1790000099.000100"))  # a message of no post here
    assert store.posts_waiting_for_dry_run() == []
    store.note_revision(OWNER, key)
    assert store.post_changes_through(key) == 2
    (waiting,) = store.posts_waiting_for_dry_run()
    assert (waiting.key, waiting.revision) == (key, 2)
    with pytest.raises(StateConflict):  # knows of no newer change, and reads nothing new
        store.finish_dry_run(OWNER, key, DryRunOutcome.SILENT, revision=2, read=[key.ts], changes=0)
    with pytest.raises(ValueError, match="noted for its post"):
        store.finish_dry_run(OWNER, key, DryRunOutcome.SILENT, revision=2, read=[key.ts], changes=1)
    store.finish_dry_run(OWNER, key, DryRunOutcome.WITHHELD, revision=2, read=[key.ts], changes=2)
    assert store.posts_waiting_for_dry_run() == []
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT read_through, changes_through FROM dry_run_revisions").fetchall() == [(1, 2)]
        for change in (
            "UPDATE dry_run_revisions SET changes_through = 0",
            "UPDATE dry_runs SET changes_through = 2",
            # Knowing of no newer change and reading nothing new, knowing of fewer, or of no noted change:
            "INSERT INTO dry_run_revisions (post_id, revision, read_through, changes_through, outcome, created_at)"
            " VALUES (1, 3, 1, 2, 'silent', 'now')",
            "INSERT INTO dry_run_revisions (post_id, revision, read_through, changes_through, outcome, created_at)"
            " VALUES (1, 3, 1, 0, 'silent', 'now')",
            "INSERT INTO dry_run_revisions (post_id, revision, read_through, changes_through, outcome, created_at)"
            " VALUES (1, 3, 1, 9, 'silent', 'now')",
            "INSERT INTO dry_run_revisions (post_id, revision, read_through, outcome, created_at)"
            " VALUES (1, 3, 1, 'silent', 'now')",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(change)


def test_posts_answered_before_changes_were_tracked_wait_only_for_new_changes(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = store_db._migrations()
    monkeypatch.setattr(store_db, "_migrations", lambda: real[:7])
    old = Store.open(path)
    old.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    key = PostKey(WORKSPACE, CHANNEL, "1790000000.000100")
    old.place_message(OWNER, key, user_id=SAM, thread_ts=None, window_seconds=5)
    old.note_revision(OWNER, key)  # edited after its dry run, before the upgrade
    with sqlite3.connect(path) as raw:  # this code's finish_dry_run writes the new column
        raw.execute("UPDATE posts SET dry_run_at = 'then'")
        raw.execute(
            "INSERT INTO dry_runs (post_id, outcome, read_through, created_at) VALUES (1, 'replied', 1, 'then')"
        )
    old.close()
    monkeypatch.setattr(store_db, "_migrations", lambda: real)
    upgraded = Store.open(path)
    upgraded.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    assert upgraded.posts_waiting_for_dry_run() == []  # an upgrade never answers an old edit
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT changes_through FROM dry_runs").fetchall() == [(1,)]
    upgraded.note_revision(OWNER, PostKey(WORKSPACE, CHANNEL, "1790000003.000100"))
    upgraded.place_message(
        OWNER, PostKey(WORKSPACE, CHANNEL, "1790000003.000100"), user_id=SAM, thread_ts=None, window_seconds=5
    )
    assert [(post.key, post.revision) for post in upgraded.posts_waiting_for_dry_run()] == [(key, 2)]
    upgraded.close()


def test_an_upgrade_keeps_every_noted_change_and_notes_a_second_edit_of_a_message(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = store_db._migrations()
    monkeypatch.setattr(store_db, "_migrations", lambda: real[:8])
    old = Store.open(path)
    old.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    key = PostKey(WORKSPACE, CHANNEL, ROOT)
    old.place_message(OWNER, key, user_id=SAM, thread_ts=None, window_seconds=5)
    old.note_revision(OWNER, PostKey(WORKSPACE, CHANNEL, "1790000099.000100"))  # a message of no post here
    enqueue(old, edit_of(ROOT, "1790000030.000100", TAKE, "took 3 Disc 40 from main stock to truck 1"))
    old.note_revision(OWNER, key, 1)  # its first edit; a second one would not have been noted then
    old.finish_dry_run(OWNER, key, DryRunOutcome.WITHHELD, result={"withheld": "changed"})
    old.close()
    monkeypatch.setattr(store_db, "_migrations", lambda: real)
    upgraded = Store.open(path)
    upgraded.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT id, ts FROM revised_messages ORDER BY id").fetchall() == [
            (1, "1790000099.000100"),
            (2, ROOT),
        ]
        assert raw.execute("SELECT changes_through FROM dry_runs").fetchall() == [(2,)]
        with pytest.raises(sqlite3.IntegrityError, match="knows which changes"):  # the triggers were made again
            raw.execute(
                "INSERT INTO dry_runs (post_id, outcome, read_through, changes_through, created_at)"
                " VALUES (2, 'silent', 1, 99, 'now')"
            )
    assert upgraded.posts_waiting_for_dry_run() == []
    enqueue(upgraded, edit_of(ROOT, "1790000040.000100", TAKE, "took 4 Disc 40 from main stock to truck 1"))
    upgraded.note_revision(OWNER, key, 2)  # the next edit of the message: noted, after every earlier one
    assert [(post.key, post.revision) for post in upgraded.posts_waiting_for_dry_run()] == [(key, 2)]
    assert upgraded.post_changes_through(key) == 3
    # A note from before the upgrade may not be the newest edit: it is never read, so the post is withheld.
    bodies = upgraded.post_change_bodies(key, 3)[ROOT]
    assert bodies[0] is None
    assert bodies[1] is not None
    upgraded.close()


def test_posts_answered_before_revisions_existed_wait_only_for_new_messages(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = store_db._migrations()
    monkeypatch.setattr(store_db, "_migrations", lambda: real[:6])
    old = Store.open(path)
    old.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    key = PostKey(WORKSPACE, CHANNEL, "1790000000.000100")
    old.place_message(OWNER, key, user_id=SAM, thread_ts=None, window_seconds=5)
    old.place_message(
        OWNER, PostKey(WORKSPACE, CHANNEL, "1790000003.000100"), user_id=SAM, thread_ts=None, window_seconds=5
    )
    assert old.post_messages(key) == [key.ts, "1790000003.000100"]
    with sqlite3.connect(path) as raw:  # this code's finish_dry_run writes the new column
        raw.execute("UPDATE posts SET dry_run_at = 'then'")
        raw.execute("INSERT INTO dry_runs (post_id, outcome, created_at) VALUES (1, 'silent', 'then')")
    old.close()
    monkeypatch.setattr(store_db, "_migrations", lambda: real)
    upgraded = Store.open(path)
    upgraded.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    assert upgraded.posts_waiting_for_dry_run() == []  # an upgrade never answers a post again
    upgraded.place_message(
        OWNER, PostKey(WORKSPACE, CHANNEL, "1790000020.000100"), user_id=LUIS, thread_ts=key.ts, window_seconds=5
    )
    assert [(post.key, post.revision) for post in upgraded.posts_waiting_for_dry_run()] == [(key, 2)]
    upgraded.close()


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
    assert "The post was read late" in json.dumps(slack.posted[0]["blocks"])


def test_the_clock_must_be_aware(store: Store, slack: FakeSlack) -> None:
    worker = Worker(settings(), store, OWNER, slack, lambda: CATALOG, lambda: datetime(2026, 9, 29))
    with pytest.raises(ValueError, match="aware"):
        worker.reply_due()


def test_an_edit_before_the_dry_run_is_read_in_it(
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
    assert statuses(path) == [("done", None), ("done", "revision_noted")]
    assert audit(path) == ["dry_run_replied"]
    assert len(slack.posted) == 1
    assert quantities(store.dry_run(PostKey(WORKSPACE, CHANNEL, "1790000000.000100"))) == ["3"]


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
def test_an_edit_or_deletion_that_arrives_before_its_message_still_counts(
    kind: str, worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    after = "took 3 Disc 40 from main stock to truck 1"
    first = (
        edit_of(ROOT, "1790000002.000100", TAKE, after) if kind == "edit" else deletion_of(ROOT, "1790000002.000100")
    )
    enqueue(store, first)
    worker.step()
    assert statuses(path) == [("done", "revision_noted")]  # noted: no post holds the message yet

    enqueue(store, message(ROOT, TAKE))  # Slack delivered the original late
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert record is not None
    if kind == "edit":  # never a dry run for 2 when the post now says 3
        assert audit(path) == ["dry_run_replied"]
        assert quantities(record) == ["3"]
    else:  # or is gone
        assert slack.posted == []
        assert audit(path) == ["dry_run_withheld"]
        assert (record.outcome, record.result) == (DryRunOutcome.WITHHELD, {"withheld": "changed"})


def test_an_early_edit_of_a_sibling_that_cannot_be_read_withholds_the_whole_post(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    unstamped = edit_of("1790000001.000100", "1790000002.000100", "and 1 ONT 100", "and 4 ONT 100")
    del unstamped["event"]["message"]["edited"]  # the strict checks cannot read it: kept as Revised
    enqueue(store, unstamped)
    enqueue(store, message(ROOT, TAKE))
    enqueue(store, message("1790000001.000100", "and 1 ONT 100"))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    assert slack.posted == []
    assert audit(path) == ["dry_run_withheld"]
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert record is not None
    assert record.result == {"withheld": "changed"}


def test_a_deleted_sibling_is_left_out_of_the_reading(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message(ROOT, TAKE))
    enqueue(store, message("1790000001.000100", "and 1 ONT 100"))
    enqueue(store, deletion_of("1790000001.000100", "1790000002.000100"))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    # The rules decline a post of two messages; with the sibling deleted, the root alone is read.
    assert audit(path) == ["dry_run_replied"]
    assert quantities(store.dry_run(PostKey(WORKSPACE, CHANNEL, ROOT))) == ["2"]


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


def test_every_change_event_is_noted_once(store: Store, path: Path) -> None:
    key = PostKey(WORKSPACE, CHANNEL, ROOT)
    enqueue(store, edit_of(ROOT, "1790000001.000100", TAKE, "took 3 Disc 40 from main stock to truck 1"))
    enqueue(store, edit_of(ROOT, "1790000002.000100", TAKE, "took 4 Disc 40 from main stock to truck 1"))
    store.note_revision(OWNER, key, 1)
    store.note_revision(OWNER, key, 2)
    store.note_revision(OWNER, key, 1)  # a requeued event
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT id, slack_event_id FROM revised_messages").fetchall() == [(1, 1), (2, 2)]
        with pytest.raises(sqlite3.IntegrityError, match="once per event"):
            raw.execute(
                "INSERT INTO revised_messages (workspace_id, channel_id, ts, slack_event_id, noted_at)"
                " VALUES (?, ?, ?, 2, 'now')",
                (WORKSPACE, CHANNEL, "1790000009.000100"),
            )
        with pytest.raises(sqlite3.IntegrityError, match="never"):
            raw.execute("UPDATE revised_messages SET ts = '1790000009.000100'")


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
    assert ("done", "revision_noted") in statuses(path) or ("done", "withheld_changed_post") in statuses(path)


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
MODEL = "llm-0.2/test-model"


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


def read_and_answered(worker: Worker, clock: Clock) -> None:
    """The post is due and the model is asked; the step after that posts the reply (or finishes the post)."""
    due(worker, clock)
    worker.step()


def test_a_post_the_rules_decline_is_read_by_the_model_and_gets_its_dry_run(
    store: Store, slack: FakeSlack, clock: Clock
) -> None:
    llm = FakeLLM(read())
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    read_and_answered(worker, clock)
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
    read_and_answered(worker, clock)
    assert llm.calls == [["took 2 Disc 40 from main stock", "to truck 1"]]
    assert len(slack.posted) == 1


def test_what_the_model_reads_is_still_checked_against_the_text(store: Store, slack: FakeSlack, clock: Clock) -> None:
    llm = FakeLLM(read(quantity=5))  # the post says two
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    read_and_answered(worker, clock)
    record = store.dry_run(LLM_KEY)
    assert record is not None
    assert record.result is not None
    movement = record.result["movements"][0]
    assert [reason["source"] for reason in movement["reasons"]] != []
    assert movement["gate"] != []  # it could not apply without a person


@pytest.mark.parametrize("with_photo", [True, False])
def test_a_reading_of_a_post_with_a_photo_waits_for_a_person(
    store: Store, slack: FakeSlack, clock: Clock, with_photo: bool
) -> None:
    llm = FakeLLM(read())
    worker = worker_with(llm, store, slack, clock)
    posted = message(ROOT, GAVE, subtype="file_share" if with_photo else None)
    if with_photo:
        posted["event"]["files"] = [{"id": "F0ACME0001", "mimetype": "image/jpeg"}]
    enqueue(store, posted)
    read_and_answered(worker, clock)
    record = store.dry_run(LLM_KEY)
    assert record is not None
    assert record.result is not None
    [movement] = record.result["movements"]
    unread = {"code": ReasonCode.PHOTO_NOT_READ.value, "source": "rule", "detail": ""}
    assert (unread in movement["reasons"]) is with_photo
    assert (ReasonCode.PHOTO_NOT_READ.value in [gate[0] for gate in movement["gate"]]) is with_photo


def test_a_post_the_model_declines_gets_no_reply(store: Store, slack: FakeSlack, clock: Clock, path: Path) -> None:
    llm = FakeLLM(Extraction(MODEL, (), declined=True))
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    read_and_answered(worker, clock)
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
    worker.step()
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
    assert PostKey(WORKSPACE, CHANNEL, "1790000000.000100") not in worker._llm_waiting  # finished: nothing is kept


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
    read_and_answered(worker, clock)
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
    assert worker._llm_waiting == {}


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
    worker.step()  # the first reply that fails
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
    worker.step()  # the declined post is first, but the slow call is made last
    assert len(llm.calls) == 1
    assert [reply["thread_ts"] for reply in slack.posted] == [OTHER]  # sent before the call began
    assert llm.at[0] == 1790000030
    poll(worker, clock, 1)
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
    worker.step()
    with pytest.raises(ConnectionError):
        worker.step()  # read by the model, then the catalog is down: the post stays open
    assert len(llm.calls) == 1
    enqueue(store, message("1790000031.000100", "sorry, three Disc 40 not two", thread_ts=ROOT))
    clock.at("1790000032.000000")
    worker.step()
    clock.at("1790000060.000000")
    worker.step()
    assert llm.calls == [[GAVE], [GAVE, "sorry, three Disc 40 not two"]]
    worker.step()
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


ONT = "gave truck 1 two ONT 100 from main stock"


def test_in_an_outage_the_posts_that_wait_take_turns_and_each_gets_a_last_call_of_its_own(
    store: Store, slack: FakeSlack, clock: Clock
) -> None:
    llm = FakeLLM()
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    enqueue(store, message(OTHER, ONT, user=LUIS))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    poll(worker, clock, RETRY_FOR_SECONDS - 2)
    assert llm.calls[:4] == [[GAVE], [ONT], [GAVE], [ONT]]
    assert abs(llm.calls.count([GAVE]) - llm.calls.count([ONT])) <= 1
    assert len(llm.calls) <= 12 + RETRY_FOR_SECONDS // MAX_BACKOFF_SECONDS  # still one call per interval
    llm.answers = [ProviderError("down"), read(item="ONT 100")]  # the provider is back for the second post
    poll(worker, clock, 4)
    first, second = store.dry_run(LLM_KEY), store.dry_run(PostKey(WORKSPACE, CHANNEL, OTHER))
    assert first is not None
    assert first.outcome is DryRunOutcome.SILENT
    assert first.result is not None
    assert first.result["llm_unavailable"] is True
    assert second is not None
    assert second.outcome is DryRunOutcome.REPLIED
    assert llm.calls[-1] == [ONT]


def test_a_post_the_model_refuses_is_finished_alone_and_holds_no_other_post_back(
    store: Store, slack: FakeSlack, clock: Clock, path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A healthy provider that answers, but never with a reading of the first post.
    llm = FakeLLM(NoReading("the model did not finish its answer"), read(item="ONT 100"))
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    enqueue(store, message(OTHER, ONT, user=LUIS))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    refused = store.dry_run(LLM_KEY)
    assert refused is not None
    assert refused.result is not None
    assert (refused.outcome, refused.extractor) == (DryRunOutcome.SILENT, extract_rules.EXTRACTOR)
    assert refused.result["llm_no_reading"] is True
    assert refused.result["llm_unavailable"] is False
    worker.step()  # no backoff: the next post is asked in the very next step
    worker.step()
    assert llm.calls == [[GAVE], [ONT]]
    assert [reply["thread_ts"] for reply in slack.posted] == [OTHER]
    assert worker._failures == {}
    assert worker._llm_waiting == {}
    assert worker._llm_quiet_until == 0
    assert "did not finish" in caplog.text
    assert "Disc" not in caplog.text
    poll(worker, clock, 120)
    assert len(llm.calls) == 2  # the refused post is never sent again


def test_a_refusal_during_an_outage_ends_the_backoff(store: Store, slack: FakeSlack, clock: Clock) -> None:
    llm = FakeLLM(ProviderError("down"), NoReading("refused"), read(item="ONT 100"))
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    enqueue(store, message(OTHER, ONT, user=LUIS))
    worker.step()
    clock.at("1790000030.000000")
    worker.step()  # the first post: down
    poll(worker, clock, 2)  # the second post's turn: refused, so the provider is back
    assert worker._llm_failed == 0
    poll(worker, clock, 2)
    assert llm.calls == [[GAVE], [ONT], [GAVE]]
    assert [reply["thread_ts"] for reply in slack.posted] == [ROOT]


class Flapping(FakeLLM):
    """Answers every third call (it declines the post); the others fail."""

    def extract(self, messages: Sequence[MessageInput]) -> Extraction:
        self.calls.append([message.text for message in messages])
        if len(self.calls) % 3 == 0:
            return Extraction(MODEL, (), declined=True)
        raise ProviderError("the provider answered HTTP 503")


def test_many_waiting_posts_never_crowd_a_step_out(store: Store, slack: FakeSlack, clock: Clock) -> None:
    # More posts wait for the provider than a step handles, and the provider's answers keep its backoff short:
    # the calls must go on, and a post the rules read must not wait behind them.
    llm = Flapping()
    worker = worker_with(llm, store, slack, clock)
    for number in range(2 * MAX_POSTS_PER_STEP):
        enqueue(store, message(f"{1790000000 + 10 * number}.000100", GAVE))
    worker.step()
    clock.at("1790000500.000000")
    worker.step()
    poll(worker, clock, 100)
    before = len(llm.calls)
    assert before >= 40  # a call every 2 or 4 seconds
    enqueue(store, message("1790000590.000100", TAKE, user=LUIS))
    poll(worker, clock, 40)
    assert len(llm.calls) > before
    assert [reply["thread_ts"] for reply in slack.posted] == ["1790000590.000100"]
    poll(worker, clock, RETRY_FOR_SECONDS)
    assert store.posts_waiting_for_dry_run() == []
    assert worker._llm_waiting == {}
    assert len({call[0] for call in llm.calls}) == 1  # and every call was for a post


def test_a_reply_slack_keeps_refusing_does_not_stop_the_model_calls(
    store: Store, slack: FakeSlack, clock: Clock
) -> None:
    llm = FakeLLM()
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, TAKE))  # the rules read it, and Slack takes no reply
    enqueue(store, message(OTHER, GAVE, user=LUIS))
    slack.failures = 10_000
    worker.step()
    clock.at("1790000030.000000")
    worker.step()
    poll(worker, clock, 60)
    assert len(llm.calls) >= 5
    assert LLM_KEY in worker._failures
    assert list(worker._llm_waiting) == [PostKey(WORKSPACE, CHANNEL, OTHER)]


@pytest.mark.parametrize(
    ("last_answer", "outcome", "unheard"),
    [
        (NoReading("the model refused the post"), DryRunOutcome.SILENT, "llm_no_reading"),
        (read(), DryRunOutcome.REPLIED, None),
    ],
)
def test_the_last_call_for_a_post_may_still_be_answered(
    store: Store,
    slack: FakeSlack,
    clock: Clock,
    last_answer: Extraction | Exception,
    outcome: DryRunOutcome,
    unheard: str | None,
) -> None:
    llm = FakeLLM()
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    due(worker, clock)
    poll(worker, clock, RETRY_FOR_SECONDS - 1)
    assert store.dry_run(LLM_KEY) is None
    llm.answers = [last_answer]
    poll(worker, clock, 2)
    record = store.dry_run(LLM_KEY)
    assert record is not None
    assert record.result is not None
    assert record.outcome is outcome
    assert record.result["llm_unavailable"] is False
    assert record.result["llm_no_reading"] is (unheard == "llm_no_reading")
    assert worker._llm_waiting == {}
    assert worker._llm_quiet_until == 0  # the provider is back: nothing else waits the old backoff out


def test_a_post_that_arrives_during_an_outage_gets_its_turn(store: Store, slack: FakeSlack, clock: Clock) -> None:
    llm = FakeLLM()
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    due(worker, clock)
    poll(worker, clock, 120)
    enqueue(store, message("1790000150.000100", ONT, user=LUIS))
    before = len(llm.calls)
    poll(worker, clock, 300)
    since = llm.calls[before:]
    assert len(since) >= 4
    assert [ONT] in since[:2]  # never asked: once it is due, its turn comes first
    assert abs(since.count([ONT]) - since.count([GAVE])) <= 1


class EditedMeanwhile(FakeLLM):
    """While the model reads the post, its author edits it."""

    def __init__(self, store: Store, *answers: Extraction | Exception) -> None:
        super().__init__(*answers)
        self.store = store

    def extract(self, messages: Sequence[MessageInput]) -> Extraction:
        edit = message("1790000029.000100", "", subtype="message_changed")
        edit["event"]["message"] = {"type": "message", "user": SAM, "text": "gave truck 1 three Disc 40", "ts": ROOT}
        edit["event"].pop("user")
        enqueue(self.store, edit)
        return super().extract(messages)


def test_an_edit_that_arrives_while_the_model_reads_the_post_withholds_it(
    store: Store, slack: FakeSlack, clock: Clock
) -> None:
    llm = EditedMeanwhile(store, read())
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    due(worker, clock)
    assert len(llm.calls) == 1
    assert slack.posted == []  # not in the step of the call: the queue is drained first
    worker.step()
    assert slack.posted == []
    record = store.dry_run(LLM_KEY)
    assert record is not None
    assert record.result is not None
    assert (record.outcome, record.result["withheld"]) == (DryRunOutcome.WITHHELD, "changed")
    assert worker._llm_read == {}


class StampedEditMeanwhile(FakeLLM):
    """While the model reads the post for the first time, its author makes a readable edit of it."""

    def __init__(self, store: Store, *answers: Extraction | Exception) -> None:
        super().__init__(*answers)
        self.store = store

    def extract(self, messages: Sequence[MessageInput]) -> Extraction:
        if not self.calls:
            enqueue(self.store, edit_of(ROOT, "1790000029.000100", GAVE, GAVE.replace("two", "three")))
        return super().extract(messages)


def test_a_readable_edit_that_arrives_while_the_model_reads_the_post_is_read_again(
    store: Store, slack: FakeSlack, clock: Clock
) -> None:
    llm = StampedEditMeanwhile(store, read(), read(quantity=3))
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    due(worker, clock)
    worker.step()
    worker.step()
    # The timestamps are the same before and after the edit: only the text tells the two readings apart.
    assert llm.calls == [[GAVE], [GAVE.replace("two", "three")]]
    record = store.dry_run(LLM_KEY)
    assert record is not None
    assert record.outcome is DryRunOutcome.REPLIED
    assert quantities(record) == ["3"]
    assert len(slack.posted) == 1
    assert worker._llm_read == {}


def test_a_finished_post_keeps_nothing_of_what_the_model_read(store: Store, slack: FakeSlack, clock: Clock) -> None:
    llm = FakeLLM(Extraction(MODEL, (), declined=True))
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, GAVE))
    read_and_answered(worker, clock)
    assert worker._llm_read == {}
    assert worker._failures == {}
    assert worker._llm_waiting == {}


# Revisions read by the model: the same reading posts nothing, a different one posts a revised dry run.

THREAD = "1790000040.000100"


def test_a_revision_that_reads_as_the_standing_reply_posts_nothing(
    store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    llm = FakeLLM(read())
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, TAKE))  # the rules read it: the model is not asked
    due(worker, clock)
    enqueue(store, message(THREAD, "on the way", thread_ts=ROOT))
    due(worker, clock, 1790000060)
    worker.step()
    assert llm.calls == [[TAKE, "on the way"]]
    assert len(slack.posted) == 1
    assert revisions(path) == [(2, "unchanged", False)]
    record = store.dry_run(LLM_KEY)
    assert record is not None
    assert (record.revision, record.outcome, record.extractor) == (2, DryRunOutcome.UNCHANGED, MODEL)


def test_a_revision_that_reads_differently_posts_a_revised_dry_run(
    store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    llm = FakeLLM(read(3))
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, TAKE))
    due(worker, clock)
    enqueue(store, message(THREAD, "make that 3", thread_ts=ROOT))
    due(worker, clock, 1790000060)
    worker.step()
    assert [reply["thread_ts"] for reply in slack.posted] == [ROOT, ROOT]
    assert slack.posted[1]["text"] == "Revised dry run: 1 movement, nothing changed."
    assert "Revised dry run" in json.dumps(slack.posted[1]["blocks"])
    assert revisions(path) == [(2, "replied", True)]
    history = store.dry_run_history(LLM_KEY)
    assert history[1].reply_ts == "1790009999.000002"
    assert history[1].result is not None
    assert history[1].result["movements"][0]["items"][0]["quantity"] == "3"
    assert history[1].result["messages"] == [ROOT, THREAD]
    clock.at("1790000200.000000")
    worker.step()
    assert len(slack.posted) == 2  # read through the new message: not due again


def test_a_message_that_joins_while_a_revision_is_read_makes_the_next_revision(
    store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    llm = FakeLLM(read(3), read(4))
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, TAKE))
    due(worker, clock)
    enqueue(store, message(THREAD, "make that 3", thread_ts=ROOT))
    due(worker, clock, 1790000060)  # the model reads the thread; the reply waits for the next step
    enqueue(store, message("1790000061.000100", "no, 4", thread_ts=ROOT))
    worker.step()  # the kept reading no longer covers the post: not posted
    clock.at("1790000090.000000")
    worker.step()
    worker.step()
    assert llm.calls == [[TAKE, "make that 3"], [TAKE, "make that 3", "no, 4"]]
    assert len(slack.posted) == 2
    assert revisions(path) == [(2, "replied", True)]
    history = store.dry_run_history(LLM_KEY)
    assert history[1].result is not None
    assert history[1].result["movements"][0]["items"][0]["quantity"] == "4"


def test_a_movement_read_after_its_reply_was_taken_back_is_a_plain_dry_run(
    store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    llm = FakeLLM(NoReading("the model declined"), read(3))
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, TAKE))
    due(worker, clock)
    enqueue(store, message(THREAD, "hmm", thread_ts=ROOT))
    due(worker, clock, 1790000060)
    enqueue(store, message("1790000070.000100", "make that 3", thread_ts=ROOT))
    due(worker, clock, 1790000100)
    worker.step()
    assert [reply["text"] for reply in slack.posted] == [
        "Dry run: 1 movement, nothing changed.",
        "The dry run above no longer stands. Nothing was changed in inventory.",
        "Dry run: 1 movement, nothing changed.",  # nothing stands above it to revise
    ]
    assert revisions(path) == [(2, "retracted", True), (3, "replied", True)]
    # The model gave no reading: the notice does not blame the thread.
    assert "language model gave no answer" in json.dumps(slack.posted[1]["blocks"])
    history = store.dry_run_history(LLM_KEY)
    assert history[1].result is not None
    assert history[1].result["retracted"] == "unread"


def test_lateness_alone_does_not_make_a_revised_reply(store: Store, slack: FakeSlack, clock: Clock, path: Path) -> None:
    llm = FakeLLM(read())
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, TAKE))
    due(worker, clock, 1790000000 + LATE_AFTER_SECONDS + 60)  # answered late: the reply carries late_ingest
    first = store.dry_run(LLM_KEY)
    assert first is not None
    assert first.result is not None
    assert first.result["late"] is True
    thread = f"{1790000000 + LATE_AFTER_SECONDS + 100}.000100"
    enqueue(store, message(thread, "on the way", thread_ts=ROOT))
    due(worker, clock, 1790000000 + LATE_AFTER_SECONDS + 130)  # read on time: no late_ingest
    worker.step()
    assert len(slack.posted) == 1
    assert revisions(path) == [(2, "unchanged", False)]


def test_a_standing_reply_whose_record_expired_is_revised_rather_than_called_unchanged(
    store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    llm = FakeLLM(read())
    worker = worker_with(llm, store, slack, clock)
    enqueue(store, message(ROOT, TAKE))
    due(worker, clock)
    with sqlite3.connect(path) as raw:
        raw.execute("UPDATE dry_runs SET result = NULL")  # retention expired the quoted words
    enqueue(store, message(THREAD, "on the way", thread_ts=ROOT))
    due(worker, clock, 1790000060)
    worker.step()
    assert [reply["text"] for reply in slack.posted][1] == "Revised dry run: 1 movement, nothing changed."
    assert revisions(path) == [(2, "replied", True)]


# Questions (design section 4, "Questions"): the one question a post needs is asked in its reply.

NO_SOURCE = "took 2 Disc 40 to truck 1"


def test_a_post_with_a_reason_asks_its_question_in_the_reply(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock
) -> None:
    enqueue(store, message(ROOT, NO_SOURCE))
    due(worker, clock)
    [reply] = slack.posted
    assert reply["blocks"][-2] == {"type": "divider", "block_id": "question"}
    assert "Where was it taken from?" in reply["blocks"][-1]["text"]["text"]
    assert reply["text"] == "Dry run: 1 movement, nothing changed."
    [item] = store.questions(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert item.question["ask"] == "name_location"
    assert item.question["side"] == "source"
    assert (item.revision, item.message_ts, item.approver_only) == (1, "1790009999.000001", False)


def test_a_clear_post_asks_nothing(worker: Worker, store: Store, slack: FakeSlack, clock: Clock) -> None:
    enqueue(store, message(ROOT, TAKE))
    due(worker, clock)
    assert all(block["type"] != "actions" for block in slack.posted[0]["blocks"])
    assert store.questions(PostKey(WORKSPACE, CHANNEL, ROOT)) == []


def test_each_button_carries_the_id_of_its_stored_row(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock
) -> None:
    enqueue(store, message(ROOT, TAKE))
    worker.step()
    clock.at(f"{1790000000 + LATE_AFTER_SECONDS + 60}.000000")  # read late: only an approver clears that
    worker.step()
    [reply] = slack.posted
    actions = reply["blocks"][-1]
    assert actions["type"] == "actions"
    [item] = store.questions(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert item.approver_only is True
    assert item.question["reason"] == "late_ingest"
    found = []
    for n, element in enumerate(actions["elements"]):
        assert element["action_id"] == f"answer-{n}"
        button = store.button(element["value"])
        assert button is not None
        assert (button.item, button.position) == (item, n)
        found.append(button.choice["kind"])
    assert found == ["yes", "no"]
    assert len({element["value"] for element in actions["elements"]}) == 2


def test_a_reply_still_on_its_way_is_stored_with_the_buttons_it_was_sent_with(store: Store, clock: Clock) -> None:
    slow = SlowSlack()
    worker = Worker(settings(), store, OWNER, slow, lambda: CATALOG, clock)
    enqueue(store, message(ROOT, TAKE))
    worker.step()
    clock.at(f"{1790000000 + LATE_AFTER_SECONDS + 60}.000000")
    worker.step()
    assert slow.posted == []  # still running
    assert store.questions(PostKey(WORKSPACE, CHANNEL, ROOT)) == []
    clock.at(f"{1790000000 + LATE_AFTER_SECONDS + 120}.000000")
    worker.step()
    [reply] = slow.posted
    for element in reply["blocks"][-1]["elements"]:
        assert store.button(element["value"]) is not None


def test_a_reply_that_is_given_up_on_stores_no_question(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message(ROOT, NO_SOURCE))
    slack.failures = 10_000
    due(worker, clock)
    clock.at(f"{1790000030 + RETRY_FOR_SECONDS}.000000")
    worker.step()
    assert replies(path) == [(ROOT, None, True)]
    assert store.questions(PostKey(WORKSPACE, CHANNEL, ROOT)) == []


def test_a_notice_that_the_reply_no_longer_stands_makes_its_question_stale(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    enqueue(store, message(ROOT, NO_SOURCE))
    due(worker, clock)
    enqueue(store, message("1790000040.000100", "from main stock", thread_ts=ROOT))
    due(worker, clock, 1790000060)
    assert revisions(path) == [(2, "retracted", True)]  # the rules read no thread
    [item] = store.questions(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert item.status.value == "stale"
    assert all(block["type"] != "actions" for block in slack.posted[1]["blocks"])


@pytest.mark.parametrize(
    ("after", "outcome"), [("took two Disc 40 to truck 1", "unchanged"), ("took 3 Disc 40 to truck 1", "replied")]
)
def test_an_edit_keeps_the_question_or_asks_it_again_in_the_revised_reply(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, path: Path, after: str, outcome: str
) -> None:
    enqueue(store, message(ROOT, NO_SOURCE))
    due(worker, clock)
    enqueue(store, edit_of(ROOT, "1790000040.000100", NO_SOURCE, after))
    due(worker, clock, 1790000060)
    assert revisions(path) == [(2, outcome, outcome == "replied")]
    items = store.questions(PostKey(WORKSPACE, CHANNEL, ROOT))
    if outcome == "unchanged":
        assert [(item.revision, item.status.value, item.message_ts) for item in items] == [
            (1, "open", "1790009999.000001")
        ]
        assert len(slack.posted) == 1
    else:
        assert [(item.revision, item.status.value, item.message_ts) for item in items] == [
            (1, "stale", "1790009999.000001"),
            (2, "open", "1790009999.000002"),
        ]
        assert "Where was it taken from?" in slack.posted[1]["blocks"][-1]["text"]["text"]


def test_a_question_that_cannot_be_put_leaves_the_reply_without_it(
    worker: Worker, store: Store, slack: FakeSlack, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("synthetic wording bug")

    monkeypatch.setattr("stockthread.worker.question_message", broken)
    enqueue(store, message(ROOT, NO_SOURCE))
    due(worker, clock)
    [reply] = slack.posted
    assert "No source location was stated." in json.dumps(reply["blocks"])
    assert all(block["type"] != "divider" for block in reply["blocks"])
    assert store.questions(PostKey(WORKSPACE, CHANNEL, ROOT)) == []
    assert store.dry_run(PostKey(WORKSPACE, CHANNEL, ROOT)).outcome is DryRunOutcome.REPLIED  # type: ignore[union-attr]


# Clicks on a question's buttons (design section 4, "Authorization").

TWO_DISCS = Catalog(
    parts=(PartRecord(1, "DISC-40-P", "Disc 40 Pro"), PartRecord(2, "DISC-40-L", "Disc 40 Lite")),
    locations=CATALOG.locations,
)
RIVER = "U0MIKE0002"


def approving(*channels: str) -> Settings:
    data = {
        "slack": {"workspace_id": WORKSPACE, "app_id": APP, "channels": list(channels or (CHANNEL,))},
        "inventree": {"url": "http://inventree.local:8000"},
    }
    data["slack"]["approvers"] = [LUIS]  # type: ignore[index]
    return parse_settings(data, ENV)


def click_on(reply: dict[str, Any], ts: str, n: int, user: str, *, at: str = "1790000100.000100") -> dict[str, Any]:
    """The payload Slack sends when `user` clicks button `n` of a reply posted at `ts`."""
    element = reply["blocks"][-1]["elements"][n]
    channel = reply["channel"]
    return {
        "type": "block_actions",
        "api_app_id": APP,
        "team": {"id": WORKSPACE},
        "user": {"id": user, "team_id": WORKSPACE},
        "channel": {"id": channel},
        "container": {"type": "message", "message_ts": ts, "channel_id": channel, "is_ephemeral": False},
        "message": {"type": "message", "ts": ts, "thread_ts": reply["thread_ts"], "text": reply["text"]},
        "actions": [{**element, "type": "button", "action_ts": at}],
    }


def clicked(store: Store, payload: dict[str, Any], key: str) -> None:
    assert store.enqueue_event(f"click:{key}", "interactive", json.dumps(payload))


def asked(store: Store, clock: Clock, catalog: Catalog = TWO_DISCS, text: str = TAKE, *, late: bool = False) -> Worker:
    worker = Worker(approving(), store, OWNER, FakeSlack(), lambda: catalog, clock)
    enqueue(store, message(ROOT, text))
    worker.step()
    clock.at(f"{1790000000 + LATE_AFTER_SECONDS + 60}.000000" if late else "1790000030.000000")
    worker.step()
    return worker


def posted(worker: Worker) -> tuple[dict[str, Any], str]:
    slack = worker.slack
    assert isinstance(slack, FakeSlack)
    [reply] = slack.posted
    return reply, "1790009999.000001"


def last_status(path: Path) -> tuple[str, str | None]:
    return statuses(path)[-1]


def notes(worker: Worker, channel: str = CHANNEL) -> list[tuple[str, str]]:
    """Who was told what about their clicks, each in the thread they clicked in."""
    slack = worker.slack
    assert isinstance(slack, FakeSlack)
    assert all((note["channel"], note["thread_ts"]) == (channel, ROOT) for note in slack.notes)
    return [(note["user"], note["text"]) for note in slack.notes]


def test_the_poster_answers_their_question_by_button(store: Store, clock: Clock, path: Path) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    [item] = store.questions(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert item.question["ask"] == "choose_part"
    clicked(store, click_on(reply, ts, 1, SAM), "1")
    worker.step()
    assert last_status(path) == ("done", "answered")
    [item] = store.questions(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert item.status.value == "answered"
    assert store.answer(item.id) == ({"button": 1, "kind": "record", "record": 2, "movement_type": None}, SAM)
    assert notes(worker) == [(SAM, CLICK_FEEDBACK["answered"])]
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT actor FROM audit_log WHERE event = 'question_answered'").fetchall() == [("human",)]


def test_an_approver_may_answer_anyone_else_may_not(store: Store, clock: Clock, path: Path) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    clicked(store, click_on(reply, ts, 0, RIVER), "1")
    worker.step()
    assert last_status(path) == ("dropped", "not_allowed")
    clicked(store, click_on(reply, ts, 0, LUIS), "2")
    worker.step()
    assert last_status(path) == ("done", "answered")
    [item] = store.questions(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert store.answer(item.id) == ({"button": 0, "kind": "record", "record": 1, "movement_type": None}, LUIS)
    assert notes(worker) == [(RIVER, CLICK_FEEDBACK["not_allowed"]), (LUIS, CLICK_FEEDBACK["answered"])]


def test_only_an_approver_answers_a_question_for_approvers(store: Store, clock: Clock, path: Path) -> None:
    worker = asked(store, clock, CATALOG, late=True)
    reply, ts = posted(worker)
    [item] = store.questions(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert item.approver_only is True
    clicked(store, click_on(reply, ts, 0, SAM, at="1790090000.000100"), "1")  # the poster
    worker.step()
    assert last_status(path) == ("dropped", "not_allowed")
    clicked(store, click_on(reply, ts, 0, LUIS, at="1790090000.000200"), "2")
    worker.step()
    assert last_status(path) == ("done", "answered")
    assert notes(worker) == [(SAM, APPROVERS_ONLY), (LUIS, CLICK_FEEDBACK["answered"])]


def test_a_question_is_answered_once(store: Store, clock: Clock, path: Path) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    clicked(store, click_on(reply, ts, 0, SAM), "1")
    clicked(store, click_on(reply, ts, 1, LUIS, at="1790000100.000200"), "2")
    worker.step()
    assert statuses(path)[-2:] == [("done", "answered"), ("dropped", "already_answered")]
    [item] = store.questions(PostKey(WORKSPACE, CHANNEL, ROOT))
    assert store.answer(item.id) == ({"button": 0, "kind": "record", "record": 1, "movement_type": None}, SAM)
    assert notes(worker) == [(SAM, CLICK_FEEDBACK["answered"]), (LUIS, CLICK_FEEDBACK["already_answered"])]


@pytest.mark.parametrize(
    ("change", "outcome"),
    [
        ({"value": "x" * 22}, "unknown_button"),
        ({"message_ts": "1790009999.000002"}, "other_message"),
        ({"channel": "C0ACME002"}, "other_message"),
        ({"action_id": "undo"}, "action_not_handled_yet"),
        ({"team": "T0OTHER01"}, "wrong_workspace"),
    ],
)  # the out-of-date note for the first three; nothing is said about a button that does nothing or a payload
# the ingest checks drop
def test_a_click_that_does_not_fit_its_stored_button_answers_nothing(
    store: Store, clock: Clock, path: Path, change: dict[str, str], outcome: str
) -> None:
    worker = asked(store, clock)
    worker.settings = approving(CHANNEL, "C0ACME002")
    reply, ts = posted(worker)
    payload = click_on(reply, change.get("message_ts", ts), 0, SAM)
    if "value" in change:
        payload["actions"][0]["value"] = change["value"]
    if "channel" in change:
        payload["channel"]["id"] = payload["container"]["channel_id"] = change["channel"]
    if "action_id" in change:
        payload["actions"][0]["action_id"] = change["action_id"]
    if "team" in change:
        payload["team"]["id"] = change["team"]
    clicked(store, payload, "1")
    worker.step()
    assert last_status(path) == ("dropped", outcome)
    assert [item.status.value for item in store.questions(PostKey(WORKSPACE, CHANNEL, ROOT))] == ["open"]
    told = outcome in ("unknown_button", "other_message")
    said = notes(worker, change.get("channel", CHANNEL))
    assert said == ([(SAM, CLICK_FEEDBACK[outcome])] if told else [])


def test_a_click_on_a_stale_question_answers_nothing(store: Store, clock: Clock, path: Path) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    enqueue(store, message("1790000040.000100", "the pro one", thread_ts=ROOT))
    due(worker, clock, 1790000060)
    assert revisions(path) == [(2, "retracted", True)]
    clicked(store, click_on(reply, ts, 0, SAM), "1")
    worker.step()
    assert last_status(path) == ("dropped", "stale")
    assert notes(worker) == [(SAM, CLICK_FEEDBACK["stale"])]


@pytest.mark.parametrize(("past", "outcome"), [(".000100", "answered"), (".000101", "expired")])
def test_a_click_after_the_plan_expired_answers_nothing(
    store: Store, clock: Clock, path: Path, past: str, outcome: str
) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    clock.at(f"{1790000000 + worker.settings.slack.plan_expiry_hours * 3600}{past}")  # the post is at .000100
    clicked(store, click_on(reply, ts, 0, SAM), "1")
    worker.step()
    assert last_status(path) == ("done" if outcome == "answered" else "dropped", outcome)
    expected = "This question has expired: a post can be changed for 24 hours after it was made."
    assert notes(worker) == [(SAM, CLICK_FEEDBACK["answered"] if outcome == "answered" else expected)]


def test_the_plan_of_a_reply_answered_without_its_root_expires_from_the_reply(
    store: Store, clock: Clock, path: Path
) -> None:
    root, reply_ts = "1789990000.000100", "1790000000.000100"  # the root is older, and never arrives
    worker = Worker(approving(), store, OWNER, FakeSlack(), lambda: TWO_DISCS, clock)
    enqueue(store, message(reply_ts, TAKE, user=RIVER, thread_ts=root))
    due(worker, clock)
    reply, ts = posted(worker)
    [item] = store.questions(PostKey(WORKSPACE, CHANNEL, root))
    assert (item.poster_id, item.expires_from) == (RIVER, reply_ts)
    clock.at("1790082800.000000")  # 23 hours after the reply, 25.8 after the root
    clicked(store, click_on(reply, ts, 0, RIVER), "1")
    worker.step()
    assert last_status(path) == ("done", "answered")


def test_a_click_while_the_post_waits_for_its_revision_answers_nothing(store: Store, clock: Clock, path: Path) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    enqueue(store, message("1790000040.000100", "the pro one", thread_ts=ROOT))
    clicked(store, click_on(reply, ts, 0, SAM), "1")
    clock.at("1790000041.000000")
    worker.step()  # the message is placed, then the click is read; the revision is not due yet
    assert last_status(path) == ("dropped", "post_changed")
    assert notes(worker) == [(SAM, CLICK_FEEDBACK["post_changed"])]
    assert [item.status.value for item in store.questions(PostKey(WORKSPACE, CHANNEL, ROOT))] == ["open"]


class CheckingSlack(FakeSlack):
    """Notes what the click's event looks like in the store when its note is sent."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.seen: list[tuple[str, str | None]] = []

    def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:
        self.seen.append(last_status(self.path))
        return super().chat_postEphemeral(**kwargs)


def test_the_note_is_sent_once_the_click_is_finished(store: Store, clock: Clock, path: Path) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    worker.slack = slack = CheckingSlack(path)
    clicked(store, click_on(reply, ts, 1, SAM), "1")
    worker.step()
    assert slack.seen == [("done", "answered")]


def test_a_note_that_fails_is_logged_not_sent_again_and_quiets_notes_for_a_while(
    store: Store, clock: Clock, path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    slack = worker.slack
    assert isinstance(slack, FakeSlack)
    slack.note_failures = 1
    clicked(store, click_on(reply, ts, 1, SAM), "1")
    with caplog.at_level(logging.WARNING):
        worker.step()
    assert last_status(path) == ("done", "answered")
    assert "click feedback" in caplog.text
    assert "synthetic outage" not in caplog.text
    worker.step()
    clicked(store, click_on(reply, ts, 1, SAM, at="1790000100.000200"), "2")
    worker.step()  # within NOTE_QUIET_SECONDS of the failure: a slow Slack is not asked again
    assert notes(worker) == []
    clock.at(f"{1790000030 + NOTE_QUIET_SECONDS}.000000")
    clicked(store, click_on(reply, ts, 1, LUIS, at="1790000100.000300"), "3")
    worker.step()
    assert notes(worker) == [(LUIS, CLICK_FEEDBACK["already_answered"])]


def test_a_click_handled_again_after_a_crash_tells_the_clicker_their_answer_is_recorded(
    store: Store, clock: Clock, path: Path
) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    button = store.button(reply["blocks"][-1]["elements"][1]["value"])
    assert button is not None
    store.answer_question(OWNER, button, SAM)  # recorded, then the process died before the click was finished
    clicked(store, click_on(reply, ts, 1, SAM), "1")
    clicked(store, click_on(reply, ts, 0, LUIS, at="1790000100.000200"), "2")
    worker.step()
    assert statuses(path)[-2:] == [("dropped", "already_answered"), ("dropped", "already_answered")]
    assert notes(worker) == [(SAM, CLICK_FEEDBACK["answered"]), (LUIS, CLICK_FEEDBACK["already_answered"])]


def test_the_same_users_other_button_after_their_answer_is_told_it_was_not_recorded(
    store: Store, clock: Clock, path: Path
) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    clicked(store, click_on(reply, ts, 1, SAM), "1")
    clicked(store, click_on(reply, ts, 0, SAM, at="1790000100.000200"), "2")  # changes their mind: refused
    worker.step()
    assert statuses(path)[-2:] == [("done", "answered"), ("dropped", "already_answered")]
    assert notes(worker) == [(SAM, CLICK_FEEDBACK["answered"]), (SAM, CLICK_FEEDBACK["already_answered"])]
    button = store.button(reply["blocks"][-1]["elements"][1]["value"])
    assert button is not None
    assert store.answer(button.item.id) == (
        {"button": 1, "kind": "record", "record": 2, "movement_type": None},
        SAM,
    )


def test_a_note_belongs_to_its_own_click_only(store: Store, clock: Clock, path: Path) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    clicked(store, click_on(reply, ts, 1, SAM), "1")
    enqueue(store, message("1790000040.000100", "anyone seen my coffee?"))
    clicked(store, {"type": "block_actions", "garbage": True}, "2")
    worker.step()  # one batch: a click, a message, a click the ingest checks drop
    assert notes(worker) == [(SAM, CLICK_FEEDBACK["answered"])]


def test_no_note_is_sent_when_the_click_cannot_be_finished(
    store: Store, clock: Clock, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    clicked(store, click_on(reply, ts, 1, SAM), "1")

    def lost(*args: Any, **kwargs: Any) -> None:
        raise LockLost("synthetic")

    monkeypatch.setattr(store, "finish_event", lost)
    with pytest.raises(LockLost):
        worker.step()
    assert notes(worker) == []


def test_no_note_is_sent_once_the_worker_is_stopping(store: Store, clock: Clock, path: Path) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    worker.stopping = lambda: True
    clicked(store, click_on(reply, ts, 1, SAM), "1")
    worker.drain()
    assert last_status(path) == ("done", "answered")
    assert notes(worker) == []


def test_a_button_of_a_post_in_another_workspace_answers_nothing(store: Store, clock: Clock, path: Path) -> None:
    other = PostKey("T0OTHER01", CHANNEL, ROOT)
    store.place_message(OWNER, other, user_id=SAM, thread_ts=None, window_seconds=5)
    question = NewQuestion({"ask": "confirm"}, False, [("y" * 22, {"kind": "yes"})], poster=SAM, expires_from=ROOT)
    store.finish_dry_run(OWNER, other, DryRunOutcome.REPLIED, "1790009999.000001", question=question)
    clock.at("1790000100.000000")
    worker = Worker(approving(), store, OWNER, FakeSlack(), lambda: CATALOG, clock)
    reply = {"channel": CHANNEL, "thread_ts": ROOT, "text": "", "blocks": [{"elements": [
        {"action_id": "answer-0", "value": "y" * 22, "text": {"type": "plain_text", "text": "Yes"}}]}]}  # fmt: skip
    clicked(store, click_on(reply, "1790009999.000001", 0, SAM), "1")
    worker.step()
    assert last_status(path) == ("dropped", "other_message")


@pytest.mark.parametrize(
    ("late", "user", "outcome"),
    [(False, SAM, "answered"), (True, SAM, "not_allowed"), (True, LUIS, "not_allowed")],
)
def test_with_no_approvers_the_poster_still_answers_and_nobody_answers_for_approvers(
    store: Store, clock: Clock, path: Path, late: bool, user: str, outcome: str
) -> None:
    worker = asked(store, clock, CATALOG if late else TWO_DISCS, late=late)
    worker.settings = settings()  # no approvers
    reply, ts = posted(worker)
    clicked(store, click_on(reply, ts, 0, user, at="1790090000.000100"), "1")
    worker.step()
    assert last_status(path) == ("done" if outcome == "answered" else "dropped", outcome)
    told = {"answered": CLICK_FEEDBACK["answered"], "not_allowed": NO_APPROVERS}[outcome]
    assert notes(worker) == [(user, told)]


@pytest.mark.parametrize(
    ("name", "outcome"),
    [
        ("block_actions_answer", "answered"),
        ("block_actions_answer_other_user", "not_allowed"),
        ("block_actions_answer_other_message", "other_message"),
        ("block_actions_answer_other_channel", "channel_not_allowed"),
    ],
)
def test_recorded_answer_clicks_are_checked_against_the_stored_button(
    store: Store, clock: Clock, path: Path, name: str, outcome: str
) -> None:
    fixture = json.loads((Path(__file__).parent / "fixtures" / "slack" / "events" / f"{name}.json").read_text())
    button = fixture["body"]["actions"][0]["value"]
    store.place_message(OWNER, PostKey(WORKSPACE, CHANNEL, ROOT), user_id=SAM, thread_ts=None, window_seconds=5)
    buttons: list[tuple[str, dict[str, Any]]] = [
        ("p" * 22, {"kind": "record", "record": 1}),
        (button, {"kind": "record", "record": 2}),
        ("n" * 22, {"kind": "none"}),
    ]
    question = NewQuestion({"ask": "choose_part"}, False, buttons, poster=SAM, expires_from=ROOT)
    store.finish_dry_run(OWNER, PostKey(WORKSPACE, CHANNEL, ROOT), DryRunOutcome.REPLIED, "1790000020.000200",
                         question=question)  # fmt: skip
    clock.at("1790000300.000000")
    worker = Worker(approving(), store, OWNER, FakeSlack(), lambda: CATALOG, clock)
    clicked(store, fixture["body"], name)
    worker.step()
    assert last_status(path) == ("done" if outcome == "answered" else "dropped", outcome)
    [item] = store.questions(PostKey(WORKSPACE, CHANNEL, ROOT))
    expected = ({"button": 1, "kind": "record", "record": 2}, SAM) if outcome == "answered" else None
    assert store.answer(item.id) == expected


# Buttons taken down once their question is closed (design section 4, "Interactivity").


def buttons(path: Path) -> list[tuple[str, str, int]]:
    with sqlite3.connect(path) as raw:
        return raw.execute("SELECT status, buttons, buttons_tries FROM review_items ORDER BY id").fetchall()


def buttons_audit(path: Path) -> list[str]:
    with sqlite3.connect(path) as raw:
        return [row[0] for row in raw.execute("SELECT event FROM audit_log WHERE event LIKE 'buttons_%' ORDER BY id")]


def test_a_replaced_question_is_taken_out_of_its_reply(store: Store, clock: Clock, path: Path) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    enqueue(store, message("1790000040.000100", "the pro one", thread_ts=ROOT))
    due(worker, clock, 1790000060)
    slack = worker.slack
    assert isinstance(slack, FakeSlack)
    [update] = slack.updates
    marker = reply["blocks"].index({"type": "divider", "block_id": "question"})
    assert update["channel"] == CHANNEL
    assert update["ts"] == ts
    assert update["text"] == reply["text"]
    assert update["blocks"][:-1] == reply["blocks"][:marker]
    assert update["blocks"][-1]["elements"][0]["text"] == "This question was replaced by a newer reply in this thread."
    assert buttons(path) == [("stale", "removed", 0)]
    assert buttons_audit(path) == ["buttons_removed"]
    worker.step()
    assert len(slack.updates) == 1  # once


def test_an_answered_question_is_taken_out_and_says_who_answered(store: Store, clock: Clock, path: Path) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    slack = worker.slack
    assert isinstance(slack, FakeSlack)
    worker.step()
    assert slack.updates == []  # an open question keeps its buttons
    clicked(store, click_on(reply, ts, 1, SAM), "1")
    worker.step()
    [update] = slack.updates
    assert update["blocks"][-1]["elements"][0]["text"] == f"This question was answered by <@{SAM}>."
    assert all(block["type"] != "actions" for block in update["blocks"])
    assert buttons(path) == [("answered", "removed", 0)]


@pytest.mark.parametrize(("past", "expired"), [(".000100", False), (".000101", True)])
def test_an_expired_question_is_taken_out_and_says_so(
    store: Store, clock: Clock, path: Path, past: str, expired: bool
) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    slack = worker.slack
    assert isinstance(slack, FakeSlack)
    clock.at(f"{1790000000 + worker.settings.slack.plan_expiry_hours * 3600}{past}")  # the post is at .000100
    worker.step()
    if not expired:  # the same instant a click still answers at
        assert slack.updates == []
        assert buttons(path) == [("open", "shown", 0)]
        return
    [update] = slack.updates
    assert update["blocks"][-1]["elements"][0]["text"] == (
        "This question expired, so nothing from this post will be applied. To record it, post it again."
    )
    assert all(block["type"] != "actions" for block in update["blocks"])
    assert buttons(path) == [("open", "removed", 0)]
    clicked(store, click_on(reply, ts, 0, SAM), "1")  # a client that still shows the buttons
    worker.step()
    assert last_status(path) == ("dropped", "expired")
    worker.step()
    assert len(slack.updates) == 1  # once


def test_buttons_that_cannot_be_taken_down_are_tried_with_backoff_then_left(
    store: Store, clock: Clock, path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    slack = worker.slack
    assert isinstance(slack, FakeSlack)
    slack.update_failures = TAKE_DOWN_TRIES
    clicked(store, click_on(reply, ts, 1, SAM), "1")
    worker.step()
    assert buttons(path) == [("answered", "shown", 1)]
    worker.step()
    assert buttons(path) == [("answered", "shown", 1)]  # not before its backoff
    at = 1790000030
    for tries, wait in enumerate([TAKE_DOWN_BACKOFF_SECONDS, 2 * TAKE_DOWN_BACKOFF_SECONDS], start=2):
        at += wait
        clock.at(f"{at}.000000")
        worker.step()
        assert buttons(path)[0][2] == tries
    assert buttons(path) == [("answered", "left", 3)]
    assert slack.updates == []
    assert buttons_audit(path) == ["buttons_left"]
    assert "synthetic outage" not in caplog.text


class EditedButLostSlack(FakeSlack):
    """Slack makes the edit, and the answer never comes back."""

    def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.edited[kwargs["ts"]] = list(kwargs["blocks"])
        raise TimeoutError("synthetic")


def test_an_edit_that_went_through_unseen_counts_as_done(store: Store, clock: Clock, path: Path) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    lost = EditedButLostSlack()
    lost.posted = worker.slack.posted  # type: ignore[attr-defined]
    worker.slack = lost
    clicked(store, click_on(reply, ts, 1, SAM), "1")
    worker.step()
    assert buttons(path) == [("answered", "shown", 1)]
    clock.at(f"{1790000030 + TAKE_DOWN_BACKOFF_SECONDS}.000000")
    worker.step()
    assert buttons(path) == [("answered", "removed", 1)]


@pytest.mark.parametrize("change", ["gone", "not_ours", "no_marker"])
def test_a_reply_that_is_gone_or_not_ours_or_has_no_question_is_left_alone(
    store: Store, clock: Clock, path: Path, change: str
) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    slack = worker.slack
    assert isinstance(slack, FakeSlack)
    real = slack.conversations_replies

    def replies(**kwargs: Any) -> dict[str, Any]:
        found = real(**kwargs)
        if change == "gone":
            found["messages"] = found["messages"][:1]
        elif change == "not_ours":
            found["messages"][-1]["app_id"] = "A0OTHER999"
        else:
            found["messages"][-1]["blocks"] = [b for b in found["messages"][-1]["blocks"] if b["type"] != "divider"]
        return found

    slack.conversations_replies = replies  # type: ignore[method-assign]
    clicked(store, click_on(reply, ts, 1, SAM), "1")
    worker.step()
    assert slack.updates == []
    assert buttons(path) == [("answered", "left", 0)]


def test_the_connectors_own_edit_of_a_reply_is_not_a_revision(store: Store, clock: Clock, path: Path) -> None:
    worker = asked(store, clock)
    _, ts = posted(worker)
    edit = {
        "type": "event_callback",
        "team_id": WORKSPACE,
        "api_app_id": APP,
        "event_id": "Ev0EDIT0001",
        "event": {
            "type": "message",
            "subtype": "message_changed",
            "channel": CHANNEL,
            "ts": "1790000200.000100",
            "message": {"type": "message", "bot_id": "B0STOCKBOT", "app_id": APP, "text": "edited", "ts": ts},
            "previous_message": {"type": "message", "bot_id": "B0STOCKBOT", "app_id": APP, "text": "", "ts": ts},
        },
    }
    enqueue(store, edit)
    worker.step()
    assert last_status(path) == ("dropped", "bot")


class BusySlack(FakeSlack):
    """Another side call is still on its way (run.DeadlinePoster), then Slack rate-limits."""

    def __init__(self) -> None:
        super().__init__()
        self.busy = 1
        self.limited = 0

    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        if self.busy:
            self.busy -= 1
            raise PosterBusy
        if self.limited:
            self.limited -= 1
            limited = SimpleNamespace(headers={"Retry-After": "300"}, data={})
            raise SlackApiError("ratelimited", limited)  # type: ignore[no-untyped-call]
        return super().conversations_replies(**kwargs)


def test_a_busy_slot_costs_no_try_and_a_rate_limit_waits_as_long_as_slack_asks(
    store: Store, clock: Clock, path: Path
) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    busy = BusySlack()
    busy.posted = worker.slack.posted  # type: ignore[attr-defined]
    worker.slack = busy
    clicked(store, click_on(reply, ts, 1, SAM), "1")
    worker.step()
    assert buttons(path) == [("answered", "shown", 0)]  # nothing was tried
    busy.limited = 1
    clock.at(f"{1790000030 + TAKE_DOWN_SPACING_SECONDS}.000000")
    worker.step()
    assert buttons(path) == [("answered", "shown", 1)]
    clock.at(f"{1790000030 + TAKE_DOWN_SPACING_SECONDS + 299}.000000")
    worker.step()
    assert busy.updates == []  # Retry-After (300 s) is longer than the backoff (60 s)
    clock.at(f"{1790000030 + TAKE_DOWN_SPACING_SECONDS + 300}.000000")
    worker.step()
    assert buttons(path) == [("answered", "removed", 1)]


def test_one_reply_is_edited_per_spacing(store: Store, clock: Clock, path: Path) -> None:
    slack = FakeSlack()
    worker = Worker(approving(), store, OWNER, slack, lambda: TWO_DISCS, clock)
    enqueue(store, message(ROOT, TAKE))
    enqueue(store, message("1790000010.000100", TAKE))  # a second post, past the sibling window
    due(worker, clock, 1790000040)
    first, second = slack.posted
    clicked(store, click_on(first, "1790009999.000001", 1, SAM), "1")
    clicked(store, click_on(second, "1790009999.000002", 1, SAM, at="1790000100.000200"), "2")
    worker.step()
    assert [status for status, _, _ in buttons(path)] == ["answered", "answered"]
    assert len(slack.updates) == 1
    worker.step()
    assert len(slack.updates) == 1  # within the spacing
    clock.at(f"{1790000040 + TAKE_DOWN_SPACING_SECONDS}.000000")
    worker.step()
    assert [update["ts"] for update in slack.updates] == ["1790009999.000001", "1790009999.000002"]


class LimitedSlack(FakeSlack):
    """Slack rate-limits every read of a thread, asking for 300 s."""

    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        limited = SimpleNamespace(headers={"Retry-After": "300"}, data={})
        raise SlackApiError("ratelimited", limited)  # type: ignore[no-untyped-call]


def test_the_last_failed_try_of_a_question_still_pauses_the_others(store: Store, clock: Clock, path: Path) -> None:
    slack = LimitedSlack()
    worker = Worker(approving(), store, OWNER, slack, lambda: TWO_DISCS, clock)
    enqueue(store, message(ROOT, TAKE))
    enqueue(store, message("1790000010.000100", TAKE))
    due(worker, clock, 1790000040)
    first, second = slack.posted
    with sqlite3.connect(path) as raw:
        raw.execute("UPDATE review_items SET buttons_tries = ?", (TAKE_DOWN_TRIES - 1,))
    clicked(store, click_on(first, "1790009999.000001", 1, SAM), "1")
    clicked(store, click_on(second, "1790009999.000002", 1, SAM, at="1790000100.000200"), "2")
    worker.step()
    assert buttons(path) == [("answered", "left", TAKE_DOWN_TRIES), ("answered", "shown", TAKE_DOWN_TRIES - 1)]
    clock.at(f"{1790000040 + 299}.000000")
    worker.step()
    assert buttons(path)[1] == ("answered", "shown", TAKE_DOWN_TRIES - 1)  # Slack asked for 300 s
    clock.at(f"{1790000040 + 300}.000000")
    worker.step()
    assert buttons(path)[1] == ("answered", "left", TAKE_DOWN_TRIES)


# Acting on an answer (design section 4, "Acting on an answer").

SHUFFLED = "shuffled 2 Disc 40 from main stock to truck 1"  # declined by the rules
KEY = PostKey(WORKSPACE, CHANNEL, ROOT)


def unclear(kind: MovementType = MovementType.UNCLEAR) -> Extraction:
    movement = CandidateMovement(kind, (LineItem("Disc 40", Decimal(2)),), "main stock", "truck 1")
    return Extraction(MODEL, (movement,))


def reply_text(reply: dict[str, Any]) -> str:
    return json.dumps(reply["blocks"])


def test_a_part_picked_by_button_is_applied_in_a_revised_dry_run_at_once(
    store: Store, clock: Clock, path: Path
) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    [item] = store.questions(KEY)
    clicked(store, click_on(reply, ts, 1, SAM), "1")
    worker.step()  # no sibling window: the answer is acted on in the same step
    slack = worker.slack
    assert isinstance(slack, FakeSlack)
    assert len(slack.posted) == 2
    revised = slack.posted[1]
    assert revised["text"].startswith("Revised dry run")
    assert "Disc 40 Lite" in reply_text(revised)
    assert "*Revised dry run* (a question was answered)." in reply_text(revised)
    assert "this could apply" in reply_text(revised)
    record = store.dry_run(KEY)
    assert record is not None
    assert record.revision == 2
    assert record.outcome is DryRunOutcome.REPLIED
    assert record.result is not None
    part = record.result["movements"][0]["items"][0]["part"]
    assert part == {"status": "matched", "rule": "answer", "record": 2, "candidates": []}
    assert record.result["answers"] == [item.id]
    assert record.result["ignored"] == []
    assert record.result["rejected"] is None
    assert [q.status.value for q in store.questions(KEY)] == ["answered"]  # nothing else to ask
    worker.step()
    assert len(slack.posted) == 2  # the answer is applied once


def test_none_of_these_leaves_the_line_unmatched_and_asks_for_the_part_by_name(store: Store, clock: Clock) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    clicked(store, click_on(reply, ts, 2, SAM), "1")  # the third button: none of these
    worker.step()
    record = store.dry_run(KEY)
    assert record is not None
    assert record.result is not None
    assert record.result["movements"][0]["items"][0]["part"]["status"] == "unmatched"
    first, second = store.questions(KEY)
    assert (first.status.value, second.status.value) == ("answered", "open")
    assert second.question["ask"] == "name_part"
    assert second.revision == 2


def test_a_type_picked_is_validated_again_and_the_model_is_not_asked_again(store: Store, clock: Clock) -> None:
    llm = FakeLLM(unclear())
    slack = FakeSlack()
    worker = Worker(approving(), store, OWNER, slack, lambda: CATALOG, clock, llm=llm)
    enqueue(store, message(ROOT, SHUFFLED))
    read_and_answered(worker, clock)
    [reply] = slack.posted
    [item] = store.questions(KEY)
    assert item.question["ask"] == "movement_type"
    kinds = [kind for kind in MovementType if kind is not MovementType.UNCLEAR]
    clicked(
        store, click_on({**reply, "channel": CHANNEL}, "1790009999.000001", kinds.index(MovementType.RETURN), SAM), "1"
    )
    worker.step()
    assert len(llm.calls) == 1  # only answers are new: the reading that stands is kept
    assert len(slack.posted) == 2
    record = store.dry_run(KEY)
    assert record is not None
    assert record.result is not None
    assert record.extractor == MODEL
    assert record.result["extracted"] == ["unclear"]
    movement = record.result["movements"][0]
    assert movement["type"] == "return"
    assert ["contradiction", "a return whose destination is a vehicle"] in movement["gate"]
    [_, contradiction] = store.questions(KEY)
    assert (contradiction.question["ask"], contradiction.question["reason"]) == ("confirm", "contradiction")
    # The next answer replays both onto the reading as extracted, not as the type answer left it.
    clicked(store, click_on({**slack.posted[1], "channel": CHANNEL}, "1790009999.000002", 0, SAM), "2")  # yes
    worker.step()
    assert len(llm.calls) == 1
    record = store.dry_run(KEY)
    assert record is not None
    assert record.result is not None
    assert record.revision == 3
    assert record.result["answers"] == [item.id, contradiction.id]
    assert record.result["movements"][0]["type"] == "return"
    assert "contradiction" not in {reason["code"] for reason in record.result["movements"][0]["reasons"]}


def test_no_to_a_doubt_about_the_reading_ends_the_plan(store: Store, clock: Clock) -> None:
    llm = FakeLLM(unclear(MovementType.RETURN))  # a return onto a truck: a contradiction
    slack = FakeSlack()
    worker = Worker(approving(), store, OWNER, slack, lambda: CATALOG, clock, llm=llm)
    enqueue(store, message(ROOT, SHUFFLED))
    read_and_answered(worker, clock)
    [reply] = slack.posted
    [item] = store.questions(KEY)
    assert (item.question["ask"], item.question["reason"]) == ("confirm", "contradiction")
    clicked(store, click_on({**reply, "channel": CHANNEL}, "1790009999.000001", 1, SAM), "1")  # no
    worker.step()
    rejected = slack.posted[1]
    assert rejected["text"] == "The reading above was rejected. Nothing will be applied."
    assert all(block["type"] != "actions" for block in rejected["blocks"])
    record = store.dry_run(KEY)
    assert record is not None
    assert record.result is not None
    assert record.result["rejected"] == item.id
    assert [q.status.value for q in store.questions(KEY)] == ["answered"]  # nothing more is asked


def test_yes_to_a_doubt_about_the_reading_clears_it_and_nothing_else(store: Store, clock: Clock) -> None:
    llm = FakeLLM(unclear(MovementType.RETURN))
    slack = FakeSlack()
    worker = Worker(approving(), store, OWNER, slack, lambda: CATALOG, clock, llm=llm)
    enqueue(store, message(ROOT, SHUFFLED))
    read_and_answered(worker, clock)
    [reply] = slack.posted
    clicked(store, click_on({**reply, "channel": CHANNEL}, "1790009999.000001", 0, SAM), "1")  # yes
    worker.step()
    record = store.dry_run(KEY)
    assert record is not None
    assert record.result is not None
    codes = {reason["code"] for reason in record.result["movements"][0]["reasons"]}
    assert "contradiction" not in codes
    assert "movement_unclear" in codes  # "shuffled ... truck 1" still does not say which way: asked next
    [_, following] = store.questions(KEY)
    assert following.question["reason"] == "movement_unclear"


def test_yes_to_a_doubt_about_the_text_leaves_the_unclear_type_to_ask(store: Store, clock: Clock) -> None:
    slack = FakeSlack()
    worker = Worker(approving(), store, OWNER, slack, lambda: CATALOG, clock, llm=FakeLLM(unclear()))
    enqueue(store, message(ROOT, "shuffled 2 Disc 40 main stock to truck 1"))  # no "from": which way is unclear
    read_and_answered(worker, clock)
    [reply] = slack.posted
    [item] = store.questions(KEY)
    assert (item.question["ask"], item.question["detail"]) == (
        "confirm",
        "the text does not put the source on that side",
    )
    clicked(store, click_on({**reply, "channel": CHANNEL}, "1790009999.000001", 0, SAM), "1")  # yes
    worker.step()
    record = store.dry_run(KEY)
    assert record is not None
    assert record.result is not None
    reasons = {(r["code"], r["detail"]) for r in record.result["movements"][0]["reasons"]}
    assert ("movement_unclear", "the text does not put the source on that side") not in reasons
    assert ("movement_unclear", "type unclear") in reasons
    [_, following] = store.questions(KEY)
    assert following.question["ask"] == "movement_type"


def test_an_approval_answer_counts_only_from_someone_who_is_an_approver_when_it_is_applied(
    store: Store, clock: Clock, path: Path
) -> None:
    asked(store, clock, CATALOG, late=True)
    [item] = store.questions(KEY)
    assert item.approver_only
    button = store.button(next(iter(_button_ids(path))))
    assert button is not None
    assert store.answer_question(OWNER, button, LUIS).value == "answered"  # clicked while an approver
    slack = FakeSlack()
    no_approvers = Worker(settings(), store, OWNER, slack, lambda: CATALOG, clock)
    no_approvers.step()
    record = store.dry_run(KEY)
    assert record is not None
    assert record.result is not None
    assert record.result["answers"] == []
    assert record.result["ignored"] == [item.id]
    assert "late_ingest" in {reason["code"] for reason in record.result["movements"][0]["reasons"]}
    [revised] = slack.posted
    assert revised["text"].startswith("Revised dry run")  # and the question is asked again
    [_, again] = store.questions(KEY)
    assert again.question["reason"] == "late_ingest"
    assert again.status.value == "open"


def test_an_approval_answer_from_an_approver_clears_its_reason(store: Store, clock: Clock) -> None:
    worker = asked(store, clock, CATALOG, late=True)
    reply, ts = posted(worker)
    clicked(store, click_on(reply, ts, 0, LUIS, at="1790090000.000200"), "1")
    worker.step()
    record = store.dry_run(KEY)
    assert record is not None
    assert record.result is not None
    assert record.result["movements"][0]["reasons"] == []
    assert "this could apply" in reply_text(worker.slack.posted[1])  # type: ignore[attr-defined]


def test_answers_are_not_carried_over_to_a_post_read_otherwise(store: Store, clock: Clock) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    clicked(store, click_on(reply, ts, 1, SAM), "1")
    worker.step()
    assert len(worker.slack.posted) == 2  # type: ignore[attr-defined]
    enqueue(store, edit_of(ROOT, "1790000200.000100", TAKE, "took 3 Disc 40 from main stock to truck 1"))
    clock.at("1790000300.000000")
    worker.step()
    record = store.dry_run(KEY)
    assert record is not None
    assert record.result is not None
    assert record.revision == 3
    assert record.result["answers"] == []
    assert record.result["movements"][0]["items"][0]["part"]["status"] == "ambiguous"
    first, second = store.questions(KEY)
    assert first.status.value == "answered"
    assert second.status.value == "open"
    assert second.revision == 3
    assert second.question["ask"] == "choose_part"


def test_an_edit_that_reads_the_same_keeps_the_answers(store: Store, clock: Clock) -> None:
    worker = asked(store, clock)
    reply, ts = posted(worker)
    clicked(store, click_on(reply, ts, 1, SAM), "1")
    worker.step()
    [item] = store.questions(KEY)
    enqueue(store, edit_of(ROOT, "1790000200.000100", TAKE, "took 2 Disc 40 from main stock to truck 1 "))
    clock.at("1790000300.000000")
    worker.step()
    assert len(worker.slack.posted) == 2  # type: ignore[attr-defined]
    record = store.dry_run(KEY)
    assert record is not None
    assert record.result is not None
    assert record.outcome is DryRunOutcome.UNCHANGED
    assert record.result["answers"] == [item.id]


def _button_ids(path: Path) -> list[str]:
    with sqlite3.connect(path) as raw:
        return [row[0] for row in raw.execute("SELECT id FROM interactions ORDER BY position")]


def test_answers_given_before_the_upgrade_start_no_revision(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = store_db._migrations()
    monkeypatch.setattr(store_db, "_migrations", lambda: real[:12])
    old = Store.open(path)
    old.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    old.place_message(OWNER, KEY, user_id=SAM, thread_ts=None, window_seconds=5)
    question = NewQuestion({"ask": "confirm"}, False, [("y" * 22, {"kind": "yes"})], poster=SAM, expires_from=ROOT)
    old.finish_dry_run(OWNER, KEY, DryRunOutcome.REPLIED, "1790009999.000001", result={}, question=question)
    old.release_lock(OWNER)
    old.close()
    with sqlite3.connect(path) as raw:  # as the store before migration 0013 recorded a click (it reads no later column)
        raw.execute(
            "UPDATE review_items SET status = 'answered', answer = ?, answered_by = ?, closed_at = ?",
            ('{"button": 0, "kind": "yes"}', SAM, "2026-09-30T12:00:00.000000+00:00"),
        )
    monkeypatch.setattr(store_db, "_migrations", lambda: real)
    store = Store.open(path)
    try:
        assert store.posts_waiting_for_dry_run() == []
        assert store.post_answers_through(KEY) == 1
        assert store.answers(KEY, 1) == []  # never replayed, even onto a reading that stays the same
    finally:
        store.close()


def test_a_revision_needs_something_new_and_an_answer_is_something_new(store: Store) -> None:
    store.place_message(OWNER, KEY, user_id=SAM, thread_ts=None, window_seconds=5)
    question = NewQuestion({"ask": "confirm"}, False, [("y" * 22, {"kind": "yes"})], poster=SAM, expires_from=ROOT)
    store.finish_dry_run(OWNER, KEY, DryRunOutcome.REPLIED, "1790009999.000001", result={}, question=question)
    assert store.posts_waiting_for_dry_run() == []
    with pytest.raises(StateConflict):
        store.finish_dry_run(OWNER, KEY, DryRunOutcome.UNCHANGED, revision=2, result={})
    button = store.button("y" * 22)
    assert button is not None
    assert store.answer_question(OWNER, button, SAM).value == "answered"
    [waiting] = store.posts_waiting_for_dry_run()
    assert (waiting.key, waiting.revision) == (KEY, 2)
    with pytest.raises(ValueError, match="answers given about its post"):
        store.finish_dry_run(OWNER, KEY, DryRunOutcome.UNCHANGED, revision=2, result={}, answers=2)
    with pytest.raises(StateConflict):
        store.finish_dry_run(OWNER, KEY, DryRunOutcome.UNCHANGED, revision=2, result={}, answers=0)
    store.finish_dry_run(OWNER, KEY, DryRunOutcome.UNCHANGED, revision=2, result={}, answers=1)
    assert store.posts_waiting_for_dry_run() == []
    [(item, answer, by)] = store.answers(KEY, 1)
    assert (item.status.value, answer["kind"], by) == ("answered", "yes", SAM)
    assert store.answers(KEY, 0) == []


def test_yes_to_one_line_above_the_limit_leaves_the_other_line_to_ask(store: Store, clock: Clock) -> None:
    text = "shuffled 5000 Disc 40 and 6000 ONT 100 from main stock to truck 1"
    lines = (LineItem("Disc 40", Decimal(5000)), LineItem("ONT 100", Decimal(6000)))
    movement = CandidateMovement(MovementType.TAKE_TO_VEHICLE, lines, "main stock", "truck 1")
    slack = FakeSlack()
    worker = Worker(
        approving(), store, OWNER, slack, lambda: CATALOG, clock, llm=FakeLLM(Extraction(MODEL, (movement,)))
    )
    enqueue(store, message(ROOT, text))
    read_and_answered(worker, clock)
    [reply] = slack.posted
    [item] = store.questions(KEY)
    assert (item.question["reason"], item.question["line"]) == ("quantity_above_limit", 0)
    clicked(store, click_on({**reply, "channel": CHANNEL}, "1790009999.000001", 0, LUIS), "1")  # yes
    worker.step()
    record = store.dry_run(KEY)
    assert record is not None
    assert record.result is not None
    reasons = [(r["code"], r["detail"].split(":")[0]) for r in record.result["movements"][0]["reasons"]]
    assert ("quantity_above_limit", "line 1") not in reasons
    assert ("quantity_above_limit", "line 2") in reasons
    [_, following] = store.questions(KEY)
    assert (following.question["reason"], following.question["line"]) == ("quantity_above_limit", 1)
