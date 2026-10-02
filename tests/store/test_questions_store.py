"""Questions in the store: a reply's question and its buttons are stored with the reply (migration 0010)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from stockthread.core.model import PostKey
from stockthread.store import Answered, DryRunOutcome, NewQuestion, QuestionStatus, Store, new_button_id

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


def confirm(yes: str = YES, no: str = NO, *, approver_only: bool = False, expires_from: str = POST.ts) -> NewQuestion:
    return NewQuestion(
        {"ask": "confirm", "reason": "late_ingest", "movement": 0},
        approver_only,
        [(yes, {"kind": "yes"}), (no, {"kind": "no"})],
        poster="U0SAM0001",
        expires_from=expires_from,
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
        OWNER,
        POST,
        DryRunOutcome.REPLIED,
        REPLY,
        question=NewQuestion({"ask": "quantity"}, False, [], poster="U0SAM0001", expires_from=POST.ts),
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
    reply_again(
        store,
        "1790000100.000100",
        2,
        DryRunOutcome.REPLIED,
        question=confirm("c" * 22, "d" * 22),
    )
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
    "INSERT {verb} INTO review_items (id, post_id, revision, question, approver_only, channel_id, status, created_at,"
    " poster_id, expires_from) VALUES ({id}, 1, {revision}, '{{}}', 0, '{channel}', 'open', 'now', 'U0SAM0001', '1')"
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
    assert store.answer_question(OWNER, no, "U0SAM0001") is Answered.ANSWERED
    assert store.answer(no.item.id) == ({"button": 1, "kind": "no"}, "U0SAM0001")
    assert store.answer_question(OWNER, yes, "U0LUIS0005") is Answered.NOT_OPEN  # the first answer stands
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
    assert store.answer_question(OWNER, yes, "U0SAM0001") is Answered.NOT_OPEN
    assert store.answer(yes.item.id) is None


def test_answering_needs_the_lock(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    yes = store.button(YES)
    assert yes is not None
    store.release_lock(OWNER)
    with pytest.raises(Exception, match="lock"):
        store.answer_question(OWNER, yes, "U0SAM0001")


def test_a_question_keeps_who_may_answer_it_and_when_its_plan_expires(store: Store, path: Path) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    [item] = store.questions(POST)
    assert (item.poster_id, item.expires_from) == ("U0SAM0001", POST.ts)
    with sqlite3.connect(path) as raw:
        for change in ("poster_id = 'U0LUIS0005'", "expires_from = '1790009999.000100'"):
            with pytest.raises(sqlite3.DatabaseError, match="a question never changes"):
                raw.execute(f"UPDATE review_items SET {change}")  # noqa: S608
    reply_again(store, "1790000100.000100", 2, DryRunOutcome.RETRACTED)
    with sqlite3.connect(path) as raw, pytest.raises(sqlite3.DatabaseError, match="knows its poster"):
        raw.execute(
            "INSERT INTO review_items (post_id, revision, question, approver_only, channel_id, status, created_at)"
            " VALUES (1, 3, '{}', 0, 'C0STOCK001', 'open', 'now')"
        )


@pytest.mark.parametrize("change", ["message", "edit", "later_reply", "later_withheld"])
def test_a_post_that_moved_on_from_its_question_takes_no_answer(store: Store, change: str) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    yes = store.button(YES)
    assert yes is not None
    later = PostKey(POST.workspace_id, POST.channel_id, "1790000100.000100")
    if change == "message":
        store.place_message(OWNER, later, user_id="U0SAM0001", thread_ts=POST.ts, window_seconds=5)
    elif change == "edit":
        store.note_revision(OWNER, POST)
    elif change == "later_reply":
        reply_again(store, later.ts, 2, DryRunOutcome.GAVE_UP)  # a revised reply that never reached Slack
    else:
        reply_again(store, later.ts, 2, DryRunOutcome.WITHHELD)
    assert store.answer_question(OWNER, yes, "U0SAM0001") is Answered.POST_CHANGED
    assert store.answer(yes.item.id) is None
    assert store.questions(POST)[0].status is QuestionStatus.OPEN


def test_an_unchanged_revision_keeps_the_question_answerable(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    reply_again(store, "1790000100.000100", 2, DryRunOutcome.UNCHANGED)
    yes = store.button(YES)
    assert yes is not None
    assert store.answer_question(OWNER, yes, "U0SAM0001") is Answered.ANSWERED


def test_a_button_of_another_question_answers_nothing(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    yes = store.button(YES)
    assert yes is not None
    forged = replace(yes, id="z" * 22)  # no such button of this question
    assert store.answer_question(OWNER, forged, "U0SAM0001") is Answered.NOT_OPEN


def shown(path: Path) -> list[tuple[str, str, int]]:
    with sqlite3.connect(path) as raw:
        return raw.execute("SELECT status, buttons, buttons_tries FROM review_items ORDER BY id").fetchall()


def test_a_closed_question_is_listed_until_its_buttons_are_taken_down(store: Store, path: Path) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    assert store.closed_with_buttons(limit=10) == []  # open: its buttons stay
    yes = store.button(YES)
    assert yes is not None
    assert store.answer_question(OWNER, yes, "U0LUIS0005") is Answered.ANSWERED
    [(item, answered_by)] = store.closed_with_buttons(limit=10)
    assert (item.id, item.post, item.message_ts, item.status, answered_by) == (
        yes.item.id, POST, REPLY, QuestionStatus.ANSWERED, "U0LUIS0005"
    )  # fmt: skip
    assert store.buttons_failed(OWNER, item.id) == 1
    assert store.buttons_failed(OWNER, item.id) == 2
    store.buttons_taken_down(OWNER, item.id, removed=True)
    store.buttons_taken_down(OWNER, item.id, removed=False)  # once: the first outcome stands
    assert store.closed_with_buttons(limit=10) == []
    assert shown(path) == [("answered", "removed", 2)]
    assert store.buttons_failed(OWNER, item.id) == 2  # no longer counted


def test_stale_questions_are_listed_fewest_tries_first(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    second = confirm("s" * 22, "t" * 22)
    reply_again(store, "1790000100.000100", 2, DryRunOutcome.REPLIED, question=second)
    reply_again(store, "1790000200.000100", 3, DryRunOutcome.RETRACTED)
    first, then = (item.id for item in store.questions(POST))
    assert [i.id for i, _ in store.closed_with_buttons(limit=10)] == [first, then]
    store.buttons_failed(OWNER, first)
    assert [(i.id, by) for i, by in store.closed_with_buttons(limit=10)] == [(then, None), (first, None)]
    assert [i.id for i, _ in store.closed_with_buttons(limit=1)] == [then]


def test_taking_buttons_down_needs_the_lock(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    reply_again(store, "1790000100.000100", 2, DryRunOutcome.RETRACTED)
    [(item, _)] = store.closed_with_buttons(limit=10)
    store.release_lock(OWNER)
    with pytest.raises(Exception, match="lock"):
        store.buttons_taken_down(OWNER, item.id, removed=True)
    with pytest.raises(Exception, match="lock"):
        store.buttons_failed(OWNER, item.id)


@pytest.mark.parametrize(
    ("setup", "statement"),
    [
        ("", "UPDATE review_items SET buttons = 'removed'"),  # still open
        ("stale", "UPDATE review_items SET buttons = 'shown'"),
        ("removed", "UPDATE review_items SET buttons = 'left'"),
        ("removed", "UPDATE review_items SET buttons = 'shown'"),
    ],
)
def test_buttons_are_taken_down_once_after_their_question_closed(
    store: Store, path: Path, setup: str, statement: str
) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    if setup:
        reply_again(store, "1790000100.000100", 2, DryRunOutcome.RETRACTED)
    if setup == "removed":
        [(item, _)] = store.closed_with_buttons(limit=10)
        store.buttons_taken_down(OWNER, item.id, removed=True)
    with sqlite3.connect(path) as raw, pytest.raises(sqlite3.IntegrityError, match="taken down once"):
        raw.execute(statement)


# Plan expiry (migration 0014): an open question whose plan expired is marked, and its buttons are taken down.

EXPIRES = Decimal(POST.ts)
"""`confirm()` counts its plan's expiry from the post's own time."""


def test_an_open_question_is_marked_expired_once_and_listed_for_its_buttons(store: Store, path: Path) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    assert store.expire_questions(OWNER, EXPIRES) == 0  # not before: the same instant is not expired, as for a click
    assert store.closed_with_buttons(limit=10) == []
    assert store.expire_questions(OWNER, EXPIRES + Decimal("0.000001")) == 1
    assert store.expire_questions(OWNER, EXPIRES + 1) == 0  # once
    [(item, answered_by)] = store.closed_with_buttons(limit=10)
    assert (item.status, answered_by) == (QuestionStatus.OPEN, None)
    store.buttons_taken_down(OWNER, item.id, removed=True)
    assert shown(path) == [("open", "removed", 0)]
    with sqlite3.connect(path) as raw:
        events = [row[0] for row in raw.execute("SELECT event FROM audit_log ORDER BY id")]
    assert events[-2:] == ["question_expired", "buttons_removed"]


def test_an_expired_question_takes_no_answer(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    store.expire_questions(OWNER, EXPIRES + 1)
    yes = store.button(YES)
    assert yes is not None
    assert store.answer_question(OWNER, yes, "U0SAM0001") is Answered.EXPIRED
    assert store.answer(yes.item.id) is None


def test_a_closed_question_does_not_expire(store: Store, path: Path) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    yes = store.button(YES)
    assert yes is not None
    assert store.answer_question(OWNER, yes, "U0SAM0001") is Answered.ANSWERED
    assert store.expire_questions(OWNER, EXPIRES + 1) == 0
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT expired_at FROM review_items").fetchall() == [(None,)]


def test_a_question_from_before_migration_0011_expires_from_the_post(store: Store, path: Path) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    with sqlite3.connect(path) as raw:
        raw.execute("DROP TRIGGER review_items_keep_question")  # as the upgrade left it: no expiry time
        raw.execute("UPDATE review_items SET expires_from = NULL")
    assert store.expire_questions(OWNER, EXPIRES) == 0
    assert store.expire_questions(OWNER, EXPIRES + 1) == 1


def test_expiring_questions_needs_the_lock(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    store.release_lock(OWNER)
    with pytest.raises(Exception, match="lock"):
        store.expire_questions(OWNER, EXPIRES + 1)


@pytest.mark.parametrize(
    ("setup", "statement", "message"),
    [
        ("expired", "UPDATE review_items SET expired_at = '2026-10-02T00:00:00+00:00'", "expires once"),
        ("expired", "UPDATE review_items SET expired_at = NULL", "expires once"),
        ("stale", "UPDATE review_items SET expired_at = '2026-10-02T00:00:00+00:00'", "expires once"),
        (
            "expired",
            "UPDATE review_items SET status = 'answered', answer = '{}', answered_by = 'U0SAM0001',"
            " closed_at = '2026-10-02T00:00:00+00:00'",
            "takes no answer",
        ),
    ],
)
def test_a_question_expires_once_while_open_and_then_takes_no_answer(
    store: Store, path: Path, setup: str, statement: str, message: str
) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    if setup == "expired":
        store.expire_questions(OWNER, EXPIRES + 1)
    else:
        reply_again(store, "1790000100.000100", 2, DryRunOutcome.RETRACTED)
    with sqlite3.connect(path) as raw, pytest.raises(sqlite3.IntegrityError, match=message):
        raw.execute(statement)


def test_a_question_is_asked_unexpired(store: Store, path: Path) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    with sqlite3.connect(path) as raw, pytest.raises(sqlite3.IntegrityError, match="asked unexpired"):
        raw.execute(
            "INSERT INTO review_items (post_id, revision, question, approver_only, channel_id, message_ts, status,"
            " created_at, poster_id, expires_from, expired_at) SELECT post_id, 2, question, approver_only,"
            " channel_id, message_ts, 'open', created_at, poster_id, expires_from, created_at FROM review_items"
        )


def test_closed_questions_are_taken_down_before_expired_ones(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    store.expire_questions(OWNER, EXPIRES + 1)
    other = PostKey(POST.workspace_id, POST.channel_id, "1790000500.000100")
    store.place_message(OWNER, other, user_id="U0SAM0001", thread_ts=None, window_seconds=5)
    store.finish_dry_run(
        OWNER,
        other,
        DryRunOutcome.REPLIED,
        "1790000520.000100",
        question=confirm("c" * 22, "d" * 22, expires_from=other.ts),
    )
    answered = store.button("c" * 22)
    assert answered is not None
    assert store.answer_question(OWNER, answered, "U0SAM0001") is Answered.ANSWERED
    assert [i.status for i, _ in store.closed_with_buttons(limit=10)] == [QuestionStatus.ANSWERED, QuestionStatus.OPEN]


def test_an_expired_question_can_still_go_stale_and_a_new_one_be_asked(store: Store) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    store.expire_questions(OWNER, EXPIRES + 1)
    reply_again(store, "1790000100.000100", 2, DryRunOutcome.REPLIED, question=confirm("s" * 22, "t" * 22))
    first, second = store.questions(POST)
    assert (first.status, second.status) == (QuestionStatus.STALE, QuestionStatus.OPEN)
    [(item, by)] = [(i, by) for i, by in store.closed_with_buttons(limit=10)]
    assert (item.id, item.status, by) == (first.id, QuestionStatus.STALE, None)  # says "replaced", not "expired"


# One plan expiry per post (migration 0015, issue #74): the first dry run fixes `plan_from`, and the post's
# revisions and questions all count from it.


def plan_from(path: Path) -> list[str | None]:
    with sqlite3.connect(path) as db:
        return [row[0] for row in db.execute("SELECT plan_from FROM posts ORDER BY id")]


def test_the_first_dry_run_fixes_the_plan_from_its_question_or_its_newest_message(store: Store, path: Path) -> None:
    store.place_message(OWNER, PostKey(POST.workspace_id, POST.channel_id, "1790000003.000100"), user_id="U0SAM0001",
                        thread_ts=POST.ts, window_seconds=5)  # fmt: skip
    assert plan_from(path) == [None]
    with pytest.raises(ValueError, match="one of its post's messages"):
        store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm(expires_from="1790000002.0"))
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm(expires_from=POST.ts))
    assert plan_from(path) == [POST.ts]
    other = PostKey(POST.workspace_id, POST.channel_id, "1790000500.000100")
    store.place_message(OWNER, other, user_id="U0SAM0001", thread_ts=None, window_seconds=5)
    store.place_message(OWNER, PostKey(POST.workspace_id, POST.channel_id, "1790000502.000100"), user_id="U0SAM0001",
                        thread_ts=other.ts, window_seconds=5)  # fmt: skip
    store.finish_dry_run(OWNER, other, DryRunOutcome.SILENT)
    assert plan_from(path) == [POST.ts, "1790000502.000100"]  # no question: the newest message it read


def test_a_revision_counts_from_the_plan_and_its_question_expires_with_it(store: Store, path: Path) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, REPLY, question=confirm())
    store.place_message(OWNER, PostKey(POST.workspace_id, POST.channel_id, "1790000100.000100"), user_id="U0SAM0001",
                        thread_ts=POST.ts, window_seconds=5)  # fmt: skip
    [waiting] = store.posts_waiting_for_dry_run(revisable_since=Decimal(POST.ts))
    assert (waiting.revision, waiting.plan_from) == (2, POST.ts)
    assert store.posts_waiting_for_dry_run(revisable_since=Decimal(POST.ts) + Decimal("0.000001")) == []
    with pytest.raises(ValueError, match="expires with its post's plan"):
        store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, "1790000100.000109", revision=2,
                             question=confirm("c" * 22, "d" * 22, expires_from="1790000100.000100"))  # fmt: skip
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, "1790000100.000109", revision=2,
                         question=confirm("c" * 22, "d" * 22))  # fmt: skip
    assert [item.expires_from for item in store.questions(POST)] == [POST.ts, POST.ts]
    assert plan_from(path) == [POST.ts]


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("UPDATE posts SET plan_from = '1790000001.000100'", "expires from one time"),
        ("UPDATE posts SET plan_from = NULL", "expires from one time"),
    ],
)
def test_the_plan_is_fixed_once(store: Store, path: Path, statement: str, message: str) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.SILENT)
    with sqlite3.connect(path) as db, pytest.raises(sqlite3.IntegrityError, match=message):
        db.execute(statement)


def test_a_dry_run_cannot_be_recorded_without_its_plan(store: Store, path: Path) -> None:
    with sqlite3.connect(path) as db, pytest.raises(sqlite3.IntegrityError, match="fixes when the post's plan"):
        db.execute("UPDATE posts SET dry_run_at = 'now'")


def test_the_upgrade_fixes_each_plan_from_its_first_question_or_the_post(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stockthread.store import db as store_db

    real = store_db._migrations()
    monkeypatch.setattr(store_db, "_migrations", lambda: real[:14])
    old = Store.open(path)
    old.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    keys = [PostKey(POST.workspace_id, POST.channel_id, f"17900000{n}0.000100") for n in range(3)]
    for key in keys:
        old.place_message(OWNER, key, user_id="U0SAM0001", thread_ts=None, window_seconds=5)
    old.close()
    with sqlite3.connect(path) as db:  # this code's finish_dry_run writes the new column
        db.execute("UPDATE posts SET dry_run_at = 'then' WHERE id IN (1, 2)")
        db.execute(
            "INSERT INTO review_items (post_id, revision, question, approver_only, channel_id, message_ts, status,"
            " created_at, poster_id, expires_from) VALUES (1, 1, '{}', 0, ?, ?, 'open', 'then', ?, ?)",
            (POST.channel_id, REPLY, "U0SAM0001", "1790000005.000100"),
        )
    monkeypatch.setattr(store_db, "_migrations", lambda: real)
    Store.open(path).close()
    # The first question's time, the post's own time, and none yet for a post without its dry run.
    assert plan_from(path) == ["1790000005.000100", keys[1].ts, None]
