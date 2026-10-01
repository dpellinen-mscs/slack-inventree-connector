"""Catch-up: what Slack did not deliver is read from the channel history, stored as events, and answered late."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError

from stockthread.config import Settings, parse_settings
from stockthread.core.matching import Catalog, LocationRecord, PartRecord
from stockthread.core.model import PostKey
from stockthread.slack import catchup
from stockthread.slack.catchup import OVERLAP_SECONDS, SETTLE_SECONDS, CatchUp, is_catch_up
from stockthread.store import DryRunOutcome, Store
from stockthread.worker import Worker

OWNER = "instance-a"
WORKSPACE, APP, CHANNEL, OTHER = "T0ACME001", "A0ACME001", "C0ACME001", "C0ACME002"
SAM, LUIS = "U0SAM0001", "U0LUIS0005"
CATALOG = Catalog(
    parts=(PartRecord(1, "DISC-40", "Disc 40"), PartRecord(2, "ONT-100", "ONT 100")),
    locations=(LocationRecord(10, "Main stock"), LocationRecord(11, "Vehicles/Truck 1")),
)
ENV = {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic", "INVENTREE_TOKEN": "inv-synthetic"}
TAKE = "took 2 Disc 40 from main stock to truck 1"
START = "1790000000.000000"
"""When the connector first connected: the first sweep stands here."""


def settings(channels: tuple[str, ...] = (CHANNEL,), hours: int = 72) -> Settings:
    data = {
        "slack": {"workspace_id": WORKSPACE, "app_id": APP, "channels": list(channels), "catch_up_hours": hours},
        "inventree": {"url": "http://inventree.local:8000"},
    }
    return parse_settings(data, ENV)


def said(ts: str, text: str = TAKE, *, user: str = SAM, **more: Any) -> dict[str, Any]:
    """A message as `conversations.history` lists it: no channel, and more fields than an event has."""
    return {"type": "message", "user": user, "text": text, "ts": ts, "team": WORKSPACE, **more}


class FakeHistory:
    """Slack's two listings: newest first, `oldest` and `latest` exclusive, pages linked by a cursor."""

    def __init__(self) -> None:
        self.channels: dict[str, list[dict[str, Any]]] = {}
        self.threads: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.errors: dict[str, Exception] = {}
        self.page_size: int | None = None

    def _page(self, messages: list[dict[str, Any]], kwargs: dict[str, Any], keep: str | None = None) -> dict[str, Any]:
        low, high = Decimal(kwargs["oldest"]), Decimal(kwargs["latest"])
        listed = [m for m in messages if low < Decimal(m["ts"]) < high or m["ts"] == keep]
        listed.sort(key=lambda m: Decimal(m["ts"]), reverse=keep is None)
        size = self.page_size or kwargs["limit"]
        start = int(kwargs["cursor"] or 0)
        more = start + size < len(listed)
        return {
            "ok": True,
            "messages": listed[start : start + size],
            "has_more": more,
            "response_metadata": {"next_cursor": str(start + size) if more else ""},
        }

    def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("history", kwargs))
        if kwargs["channel"] in self.errors:
            raise self.errors[kwargs["channel"]]
        return self._page(self.channels.get(kwargs["channel"], []), kwargs)

    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("replies", kwargs))
        if kwargs["ts"] in self.errors:
            raise self.errors[kwargs["ts"]]
        return self._page(self.threads.get(kwargs["ts"], []), kwargs, keep=kwargs["ts"])  # the root always comes


class FakeSlack:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posted.append(kwargs)
        return {"ok": True, "ts": f"1790009999.{len(self.posted):06d}"}

    def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("no clicks in these tests")


class Clock:
    def __init__(self, ts: str) -> None:
        self.at(ts)

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
def clock() -> Clock:
    return Clock(START)


@pytest.fixture
def history() -> FakeHistory:
    return FakeHistory()


@pytest.fixture
def slack() -> FakeSlack:
    return FakeSlack()


@pytest.fixture
def sweeper(store: Store, history: FakeHistory, clock: Clock) -> CatchUp:
    """A connector that made its first sweep at START: the channel is watched from there."""
    sweeper = CatchUp(settings().slack, store, history, clock)
    assert sweeper.sweep(Decimal(START)) == 0
    history.calls.clear()
    return sweeper


@pytest.fixture
def worker(store: Store, slack: FakeSlack, clock: Clock) -> Worker:
    return Worker(settings(), store, OWNER, slack, lambda: CATALOG, clock)


def events(path: Path) -> list[tuple[str, str, str | None]]:
    with sqlite3.connect(path) as raw:
        return list(raw.execute("SELECT event_key, status, outcome FROM slack_events ORDER BY id"))


def live(store: Store, ts: str, text: str = TAKE, *, user: str = SAM, thread_ts: str | None = None) -> None:
    """The event Slack delivers for a message."""
    event: dict[str, Any] = {"type": "message", "channel": CHANNEL, "user": user, "text": text, "ts": ts}
    if thread_ts:
        event["thread_ts"] = thread_ts
    body = {"type": "event_callback", "team_id": WORKSPACE, "api_app_id": APP, "event_id": f"Ev{ts}", "event": event}
    assert store.enqueue_event(f"Ev{ts}", "event", json.dumps(body))


# The first sweep


def test_the_first_sweep_of_a_channel_reads_nothing_and_later_ones_never_reach_behind_it(
    store: Store, history: FakeHistory, clock: Clock, path: Path
) -> None:
    history.channels[CHANNEL] = [said("1789990000.000100"), said("1790000500.000100")]
    sweeper = CatchUp(settings().slack, store, history, clock)
    assert sweeper.sweep(Decimal(START)) == 0
    assert history.calls == []  # a new installation does not answer what was posted before it
    assert store.catch_up_mark(WORKSPACE, CHANNEL) == (START, START)
    clock.at("1790001000.000000")
    assert sweeper.sweep() == 1
    assert [key for key, _, _ in events(path)] == [f"history:{CHANNEL}:1790000500.000100"]
    assert history.calls[0][1]["oldest"] == "1789741800.000000"  # listed for its thread roots, never stored


def test_a_channel_added_to_the_allowlist_later_starts_from_its_own_first_sweep(
    store: Store, history: FakeHistory, clock: Clock, sweeper: CatchUp, path: Path
) -> None:
    history.channels[OTHER] = [said("1790000500.000100")]
    clock.at("1790001000.000000")
    both = CatchUp(settings((CHANNEL, OTHER)).slack, store, history, clock)
    assert both.sweep() == 0
    assert [call[1]["channel"] for call in history.calls] == [CHANNEL]
    history.channels[OTHER].append(said("1790001500.000100"))
    clock.at("1790002000.000000")
    assert both.sweep() == 1
    assert [key for key, _, _ in events(path)] == [f"history:{OTHER}:1790001500.000100"]


def test_zero_hours_turns_catch_up_off(store: Store, history: FakeHistory, clock: Clock) -> None:
    sweeper = CatchUp(settings(hours=0).slack, store, history, clock)
    assert sweeper.sweep(Decimal(START)) == 0
    assert store.catch_up_mark(WORKSPACE, CHANNEL) is None
    assert history.calls == []


# What a sweep finds


def test_a_post_made_during_downtime_is_found_and_answered_as_late(
    sweeper: CatchUp, worker: Worker, store: Store, history: FakeHistory, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    history.channels[CHANNEL] = [said("1790000100.000100", blocks=[{"type": "rich_text"}])]
    clock.at("1790000130.000000")  # back after two minutes: not late by age
    assert sweeper.sweep(Decimal("1790000130.000000")) == 1
    worker.step()
    assert [reply["thread_ts"] for reply in slack.posted] == ["1790000100.000100"]
    assert "The post was read late" in json.dumps(slack.posted[0]["blocks"])
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, "1790000100.000100"))
    assert record is not None
    assert record.result is not None
    assert record.result["late"] is True
    assert [m["reasons"][0]["code"] for m in record.result["movements"]] == ["late_ingest"]
    assert store.catch_up_mark(WORKSPACE, CHANNEL) == (START, "1790000130.000000")


def test_a_post_slack_delivered_is_not_late_and_is_not_stored_again(
    sweeper: CatchUp, worker: Worker, store: Store, history: FakeHistory, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    live(store, "1790000100.000100")
    worker.step()
    history.channels[CHANNEL] = [said("1790000100.000100")]
    clock.at("1790000300.000000")
    assert sweeper.sweep() == 0
    worker.step()
    assert [key for key, _, _ in events(path)] == ["Ev1790000100.000100"]
    assert "The post was read late" not in json.dumps(slack.posted[0]["blocks"])


def test_a_message_whose_event_is_still_queued_is_placed_once_and_from_the_event(
    sweeper: CatchUp, worker: Worker, store: Store, history: FakeHistory, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    live(store, "1790000100.000100")  # stored by the instance before, not placed yet
    history.channels[CHANNEL] = [said("1790000100.000100")]
    clock.at("1790000130.000000")
    assert sweeper.sweep(Decimal("1790000130.000000")) == 1
    worker.step()
    assert len(slack.posted) == 1
    assert "The post was read late" not in json.dumps(slack.posted[0]["blocks"])
    assert store.post_messages(PostKey(WORKSPACE, CHANNEL, "1790000100.000100")) == ["1790000100.000100"]


def test_sweeping_twice_stores_a_message_once(sweeper: CatchUp, history: FakeHistory, clock: Clock, path: Path) -> None:
    history.channels[CHANNEL] = [said("1790000100.000100")]
    clock.at("1790001000.000000")
    assert sweeper.sweep() == 1
    clock.at("1790002000.000000")
    assert sweeper.sweep() == 0  # stored, though the worker has not placed it yet
    assert len(events(path)) == 1


def test_what_the_ingest_checks_drop_is_never_stored(
    sweeper: CatchUp, history: FakeHistory, clock: Clock, path: Path
) -> None:
    history.channels[CHANNEL] = [
        said("1790000100.000100", bot_id="B0STOCKBOT"),  # the connector's own reply, or another bot
        said("1790000101.000100", subtype="channel_join"),
        said("1790000102.000100", team="T0OTHER001"),  # a poster of another organization in a shared channel
        {"type": "message", "ts": "1790000103.000100", "text": "no user"},
    ]
    clock.at("1790001000.000000")
    assert sweeper.sweep() == 0
    assert events(path) == []
    assert sweeper.store.catch_up_mark(WORKSPACE, CHANNEL) == (START, "1790000880.000000")


def test_a_later_sweep_leaves_the_newest_messages_to_their_events(
    sweeper: CatchUp, history: FakeHistory, clock: Clock, path: Path
) -> None:
    history.channels[CHANNEL] = [said("1790000950.000100")]
    clock.at("1790001000.000000")
    assert sweeper.sweep() == 0
    assert history.calls[0][1]["latest"] == f"{1790001000 - SETTLE_SECONDS}.000000"
    clock.at("1790002000.000000")
    assert sweeper.sweep() == 1


def test_the_window_is_the_configured_hours(store: Store, history: FakeHistory, clock: Clock, path: Path) -> None:
    sweeper = CatchUp(settings(hours=1).slack, store, history, clock)
    sweeper.sweep(Decimal(START))
    history.channels[CHANNEL] = [said("1790000100.000100"), said("1790009000.000100")]
    clock.at("1790010000.000000")  # down for almost three hours
    assert sweeper.sweep(Decimal("1790010000.000000")) == 1
    assert history.calls[0][1]["oldest"] == "1790006400.000000"
    assert [key for key, _, _ in events(path)] == [f"history:{CHANNEL}:1790009000.000100"]


def test_every_page_is_read(sweeper: CatchUp, history: FakeHistory, clock: Clock, path: Path) -> None:
    history.channels[CHANNEL] = [said(f"17900001{n:02d}.000100", user=f"U0USER{n:04d}") for n in range(7)]
    history.page_size = 3
    clock.at("1790001000.000000")
    assert sweeper.sweep() == 7
    assert [call[1]["cursor"] for call in history.calls] == [None, "3", "6"]


# Threads


def test_a_reply_made_during_downtime_joins_its_post_even_under_an_old_root(
    sweeper: CatchUp, worker: Worker, store: Store, history: FakeHistory, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    root, reply = "1790000100.000100", "1790000900.000100"
    live(store, root, "for the Smith job")
    worker.step()
    history.channels[CHANNEL] = [said(root, "for the Smith job", thread_ts=root, reply_count=1, latest_reply=reply)]
    history.threads[root] = [history.channels[CHANNEL][0], said(reply, thread_ts=root, parent_user_id=SAM)]
    clock.at("1790001000.000000")
    assert sweeper.sweep(Decimal("1790001000.000000")) == 1
    assert [name for name, _ in history.calls] == ["history", "replies"]
    worker.step()
    assert store.post_messages(PostKey(WORKSPACE, CHANNEL, root)) == [root, reply]


def test_a_root_and_its_reply_both_missed_make_one_late_post(
    sweeper: CatchUp, worker: Worker, store: Store, history: FakeHistory, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    root, reply = "1790000100.000100", "1790000200.000100"
    top = said(root, "grabbed these for truck 1", thread_ts=root, reply_count=1, latest_reply=reply)
    history.channels[CHANNEL] = [top]
    history.threads[root] = [top, said(reply, "2 Disc 40 from main stock", thread_ts=root)]
    clock.at("1790000300.000000")
    assert sweeper.sweep(Decimal("1790000300.000000")) == 2
    worker.step()
    assert store.post_messages(PostKey(WORKSPACE, CHANNEL, root)) == [root, reply]
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, root))
    assert record is not None
    assert (record.result or {}).get("messages") == [root, reply]


def test_a_thread_that_has_not_moved_since_the_last_sweep_is_not_read_again(
    sweeper: CatchUp, history: FakeHistory, clock: Clock, path: Path
) -> None:
    root, reply = "1790000100.000100", "1790000200.000100"
    top = said(root, thread_ts=root, reply_count=1, latest_reply=reply)
    history.channels[CHANNEL] = [top]
    history.threads[root] = [top, said(reply, thread_ts=root, bot_id="B0STOCKBOT")]  # the dry-run reply
    clock.at("1790001000.000000")
    sweeper.sweep()
    assert [name for name, _ in history.calls] == ["history", "replies"]
    history.calls.clear()
    clock.at(f"{1790000200 + OVERLAP_SECONDS + SETTLE_SECONDS + 1000}.000000")
    sweeper.sweep()
    assert [name for name, _ in history.calls] == ["history"]
    top["latest_reply"] = "1790002000.000100"
    history.threads[root].append(said("1790002000.000100", "and 1 ONT 100 from main stock to truck 1", thread_ts=root))
    history.calls.clear()
    clock.at("1790003000.000000")
    assert sweeper.sweep() == 1
    assert [name for name, _ in history.calls] == ["history", "replies"]


def test_a_root_that_does_not_date_its_newest_reply_is_read(
    sweeper: CatchUp, history: FakeHistory, clock: Clock
) -> None:
    root = "1790000100.000100"
    history.channels[CHANNEL] = [said(root, thread_ts=root, reply_count=2)]
    clock.at("1790001000.000000")
    sweeper.sweep()
    clock.at("1790005000.000000")
    sweeper.sweep()
    assert [name for name, _ in history.calls] == ["history", "replies", "history", "replies"]


# Edits


def quantities(store: Store, ts: str) -> list[str]:
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, ts))
    assert record is not None
    assert record.result is not None
    return [item["quantity"] for movement in record.result["movements"] for item in movement["items"]]


THREE = "took 3 Disc 40 from main stock to truck 1"


def test_an_edit_made_during_downtime_is_read_in_a_post_that_still_waits(
    sweeper: CatchUp, worker: Worker, store: Store, history: FakeHistory, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    ts = "1790000100.000100"
    live(store, ts)
    clock.at("1790000101.000000")
    worker.step()  # placed; the connector stops before the dry run
    history.channels[CHANNEL] = [said(ts, THREE, edited={"user": SAM, "ts": "1790000150.000000"})]
    clock.at("1790000400.000000")
    assert sweeper.sweep(Decimal("1790000400.000000")) == 1
    worker.step()
    assert len(slack.posted) == 1
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, ts))
    assert record is not None
    assert record.result is not None
    assert record.outcome is DryRunOutcome.REPLIED
    assert quantities(store, ts) == ["3"]  # as edited, not as delivered
    assert record.result["late"] is True  # the text read was found in the history
    clock.at("1790002000.000000")
    assert sweeper.sweep() == 0  # and once only


def test_a_message_found_already_edited_is_read_as_it_is_now_and_not_withheld(
    sweeper: CatchUp, worker: Worker, store: Store, history: FakeHistory, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    ts = "1790000100.000100"
    history.channels[CHANNEL] = [said(ts, edited={"user": SAM, "ts": "1790000150.000000"})]
    clock.at("1790000400.000000")
    assert sweeper.sweep(Decimal("1790000400.000000")) == 1
    assert store.claim_events(OWNER)
    assert store.place_message(
        OWNER, PostKey(WORKSPACE, CHANNEL, ts), user_id=SAM, thread_ts=None, window_seconds=5, slack_event_id=1
    )
    assert sweeper.sweep(Decimal("1790000401.000000")) == 0  # the same edit as the stored event shows
    history.channels[CHANNEL][0]["edited"] = {"user": SAM, "ts": "1790000402.000000"}
    assert sweeper.sweep(Decimal("1790000410.000000")) == 1


def test_an_edit_slack_delivered_is_not_noted_again_from_the_history(
    sweeper: CatchUp, worker: Worker, store: Store, history: FakeHistory, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    ts = "1790000100.000100"
    live(store, ts)
    edit = {
        "type": "event_callback",
        "team_id": WORKSPACE,
        "api_app_id": APP,
        "event_id": "Ev1790000150.000000",
        "event": {
            "type": "message",
            "subtype": "message_changed",
            "channel": CHANNEL,
            "message": {**said(ts, THREE), "edited": {"user": SAM, "ts": "1790000150.000000"}},
            "previous_message": said(ts),
        },
    }
    assert store.enqueue_event("Ev1790000150.000000", "event", json.dumps(edit))
    clock.at("1790000101.000000")
    worker.step()  # placed, and its edit noted
    history.channels[CHANNEL] = [said(ts, THREE, edited={"user": SAM, "ts": "1790000150.000000"})]
    clock.at("1790000400.000000")
    assert sweeper.sweep(Decimal("1790000400.000000")) == 0  # the same edit: nothing to note again


def test_a_thread_root_deleted_during_downtime_withholds_its_waiting_post(
    sweeper: CatchUp, worker: Worker, store: Store, history: FakeHistory, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    root = "1790000100.000100"
    live(store, root)
    clock.at("1790000101.000000")
    worker.step()
    history.channels[CHANNEL] = [
        {
            "type": "message",
            "subtype": "tombstone",
            "user": "USLACKBOT",
            "text": "This message was deleted.",
            "ts": root,
        }
    ]
    clock.at("1790000400.000000")
    assert sweeper.sweep(Decimal("1790000400.000000")) == 1
    worker.step()
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, root))
    assert slack.posted == []
    assert record is not None
    assert record.outcome is DryRunOutcome.WITHHELD


def test_an_edit_found_in_the_history_of_a_post_that_has_its_dry_run_revises_it(
    sweeper: CatchUp, worker: Worker, store: Store, history: FakeHistory, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    ts = "1790000100.000100"
    live(store, ts)
    worker.step()
    clock.at("1790000120.000000")
    worker.step()
    assert len(slack.posted) == 1
    history.channels[CHANNEL] = [said(ts, THREE, edited={"user": SAM, "ts": "1790000150.000000"})]
    clock.at("1790001000.000000")
    assert sweeper.sweep() == 1  # the edit, as Slack would have sent it
    worker.step()
    assert len(slack.posted) == 2  # the edit alone starts a revision, read as edited
    assert slack.posted[1]["text"] == "Revised dry run: 1 movement, nothing changed."
    assert quantities(store, ts) == ["3"]
    assert store.post_has_revision(PostKey(WORKSPACE, CHANNEL, ts))
    clock.at("1790002000.000000")
    assert sweeper.sweep() == 0  # stored once
    worker.step()
    assert len(slack.posted) == 2


# Failures


def slack_error(code: str) -> SlackApiError:
    return SlackApiError("synthetic", {"ok": False, "error": code})  # type: ignore[no-untyped-call]


def test_a_channel_that_cannot_be_read_is_left_for_the_next_sweep_and_the_others_are_read(
    store: Store, history: FakeHistory, clock: Clock, path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sweeper = CatchUp(settings((CHANNEL, OTHER)).slack, store, history, clock)
    sweeper.sweep(Decimal(START))
    history.channels = {CHANNEL: [said("1790000100.000100")], OTHER: [said("1790000100.000200")]}
    history.errors[CHANNEL] = slack_error("not_in_channel")
    clock.at("1790001000.000000")
    with caplog.at_level(logging.WARNING):
        assert sweeper.sweep() == 1
    assert "Slack answered not_in_channel: invite the app to the channel" in caplog.text
    assert store.catch_up_mark(WORKSPACE, CHANNEL) == (START, START)
    assert store.catch_up_mark(WORKSPACE, OTHER) == (START, "1790000880.000000")
    history.errors.clear()
    clock.at("1790002000.000000")
    assert sweeper.sweep() == 1
    assert len(events(path)) == 2


@pytest.mark.parametrize(
    "error",
    [slack_error("ratelimited"), ConnectionError("synthetic"), TimeoutError(), EOFError(), sqlite3.OperationalError()],
)
def test_a_thread_that_cannot_be_read_keeps_the_channel_unswept(
    sweeper: CatchUp, history: FakeHistory, clock: Clock, path: Path, error: Exception
) -> None:
    root, reply = "1790000100.000100", "1790000200.000100"
    top = said(root, thread_ts=root, reply_count=1, latest_reply=reply)
    history.channels[CHANNEL] = [top]
    history.threads[root] = [top, said(reply, "and 1 ONT 100 from main stock to truck 1", thread_ts=root, user=LUIS)]
    history.errors[root] = error
    clock.at("1790001000.000000")
    assert sweeper.sweep() == 0
    assert events(path) == []  # not the root alone: it would be answered without its reply
    assert sweeper.store.catch_up_mark(WORKSPACE, CHANNEL) == (START, START)
    history.errors.clear()
    clock.at("1790050000.000000")  # long after: the thread is still read, because the mark did not move
    assert sweeper.sweep() == 2
    assert len(events(path)) == 2


def test_a_thread_slack_refuses_is_left_out_whole_and_the_rest_of_the_channel_is_stored(
    sweeper: CatchUp, history: FakeHistory, clock: Clock, path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bad, good, alone = "1790000100.000100", "1790000200.000100", "1790000300.000100"
    newest = "1790000400.000100"
    shown = said("1790000150.000100", thread_ts=bad, subtype="thread_broadcast")  # a reply the channel lists too
    history.channels[CHANNEL] = [
        said(bad, thread_ts=bad, reply_count=2, latest_reply=newest),
        shown,
        said(good, thread_ts=good, reply_count=1, latest_reply=newest, user=LUIS),
        said(alone, user="U0USER0003"),
    ]
    history.threads[good] = [said(newest, thread_ts=good, user=LUIS)]
    history.errors[bad] = slack_error("thread_not_found")
    clock.at("1790001000.000000")
    with caplog.at_level(logging.WARNING):
        sweeper.sweep()
    assert "a thread could not be read (thread_not_found)" in caplog.text
    assert [key.rsplit(":", 1)[1] for key, _, _ in events(path)] == [good, alone, newest]
    assert sweeper.store.catch_up_mark(WORKSPACE, CHANNEL) == (START, START)  # so the thread is read again
    history.errors.clear()
    history.threads[bad] = [shown]
    clock.at("1790002000.000000")
    sweeper.sweep()
    assert sorted(key.rsplit(":", 1)[1] for key, _, _ in events(path))[:2] == [bad, shown["ts"]]
    assert sweeper.store.catch_up_mark(WORKSPACE, CHANNEL) == (START, "1790001880.000000")


def test_a_rate_limit_ends_the_reading_of_threads_for_this_sweep(
    sweeper: CatchUp, history: FakeHistory, clock: Clock, path: Path
) -> None:
    roots = ["1790000100.000100", "1790000200.000100"]
    history.channels[CHANNEL] = [
        *(said(r, thread_ts=r, reply_count=1, latest_reply="1790000300.000100") for r in roots),
        said("1790000250.000100", user=LUIS),
    ]
    history.errors[roots[1]] = slack_error("ratelimited")  # newest first: this thread is read first
    clock.at("1790001000.000000")
    assert sweeper.sweep() == 1
    assert [name for name, _ in history.calls] == ["history", "replies"]
    assert [key for key, _, _ in events(path)] == [f"history:{CHANNEL}:1790000250.000100"]
    assert sweeper.store.catch_up_mark(WORKSPACE, CHANNEL) == (START, START)


@pytest.mark.parametrize(
    "answer",
    [
        {"ok": True},
        {"ok": True, "messages": [{"type": "message", "text": "no ts"}]},
        {"ok": True, "messages": [{"type": "message", "ts": "yesterday"}]},
        {"ok": True, "messages": [{"type": "message", "ts": 1790000100.0001}]},
        {"ok": True, "messages": ["text"]},
        {"ok": True, "messages": [], "has_more": True},
    ],
)
def test_an_answer_not_in_slacks_form_never_marks_the_channel_as_swept(
    sweeper: CatchUp, history: FakeHistory, clock: Clock, path: Path, answer: dict[str, Any], monkeypatch: Any
) -> None:
    monkeypatch.setattr(history, "conversations_history", lambda **_: answer)
    clock.at("1790001000.000000")
    assert sweeper.sweep() == 0
    assert events(path) == []
    assert sweeper.store.catch_up_mark(WORKSPACE, CHANNEL) == (START, START)


def test_a_listing_that_never_ends_is_cut_off(
    sweeper: CatchUp, history: FakeHistory, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catchup, "MAX_PAGES", 3)
    history.channels[CHANNEL] = [said(f"17900001{n:02d}.000100", user=f"U0USER{n:04d}") for n in range(8)]
    history.page_size = 2
    clock.at("1790001000.000000")
    sweeper.sweep()
    assert len(history.calls) == 3
    assert sweeper.store.catch_up_mark(WORKSPACE, CHANNEL) == (START, START)


def test_nothing_of_a_message_is_logged(
    sweeper: CatchUp, history: FakeHistory, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    history.channels[CHANNEL] = [said("1790000100.000100", "took 2 Disc 40 for the Smith job")]
    clock.at("1790001000.000000")
    with caplog.at_level(logging.DEBUG):
        assert sweeper.sweep() == 1
    assert "Smith" not in caplog.text
    assert "Disc" not in caplog.text
    assert SAM not in caplog.text


# The mark


def test_a_mark_never_moves_back_and_keeps_its_start(store: Store, path: Path) -> None:
    store.mark_caught_up(WORKSPACE, CHANNEL, "1790000100.000000")
    store.mark_caught_up(WORKSPACE, CHANNEL, "1790000500.000000")
    store.mark_caught_up(WORKSPACE, CHANNEL, "1790000300.000000")
    assert store.catch_up_mark(WORKSPACE, CHANNEL) == ("1790000100.000000", "1790000500.000000")
    assert store.catch_up_mark(WORKSPACE, OTHER) is None
    with pytest.raises(ValueError, match="not a Slack timestamp"):
        store.mark_caught_up(WORKSPACE, CHANNEL, "1790000600")
    raw = sqlite3.connect(path, isolation_level=None)
    for statement, message in [
        ("UPDATE catch_up SET swept_to = '1790000200.000000'", "never moves back"),
        ("UPDATE catch_up SET since_ts = '1790000000.000000'", "keeps the point"),
        ("UPDATE catch_up SET channel_id = 'C0ACME009'", "keeps the point"),
        ("DELETE FROM catch_up", "never deleted"),
        (
            "INSERT OR REPLACE INTO catch_up VALUES ('T0ACME001', 'C0ACME001', '1.000000', '1.000000', 'now')",
            "one catch-up mark",
        ),
    ]:
        with pytest.raises(sqlite3.IntegrityError, match=message):
            raw.execute(statement)
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("INSERT INTO catch_up VALUES ('T0ACME001', 'C0ACME002', 'soon', '1.000000', 'now')")
    raw.close()


def test_event_ids_from_the_history_cannot_be_taken_for_slacks() -> None:
    assert is_catch_up(f"history:{CHANNEL}:1790000100.000100")
    assert not is_catch_up("Ev0ACME0001")


# Found in review


def test_a_reply_under_a_root_from_before_the_first_sweep_is_found_and_the_root_is_not(
    sweeper: CatchUp, history: FakeHistory, clock: Clock, path: Path
) -> None:
    root, reply = "1789999000.000100", "1790000500.000100"
    top = said(root, "old news", thread_ts=root, reply_count=1, latest_reply=reply)
    history.channels[CHANNEL] = [top]
    history.threads[root] = [top, said("1789999500.000100", thread_ts=root), said(reply, thread_ts=root)]
    clock.at("1790001000.000000")
    assert sweeper.sweep() == 1
    assert [key for key, _, _ in events(path)] == [f"history:{CHANNEL}:{reply}"]


def test_a_thread_whose_newest_reply_is_just_behind_the_last_sweep_is_read_once_more(
    sweeper: CatchUp, history: FakeHistory, clock: Clock
) -> None:
    root = "1790000100.000100"
    top = said(root, thread_ts=root, reply_count=1, latest_reply="1790000600.000100")
    history.channels[CHANNEL] = [top]
    history.threads[root] = [top]
    clock.at("1790001000.000000")
    sweeper.sweep()  # ends at ...880
    history.calls.clear()
    clock.at("1790002000.000000")
    sweeper.sweep()  # the reply is 280 s behind the mark: within the overlap
    assert [name for name, _ in history.calls] == ["history", "replies"]
    history.calls.clear()
    clock.at("1790003000.000000")
    sweeper.sweep()
    assert [name for name, _ in history.calls] == ["history"]


def test_a_sweep_that_would_end_where_the_last_one_did_reads_nothing(
    sweeper: CatchUp, history: FakeHistory, clock: Clock
) -> None:
    clock.at("1790000060.000000")
    assert sweeper.sweep() == 0  # two minutes ago is before the first sweep
    assert history.calls == []


def test_an_edit_is_not_missed_when_the_event_from_before_it_is_still_queued(
    sweeper: CatchUp, worker: Worker, store: Store, history: FakeHistory, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    ts = "1790000100.000100"
    live(store, ts)  # stored by the instance before, not placed; then the message was edited
    history.channels[CHANNEL] = [said(ts, THREE, edited={"user": SAM, "ts": "1790000150.000000"})]
    clock.at("1790000400.000000")
    assert sweeper.sweep(Decimal("1790000400.000000")) == 1
    worker.step()
    assert len(slack.posted) == 1
    assert quantities(store, ts) == ["3"]  # placed from the event before the edit, read as edited
    assert [outcome for _, _, outcome in events(path)] == [None, "revision_noted"]


def test_a_post_is_late_when_any_of_its_messages_came_from_the_history(
    sweeper: CatchUp, worker: Worker, store: Store, history: FakeHistory, slack: FakeSlack, clock: Clock
) -> None:
    root, reply = "1790000100.000100", "1790000200.000100"
    history.channels[CHANNEL] = [said(root, "grabbed these for truck 1")]
    clock.at("1790000201.000000")
    assert sweeper.sweep(Decimal("1790000150.000000")) == 1
    live(store, reply, "2 Disc 40 from main stock", thread_ts=root)
    worker.step()
    read = worker._messages(PostKey(WORKSPACE, CHANNEL, root), 0)
    assert not isinstance(read, str)
    assert [message.ts for message in read[0]] == [root, reply]
    assert read[2] is True


def test_a_stop_ends_a_sweep_between_channels_and_between_threads(
    store: Store, history: FakeHistory, clock: Clock, path: Path
) -> None:
    stopped: list[bool] = []
    sweeper = CatchUp(settings((CHANNEL, OTHER)).slack, store, history, clock, lambda: bool(stopped))
    sweeper.sweep(Decimal(START))
    roots = ["1790000100.000100", "1790000200.000100"]
    history.channels[CHANNEL] = [said(r, thread_ts=r, reply_count=1, latest_reply="1790000300.000100") for r in roots]
    real = history.conversations_replies

    def replies(**kwargs: Any) -> dict[str, Any]:
        stopped.append(True)
        return real(**kwargs)

    history.conversations_replies = replies  # type: ignore[method-assign]
    clock.at("1790001000.000000")
    sweeper.sweep()
    assert [name for name, _ in history.calls] == ["history", "replies"]  # not the second thread, nor OTHER
    assert store.catch_up_mark(WORKSPACE, CHANNEL) == (START, START)
    assert events(path) == []  # half a channel is not stored


# A sweep and the worker at the same time


def stepping(history: FakeHistory, worker: Worker) -> None:
    """The worker runs on another thread: here it takes a step before every call a sweep makes to Slack."""
    real_history, real_replies = history.conversations_history, history.conversations_replies

    def listed(**kwargs: Any) -> dict[str, Any]:
        worker.step()
        return real_history(**kwargs)

    def replies(**kwargs: Any) -> dict[str, Any]:
        worker.step()
        return real_replies(**kwargs)

    history.conversations_history = listed  # type: ignore[method-assign]
    history.conversations_replies = replies  # type: ignore[method-assign]


def test_a_root_is_not_answered_while_its_replies_are_still_being_read(
    sweeper: CatchUp, worker: Worker, store: Store, history: FakeHistory, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    root, reply = "1790000100.000100", "1790000200.000100"
    top = said(root, "grabbed these for truck 1", thread_ts=root, reply_count=1, latest_reply=reply)
    history.channels[CHANNEL] = [top]
    history.threads[root] = [top, said(reply, "2 Disc 40 from main stock", thread_ts=root)]
    clock.at("1790001000.000000")
    stepping(history, worker)
    assert sweeper.sweep() == 2
    assert [name for name, _ in history.calls] == ["history", "replies"]
    assert slack.posted == []
    assert [status for _, status, _ in events(path)] == ["pending", "pending"]
    worker.step()
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, root))
    assert record is not None
    assert (record.result or {}).get("messages") == [root, reply]


def test_siblings_on_two_pages_of_the_history_make_one_post(
    sweeper: CatchUp, worker: Worker, store: Store, history: FakeHistory, slack: FakeSlack, clock: Clock, path: Path
) -> None:
    first, second = "1790000100.000100", "1790000103.000100"
    history.channels[CHANNEL] = [said(first, "2 Disc 40 from main stock"), said(second, "to truck 1")]
    history.page_size = 1
    clock.at("1790001000.000000")
    stepping(history, worker)
    assert sweeper.sweep() == 2
    assert [call[1]["cursor"] for call in history.calls] == [None, "1"]
    assert slack.posted == []
    worker.step()
    assert store.post_messages(PostKey(WORKSPACE, CHANNEL, first)) == [first, second]
    with pytest.raises(KeyError):
        store.post_messages(PostKey(WORKSPACE, CHANNEL, second))  # not a post of its own
    record = store.dry_run(PostKey(WORKSPACE, CHANNEL, first))
    assert record is not None
    assert (record.result or {}).get("messages") == [first, second]


def test_a_sweep_stores_its_events_oldest_first(
    sweeper: CatchUp, history: FakeHistory, clock: Clock, path: Path
) -> None:
    stamps = [f"17900001{n:02d}.000100" for n in range(3)]
    history.channels[CHANNEL] = [said(ts, user=f"U0USER{n:04d}") for n, ts in enumerate(stamps)]
    clock.at("1790001000.000000")
    assert sweeper.sweep() == 3
    assert [key for key, _, _ in events(path)] == [f"history:{CHANNEL}:{ts}" for ts in stamps]


def test_a_sweep_is_stored_whole_or_not_at_all(store: Store, path: Path) -> None:
    store.mark_caught_up(WORKSPACE, CHANNEL, START)
    assert store.enqueue_event("history:a", "event", "{}")
    assert store.store_sweep(WORKSPACE, CHANNEL, "1790000500.000000", [("history:a", "{}"), ("history:b", "{}")]) == 1
    assert store.catch_up_mark(WORKSPACE, CHANNEL) == (START, "1790000500.000000")
    with pytest.raises(ValueError, match="not a Slack timestamp"):
        store.store_sweep(WORKSPACE, CHANNEL, "soon", [("history:c", "{}")])
    with pytest.raises(sqlite3.Error):
        store.store_sweep(WORKSPACE, CHANNEL, "1790000900.000000", [("history:d", "{}"), (None, "{}")])  # type: ignore[list-item]
    assert [key for key, _, _ in events(path)] == ["history:a", "history:b"]
    assert store.catch_up_mark(WORKSPACE, CHANNEL) == (START, "1790000500.000000")


def test_a_mark_is_two_slack_timestamps_in_order(store: Store, path: Path) -> None:
    raw = sqlite3.connect(path, isolation_level=None)
    for since, swept in [("9x.000000", "9.000000"), ("5.000000", "1.000000"), ("5.000000", "6.00000x")]:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("INSERT INTO catch_up VALUES ('T0ACME001', 'C0ACME001', ?, ?, 'now')", (since, swept))
    raw.close()
