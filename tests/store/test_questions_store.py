"""Questions in the store: a reply's question and its buttons are stored with the reply (migration 0010)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from stockthread.core.model import PostKey
from stockthread.store import DryRunOutcome, NewQuestion, QuestionStatus, Store, new_button_id

OWNER = "instance-a"
POST = PostKey("T0ACME0001", "C0STOCK001", "1790000000.000100")
REPLY = "1790000020.000100"
YES, NO = "a" * 22, "b" * 22


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


def confirm(yes: str = YES, no: str = NO, *, approver_only: bool = False) -> NewQuestion:
    return NewQuestion(
        {"ask": "confirm", "reason": "late_ingest", "movement": 0},
        approver_only,
        [(yes, {"kind": "yes"}), (no, {"kind": "no"})],
    )


def reply_again(store: Store, ts: str, revision: int, outcome: DryRunOutcome, **kwargs: object) -> None:
    """A thread reply joins the post, and revision `revision` is recorded."""
    store.place_message(OWNER, PostKey(POST.workspace_id, POST.channel_id, ts), user_id="U0SAM0001",
                        thread_ts=POST.ts, window_seconds=5)  # fmt: skip
    posted = outcome in (DryRunOutcome.REPLIED, DryRunOutcome.RETRACTED)
    store.finish_dry_run(OWNER, POST, outcome, f"{ts[:-1]}9" if posted else None, revision=revision, **kwargs)  # type: ignore[arg-type]


def test_a_reply_stores_its_question_and_buttons_with_its_record(store: Store, path: Path) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm(approver_only=True))
    [item] = store.questions(POST)
    assert (item.post, item.revision, item.channel_id, item.message_ts) == (POST, 1, POST.channel_id, REPLY)
    assert item.question == {"ask": "confirm", "reason": "late_ingest", "movement": 0}
    assert item.approver_only is True
    assert item.status is QuestionStatus.OPEN
    yes = store.button(YES)
    assert yes is not None
    assert (yes.item, yes.position, yes.choice) == (item, 0, {"kind": "yes"})
    no = store.button(NO)
    assert no is not None
    assert (no.position, no.choice) == (1, {"kind": "no"})
    assert store.button("c" * 22) is None
    with sqlite3.connect(path) as raw:
        events = [row[0] for row in raw.execute("SELECT event FROM audit_log ORDER BY id")]
    assert events[-2:] == ["dry_run_replied", "question_asked"]


def test_a_question_without_buttons_is_answered_in_the_thread(store: Store) -> None:
    store.finish_dry_run(
        OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=NewQuestion({"ask": "quantity"}, False, [])
    )
    [item] = store.questions(POST)
    assert item.question == {"ask": "quantity"}


def test_a_reply_without_a_usable_timestamp_keeps_its_question_unclickable(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, None, question=confirm())
    [item] = store.questions(POST)
    assert item.message_ts is None


@pytest.mark.parametrize("outcome", [DryRunOutcome.SILENT, DryRunOutcome.WITHHELD, DryRunOutcome.GAVE_UP])
def test_only_a_reply_asks(store: Store, outcome: DryRunOutcome) -> None:
    with pytest.raises(ValueError, match="only a reply asks"):
        store.finish_dry_run(OWNER, POST, outcome, question=confirm())
    assert store.dry_run(POST) is None  # nothing recorded


@pytest.mark.parametrize(("yes", "no"), [(YES, YES), ("short", NO)])
def test_every_button_has_its_own_random_id(store: Store, yes: str, no: str) -> None:
    with pytest.raises(ValueError, match="own random id"):
        store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm(yes, no))
    assert store.dry_run(POST) is None


def test_a_button_id_is_never_used_twice(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    with pytest.raises(sqlite3.DatabaseError, match="a button is stored once"):
        reply_again(store, "1790000100.000100", 2, DryRunOutcome.REPLIED, question=confirm(YES, "c" * 22))
    assert len(store.dry_run_history(POST)) == 1  # the whole revision was rolled back
    assert store.questions(POST)[0].status is QuestionStatus.OPEN


def test_new_button_ids_are_random_and_long_enough() -> None:
    ids = {new_button_id() for _ in range(1000)}
    assert len(ids) == 1000
    assert all(len(button_id) == 22 for button_id in ids)  # 16 random bytes


@pytest.mark.parametrize("outcome", [DryRunOutcome.REPLIED, DryRunOutcome.RETRACTED])
def test_a_reply_or_notice_of_a_revision_makes_the_open_question_stale(
    store: Store, path: Path, outcome: DryRunOutcome
) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    reply_again(store, "1790000100.000100", 2, outcome)
    [item] = store.questions(POST)
    assert item.status is QuestionStatus.STALE
    button = store.button(YES)
    assert button is not None
    assert button.item.status is QuestionStatus.STALE
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT closed_at IS NOT NULL FROM review_items").fetchone() == (1,)
        assert raw.execute("SELECT count(*) FROM audit_log WHERE event = 'question_stale'").fetchone() == (1,)


@pytest.mark.parametrize("outcome", [DryRunOutcome.UNCHANGED, DryRunOutcome.SILENT, DryRunOutcome.GAVE_UP])
def test_a_revision_that_posts_nothing_leaves_the_question_open(store: Store, outcome: DryRunOutcome) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    reply_again(store, "1790000100.000100", 2, outcome)
    assert [item.status for item in store.questions(POST)] == [QuestionStatus.OPEN]


def test_a_revised_reply_asks_its_own_question(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    reply_again(store, "1790000100.000100", 2, DryRunOutcome.REPLIED, question=confirm("c" * 22, "d" * 22))
    first, second = store.questions(POST)
    assert (first.revision, first.status) == (1, QuestionStatus.STALE)
    assert (second.revision, second.status, second.message_ts) == (2, QuestionStatus.OPEN, "1790000100.000109")
    button = store.button("c" * 22)
    assert button is not None
    assert button.item == second


def test_asking_needs_the_lock(store: Store) -> None:
    store.release_lock(OWNER)
    with pytest.raises(Exception, match="lock"):
        store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("UPDATE review_items SET question = '{}'", "a question never changes"),
        ("UPDATE review_items SET message_ts = '1790000099.000100'", "a question never changes"),
        ("UPDATE review_items SET approver_only = 1", "a question never changes"),
        ("UPDATE review_items SET id = 7", "a question never changes"),
        ("UPDATE review_items SET rowid = 7", "a question never changes"),
        ("UPDATE review_items SET status = 'open', closed_at = NULL", "answered or goes stale once"),
        ("UPDATE review_items SET status = 'answered', answer = '{}', answered_by = 'U0SAM0001'", "once"),
        ("DELETE FROM review_items", "never deleted"),
        ('UPDATE interactions SET choice = \'{"kind":"no"}\'', "a button never changes"),
        ("DELETE FROM interactions", "buttons are never deleted"),
    ],
)
def test_triggers_keep_questions_and_buttons(store: Store, path: Path, statement: str, message: str) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    reply_again(store, "1790000100.000100", 2, DryRunOutcome.RETRACTED)  # stale: closed once already
    with sqlite3.connect(path) as raw, pytest.raises(sqlite3.DatabaseError, match=message):
        raw.execute(statement)


INSERT_ITEM = (
    "INSERT {verb} INTO review_items (id, post_id, revision, question, approver_only, channel_id, status, created_at)"
    " VALUES ({id}, 1, {revision}, '{{}}', 0, '{channel}', 'open', 'now')"
)


def test_a_post_has_one_open_question(store: Store, path: Path) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    with sqlite3.connect(path) as raw, pytest.raises(sqlite3.DatabaseError, match="one open question"):
        raw.execute(INSERT_ITEM.format(verb="", id="NULL", revision=2, channel=POST.channel_id))


@pytest.mark.parametrize(
    ("verb", "item_id", "revision"), [("", "NULL", 1), ("OR REPLACE", "1", 3), ("OR REPLACE", "NULL", 1)]
)
def test_a_question_is_asked_once_per_dry_run_and_never_replaced(
    store: Store, path: Path, verb: str, item_id: str, revision: int
) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    reply_again(store, "1790000100.000100", 2, DryRunOutcome.RETRACTED)  # stale: no open question is in the way
    with sqlite3.connect(path) as raw, pytest.raises(sqlite3.DatabaseError, match="asked once per dry run"):
        raw.execute(INSERT_ITEM.format(verb=verb, id=item_id, revision=revision, channel=POST.channel_id))


def test_a_question_is_asked_in_the_channel_of_its_post(store: Store, path: Path) -> None:
    with sqlite3.connect(path) as raw, pytest.raises(sqlite3.DatabaseError, match="channel of its post"):
        raw.execute(INSERT_ITEM.format(verb="", id="NULL", revision=1, channel="C0OTHER001"))


def test_a_button_belongs_to_an_open_question(store: Store, path: Path) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    reply_again(store, "1790000100.000100", 2, DryRunOutcome.RETRACTED)
    with sqlite3.connect(path) as raw, pytest.raises(sqlite3.DatabaseError, match="open question"):
        raw.execute("INSERT INTO interactions VALUES ('cccccccccccccccccccccc', 1, 2, '{}')")


@pytest.mark.parametrize(
    "columns",
    [
        "status = 'stale', answer = '{}', closed_at = 'now'",
        "status = 'stale', answered_by = 'U0SAM0001', closed_at = 'now'",
    ],
)
def test_an_answer_and_who_gave_it_come_together_and_only_with_answered(store: Store, path: Path, columns: str) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    with sqlite3.connect(path) as raw, pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        raw.execute(f"UPDATE review_items SET {columns}")  # noqa: S608


def test_an_open_question_closes_once_with_an_answer(store: Store, path: Path) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    with sqlite3.connect(path) as raw:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            raw.execute("UPDATE review_items SET status = 'answered', closed_at = 'now'")  # no answer, nobody
        raw.execute(
            "UPDATE review_items SET status = 'answered', answer = '{}', answered_by = 'U0SAM0001', closed_at = 'now'"
        )
        with pytest.raises(sqlite3.DatabaseError, match="once"):
            raw.execute("UPDATE review_items SET status = 'stale'")


@pytest.mark.parametrize("values", ["('aaaaaaaaaaaaaaaaaaaaaa', 1, 5, '{}')", "('cccccccccccccccccccccc', 1, 0, '{}')"])
def test_a_button_is_never_replaced(store: Store, path: Path, values: str) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    with sqlite3.connect(path) as raw, pytest.raises(sqlite3.DatabaseError, match="a button is stored once"):
        raw.execute(f"INSERT OR REPLACE INTO interactions VALUES {values}")  # noqa: S608


def test_an_answer_is_recorded_once_with_who_gave_it(store: Store, path: Path) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    yes, no = store.button(YES), store.button(NO)
    assert yes is not None
    assert no is not None
    assert store.answer(yes.item.id) is None
    assert store.answer_question(OWNER, no, "U0SAM0001") is True
    assert store.answer(no.item.id) == ({"button": 1, "kind": "no"}, "U0SAM0001")
    assert store.answer_question(OWNER, yes, "U0LUIS0005") is False  # the first answer stands
    assert store.answer(no.item.id) == ({"button": 1, "kind": "no"}, "U0SAM0001")
    [item] = store.questions(POST)
    assert item.status is QuestionStatus.ANSWERED
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT count(*) FROM audit_log WHERE event = 'question_answered'").fetchone() == (1,)


def test_a_stale_question_takes_no_answer(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    yes = store.button(YES)
    assert yes is not None
    reply_again(store, "1790000100.000100", 2, DryRunOutcome.RETRACTED)
    assert store.answer_question(OWNER, yes, "U0SAM0001") is False
    assert store.answer(yes.item.id) is None


def test_answering_needs_the_lock(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    yes = store.button(YES)
    assert yes is not None
    store.release_lock(OWNER)
    with pytest.raises(Exception, match="lock"):
        store.answer_question(OWNER, yes, "U0SAM0001")


def test_the_poster_is_the_author_of_the_oldest_message(store: Store) -> None:
    reply = PostKey(POST.workspace_id, POST.channel_id, "1790000003.000000")
    store.place_message(OWNER, reply, user_id="U0LUIS0005", thread_ts=POST.ts, window_seconds=5)
    assert store.post_poster(POST) == "U0SAM0001"
    with pytest.raises(KeyError):
        store.post_poster(PostKey(POST.workspace_id, POST.channel_id, "1790000999.000100"))
