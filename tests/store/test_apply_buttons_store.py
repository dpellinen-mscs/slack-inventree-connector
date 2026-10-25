"""Apply buttons in the store: a dry-run reply posted with one stores it with the reply (migration 0022), so it is taken
down once its plan is closed (`Worker.take_down_buttons`)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from stockthread.core.model import Operation, PostKey
from stockthread.store import ApplyButton, DryRunOutcome, NewPlan, Store

OWNER = "instance-a"
POST = PostKey("T0ACME0001", "C0STOCK001", "1790000000.000100")
REPLY, REVISED = "1790000020.000100", "1790000100.000109"
RESULT = {"movements": [{"part": "Disc 40"}]}
PLAN = NewPlan("0" * 64, (Operation.TRANSFER,))


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "stockthread.db"


@pytest.fixture
def store(path: Path) -> Iterator[Store]:
    opened = Store.open(path)
    opened.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    opened.place_message(OWNER, POST, user_id="U0SAM0001", thread_ts=None, window_seconds=5)
    yield opened
    opened.close()


def revise(store: Store, *, apply: bool) -> None:
    """A thread reply joins the post, and revision 2 replies with a plan of its own."""
    store.place_message(OWNER, PostKey(POST.workspace_id, POST.channel_id, "1790000100.000100"), user_id="U0SAM0001",
                        thread_ts=POST.ts, window_seconds=5)  # fmt: skip
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REVISED, result=RESULT, plan=PLAN, revision=2, apply=apply)


def test_a_reply_with_an_apply_button_is_stored_with_it_and_listed_until_taken_down(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, result=RESULT, plan=PLAN, apply=True)
    revise(store, apply=True)
    first, second = store.apply_buttons_shown(limit=10)
    assert (first.post, first.revision, first.message_ts) == (POST, 1, REPLY)
    assert (second.post, second.revision, second.message_ts) == (POST, 2, REVISED)
    assert store.apply_failed(OWNER, first.id) == 1
    assert store.apply_buttons_shown(limit=10) == [second, first]  # fewest failed tries first
    assert store.apply_buttons_shown(limit=1) == [second]
    store.apply_taken_down(OWNER, second.id, removed=True)
    store.apply_taken_down(OWNER, first.id, removed=False)
    assert store.apply_buttons_shown(limit=10) == []
    store.apply_taken_down(OWNER, first.id, removed=True)  # once: a second call changes nothing
    assert store.apply_failed(OWNER, first.id) == 1
    rows = store._db.execute("SELECT revision, buttons, buttons_tries FROM apply_buttons ORDER BY id").fetchall()
    assert rows == [(1, "left", 1), (2, "removed", 0)]
    events = [row[0] for row in store._db.execute("SELECT event FROM audit_log WHERE event LIKE 'apply_%'")]
    assert events == ["apply_removed", "apply_left"]


def test_a_reply_without_an_apply_button_stores_none(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, result=RESULT, plan=PLAN)
    assert store.apply_buttons_shown(limit=10) == []


@pytest.mark.parametrize(
    ("outcome", "reply_ts", "plan"),
    [
        (DryRunOutcome.REPLIED, REPLY, None),  # no plan to apply
        (DryRunOutcome.REPLIED, None, PLAN),  # nothing to take it down from
        (DryRunOutcome.SILENT, None, None),
    ],
)
def test_only_a_posted_reply_that_made_a_plan_carries_an_apply_button(
    store: Store, outcome: DryRunOutcome, reply_ts: str | None, plan: NewPlan | None
) -> None:
    with pytest.raises(ValueError, match="Apply button"):
        store.finish_dry_run(OWNER, POST, outcome, reply_ts, result=RESULT, plan=plan, apply=True)
    assert store.dry_run_history(POST) == []  # nothing recorded


def test_taking_down_needs_the_lock(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, result=RESULT, plan=PLAN, apply=True)
    [button] = store.apply_buttons_shown(limit=10)
    store.release_lock(OWNER)
    for call in (lambda: store.apply_taken_down(OWNER, button.id, removed=True),
                 lambda: store.apply_failed(OWNER, button.id)):  # fmt: skip
        with pytest.raises(Exception, match="lock"):
            call()
    assert store.apply_buttons_shown(limit=10) == [ApplyButton(button.id, POST, 1, REPLY)]


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("UPDATE apply_buttons SET buttons = 'shown'", "taken down once"),
        ("UPDATE apply_buttons SET buttons = 'left'", "taken down once"),
        ("UPDATE apply_buttons SET message_ts = '1790000099.000100'", "stays on the reply"),
        ("UPDATE apply_buttons SET revision = 2", "stays on the reply"),
        ("UPDATE apply_buttons SET post_id = 2", "stays on the reply"),
        ("UPDATE apply_buttons SET id = 7", "stays on the reply"),
        ("DELETE FROM apply_buttons", "never deleted"),
        ("INSERT OR REPLACE INTO apply_buttons (post_id, revision, message_ts) VALUES (1, 1, 'x')", "UPDATE only"),
    ],
)
def test_triggers_keep_apply_buttons(store: Store, path: Path, statement: str, message: str) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, result=RESULT, plan=PLAN, apply=True)
    [button] = store.apply_buttons_shown(limit=10)
    store.apply_taken_down(OWNER, button.id, removed=True)
    with sqlite3.connect(path) as raw, pytest.raises(sqlite3.DatabaseError, match=message):
        raw.execute(statement)
