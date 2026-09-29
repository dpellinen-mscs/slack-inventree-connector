"""Grouping in the store: a message belongs to exactly one post, whatever order it arrives in (design section 4)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from stockthread.core.model import PostKey
from stockthread.store import LockLost, Store
from stockthread.store import db as store_db

OWNER = "instance-a"
WINDOW = 5


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "stockthread.db"


@pytest.fixture
def store(path: Path) -> Iterator[Store]:
    opened = Store.open(path)
    opened.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    yield opened
    opened.close()


def key(ts: str, channel: str = "C0STOCK001") -> PostKey:
    return PostKey("T0ACME0001", channel, ts)


def place(store: Store, ts: str, user: str = "U0SAM0001", thread_ts: str | None = None) -> tuple[PostKey, bool]:
    return store.place_message(OWNER, key(ts), user_id=user, thread_ts=thread_ts, window_seconds=WINDOW)


def test_a_photo_and_its_sibling_caption_are_one_post(store: Store) -> None:
    assert place(store, "1790000000.000100") == (key("1790000000.000100"), True)
    assert place(store, "1790000003.000000") == (key("1790000000.000100"), False)
    assert store.post_messages(key("1790000000.000100")) == ["1790000000.000100", "1790000003.000000"]


def test_a_redelivered_message_is_placed_once(store: Store, path: Path) -> None:
    place(store, "1790000000.000100")
    assert place(store, "1790000000.000100") == (key("1790000000.000100"), False)
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT count(*) FROM post_messages").fetchone() == (1,)


def test_the_window_is_exact_to_the_microsecond(store: Store) -> None:
    place(store, "1790000000.000100")
    assert place(store, "1790000005.000100")[0] == key("1790000000.000100")
    assert place(store, "1790000010.000100")[0] == key("1790000000.000100")  # chained through the second
    assert place(store, "1790000015.000101")[1] is True  # 5.000001 s after the last one


def test_another_user_or_channel_starts_its_own_post(store: Store) -> None:
    place(store, "1790000000.000100")
    assert place(store, "1790000001.000000", user="U0DANA0004")[1] is True
    other = store.place_message(
        OWNER, key("1790000002.000000", "C0OTHER001"), user_id="U0SAM0001", thread_ts=None, window_seconds=WINDOW
    )
    assert other == (key("1790000002.000000", "C0OTHER001"), True)


def test_after_the_reply_no_sibling_joins_but_a_thread_reply_does(store: Store) -> None:
    place(store, "1790000000.000100")
    store.close_siblings(OWNER, key("1790000000.000100"))
    store.close_siblings(OWNER, key("1790000000.000100"))  # idempotent
    assert place(store, "1790000002.000000")[1] is True
    reply = place(store, "1790000100.000000", user="U0DANA0004", thread_ts="1790000000.000100")
    assert reply == (key("1790000000.000100"), False)


def test_a_reply_before_its_root_keys_the_post_on_the_root(store: Store) -> None:
    assert place(store, "1790000100.000000", user="U0DANA0004", thread_ts="1790000000.000100") == (
        key("1790000000.000100"),
        True,
    )
    assert place(store, "1790000000.000100") == (key("1790000000.000100"), False)
    assert store.post_messages(key("1790000000.000100")) == ["1790000000.000100", "1790000100.000000"]


def test_grouping_needs_the_lock(store: Store) -> None:
    with pytest.raises(LockLost):
        store.place_message("instance-b", key("1.000000"), user_id="U0SAM0001", thread_ts=None, window_seconds=WINDOW)
    place(store, "1.000000")
    with pytest.raises(LockLost):
        store.close_siblings("instance-b", key("1.000000"))


@pytest.mark.parametrize("bad", ["", "abc", "NaN", "-1", " 100.000000", "100.00", "1_790_000_000.000000"])
def test_a_timestamp_must_be_a_number(store: Store, bad: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - the key or the timestamp check, whichever sees it first
        place(store, bad)
    with pytest.raises(ValueError):  # noqa: PT011
        place(store, "1.000000", thread_ts=bad or "x")


def test_an_empty_thread_is_no_thread(store: Store) -> None:
    assert place(store, "1.000000", thread_ts="") == (key("1.000000"), True)
    assert place(store, "2.000000", thread_ts="") == (key("1.000000"), False)


def test_the_database_keeps_a_message_in_its_post(store: Store, path: Path) -> None:
    place(store, "1.000000")
    place(store, "50.000000", user="U0DANA0004")
    raw = sqlite3.connect(path, isolation_level=None)
    for statement in (
        "UPDATE post_messages SET post_id = 2",
        "DELETE FROM post_messages",
        "INSERT OR REPLACE INTO post_messages (post_id, workspace_id, channel_id, ts, user_id, added_at)"
        " VALUES (2, 'T0ACME0001', 'C0STOCK001', '1.000000', 'U0SAM0001', 'now')",
        "INSERT INTO post_messages (post_id, workspace_id, channel_id, ts, user_id, added_at)"
        " VALUES (1, 'T0ACME0001', 'C0OTHER001', '2.000000', 'U0SAM0001', 'now')",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(statement)
    raw.execute("UPDATE posts SET siblings_closed_at = 'now' WHERE id = 1")
    for statement in (
        "UPDATE posts SET siblings_closed_at = 'later' WHERE id = 1",
        "UPDATE posts SET siblings_closed_at = NULL WHERE id = 1",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(statement)
    raw.close()


def test_no_message_text_is_stored(path: Path, store: Store) -> None:
    place(store, "1.000000")
    with sqlite3.connect(path) as raw:
        columns = {row[1] for row in raw.execute("PRAGMA table_info(post_messages)")}
    assert columns == {"id", "post_id", "workspace_id", "channel_id", "ts", "user_id", "thread_ts", "added_at"}


def test_a_version_2_file_upgrades_with_its_posts_open(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = store_db._migrations()
    monkeypatch.setattr(store_db, "_migrations", lambda: real[:2])
    old = Store.open(path)
    old.create_post(key("1.000000"))
    old.close()
    monkeypatch.setattr(store_db, "_migrations", lambda: real)
    upgraded = Store.open(path)
    upgraded.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    assert upgraded.schema_version == 3
    assert place(upgraded, "1.000000") == (key("1.000000"), False)  # the old post's first message joins it
    assert place(upgraded, "3.000000") == (key("1.000000"), False)
    upgraded.close()
