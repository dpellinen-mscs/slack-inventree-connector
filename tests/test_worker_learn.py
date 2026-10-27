# SPDX-License-Identifier: MIT
"""Alias learning in the worker (ADR 0012): once every request of a plan an approver's click approved is verified,
`Worker.work_due` stores what the plan teaches (`learning.lessons`), and the receipt names it. Nothing else teaches."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
import pytest
from fake_inventree import ITEM, FakeInvenTree
from views import picked, view

from stockthread.applier import GateNow, approve_by_click, approve_by_policy, work_request
from stockthread.apply import Applied, undo_apply
from stockthread.config import parse_settings
from stockthread.core.matching import Alias, Catalog, LocationRecord, PartRecord, match_part
from stockthread.core.model import (
    Extraction,
    Match,
    MatchRule,
    MatchStatus,
    MessageInput,
    MovementType,
    PostKey,
    RequestKey,
)
from stockthread.core.requests import Actor, RequestState
from stockthread.core.resolve import STATUS_OK, StockItem
from stockthread.gate import Policy
from stockthread.inventree import DEPLETING_MARKER_FOUND_FROM, InvenTreeError, ServerInfo, VersionCheck
from stockthread.plan_record import plan_of
from stockthread.slack.replies import MovementView
from stockthread.store import AliasKind, DryRunOutcome, NewPlan, Store
from stockthread.worker import Worker, _record
from stockthread.writer import Outcome

S = RequestState
OWNER = "worker-1"
WORKSPACE, APP, CHANNEL = "T0ACME001", "A0ACME001", "C0ACME001"
SAM, LUIS = "U0SAM0001", "U0LUIS001"
POST = PostKey(WORKSPACE, CHANNEL, "1700000000.000100")
KEY = RequestKey(POST, 1, 1)
TAKE = view(
    lines=(("Norvane 40", "2"),), items=(Match(MatchStatus.MATCHED, MatchRule.ANSWER, 1),), stock=(picked((ITEM, "2")),)
)
"""Sam takes "Norvane 40", part 1 as they confirmed in the thread, from location 10 to his truck, location 11."""
TESTED = VersionCheck(ServerInfo("1.5.6", DEPLETING_MARKER_FOUND_FROM), True, "tested")
ON = GateNow(Policy(True, frozenset({MovementType.TAKE_TO_VEHICLE}), frozenset({LUIS}), TESTED), {SAM: 11})
CATALOG = Catalog(
    parts=(PartRecord(1, "DISC-40", "Norvane Disc 40 Access Point"), PartRecord(2, "PC-1", "Patch Cable 1 m")),
    locations=(LocationRecord(10, "Main stock"), LocationRecord(11, "Vehicles/Truck 1")),
)
ENV = {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic", "INVENTREE_TOKEN": "inv-synthetic"}
LEARNED = "Learned from this Apply: “Norvane 40” means Norvane Disc 40 Access Point."


class FakeSlack:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posted.append(kwargs)
        return {"ok": True, "ts": f"1700000400.{len(self.posted):06d}"}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store.open(tmp_path / "stockthread.db")
    opened.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    yield opened
    opened.close()


def clicked(store: Store, *movements: MovementView) -> None:
    """The dry run of `movements` (the take-out alone by default) posted in the thread, and its plan approved by
    Luis's Apply click."""
    movements = movements or (TAKE,)
    store.place_message(OWNER, POST, user_id=SAM, thread_ts=None, window_seconds=5)
    extraction = Extraction("rules", tuple(movement.movement for movement in movements))
    record = _record(extraction, list(movements), late=False, poster=SAM, messages=(), max_quantity=500)
    result = json.loads(json.dumps(record))
    made = plan_of(result)
    assert made is not None
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, "1700000100.000100", result=result, plan=NewPlan(*made))
    store.approve_plan(OWNER, POST, 1, actor=Actor.HUMAN, approver=LUIS)


SAID = "took 2 Norvane 40 and 1 patch cord from main stock to my truck"
"""What Sam posted, as the post's messages read it, for a plan stored here rather than read from a queued post."""


def worker(
    store: Store, inventree: FakeInvenTree, catalog: Any = lambda: CATALOG, *, said: str | None = SAID
) -> Worker:
    """A worker with writes on against `inventree`. `said`: the text its post's messages read as; None reads the
    post's queued events, as a running connector does."""
    lock = inventree.lock = anyio.Lock()

    def apply(key: RequestKey) -> Applied:
        return anyio.run(
            lambda: work_request(
                key, store=store, reader=inventree, writer=inventree, lock=lock, lock_owner=OWNER,
                gate_now=lambda: ON, expired=lambda _: False, settle=timedelta(0),
            )
        )  # fmt: skip

    def undo(key: RequestKey, movement: MovementView, user: str) -> Applied:
        return anyio.run(
            lambda: undo_apply(
                key, movement, store=store, reader=inventree, writer=inventree, lock=lock, lock_owner=OWNER, user=user
            )
        )

    data = {
        "slack": {"workspace_id": WORKSPACE, "app_id": APP, "channels": [CHANNEL], "approvers": [LUIS]},
        "inventree": {"url": "http://inventree.local:8000", "timeout_seconds": 1},
        "writes": {"enabled": True},
    }
    running = Worker(
        parse_settings(data, ENV), store, OWNER, FakeSlack(),  # type: ignore[arg-type]
        catalog, lambda: datetime.fromtimestamp(1700000300, UTC), undo=undo, apply=apply,
    )  # fmt: skip
    if said is not None:
        text = said

        def messages(post: PostKey, changes: int) -> tuple[list[MessageInput], str, bool, dict[str, str]]:
            return [MessageInput(post.ts, text)], SAM, False, {post.ts: SAM}

        running._messages = messages  # type: ignore[method-assign,assignment]
    return running


def receipt_text(running: Worker) -> str:
    [posted] = running.slack.posted  # type: ignore[attr-defined]
    return json.dumps(posted["blocks"], ensure_ascii=False)


def test_a_clicked_plan_once_verified_teaches_its_confirmed_text_and_the_receipt_names_it(store: Store) -> None:
    clicked(store)
    running = worker(store, FakeInvenTree())
    assert running.work_due() == 1
    assert store.request(KEY).state is S.VERIFIED
    assert store.aliases(AliasKind.PART) == (Alias("Norvane 40", 1, None, frozenset({1})),)
    assert store.aliases(AliasKind.LOCATION) == ()  # main stock and my truck were not confirmed by a person
    [stored] = store.stored_aliases()
    assert stored.learned_from == KEY
    assert running.post_receipts() == 1
    assert LEARNED in receipt_text(running)
    # The next post that says "norvane 40" is an alias match, which may authorize a write.
    assert match_part("norvane 40", CATALOG, SAM).rule is MatchRule.WORDS
    taught = replace(CATALOG, part_aliases=store.aliases(AliasKind.PART))
    assert match_part("norvane 40", taught, SAM) == Match(MatchStatus.MATCHED, MatchRule.ALIAS, 1)


def test_a_plan_whose_write_did_not_read_back_as_planned_teaches_nothing(store: Store) -> None:
    clicked(store)
    inventree = FakeInvenTree()
    inventree.moves = False
    running = worker(store, inventree)
    running.work_due()
    assert store.request(KEY).state is S.VERIFY_FAILED
    assert store.stored_aliases() == ()
    assert running.post_receipts() == 1
    assert "Learned" not in receipt_text(running)


def test_a_plan_teaches_nothing_until_every_request_of_it_is_verified(store: Store) -> None:
    cable = view(
        lines=(("patch cord", "1"),),
        items=(Match(MatchStatus.MATCHED, MatchRule.ANSWER, 2),),
        stock=(picked((58, "1")),),
    )
    clicked(store, TAKE, cable)
    inventree = FakeInvenTree()
    inventree.items[58] = [2, 10, Decimal(1)]

    def second_refused() -> None:
        if len(inventree.sent) == 2:
            inventree.answer = Outcome.REFUSED

    inventree.after_write = second_refused
    running = worker(store, inventree)
    assert running.work_due() >= 1
    assert store.request(KEY).state is S.VERIFIED
    assert store.stored_aliases() == ()  # the second request is not verified yet
    while running.work_due():
        pass
    assert store.request(RequestKey(POST, 1, 2)).state is not S.VERIFIED
    assert store.stored_aliases() == ()


def test_a_catalog_outage_while_learning_teaches_nothing_and_changes_nothing_else(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    clicked(store)
    reads = iter([InvenTreeError("down")])

    def catalog() -> Catalog:
        if error := next(reads, None):
            raise error
        return CATALOG

    running = worker(store, FakeInvenTree(), catalog)
    with caplog.at_level(logging.WARNING):
        assert running.work_due() == 1
    assert store.request(KEY).state is S.VERIFIED
    assert store.stored_aliases() == ()
    assert "nothing learned from plan revision 1 (InvenTreeError)" in caplog.text
    assert running.post_receipts() == 1
    assert "Learned" not in receipt_text(running)


def test_an_undo_forgets_what_its_plan_taught_and_its_receipt_names_nothing_learned(
    store: Store, tmp_path: Path
) -> None:
    clicked(store)
    running = worker(store, FakeInvenTree())
    running.work_due()
    running.post_receipts()
    [alias] = store.stored_aliases()
    assert running.undo is not None
    assert running.undo(KEY, TAKE, LUIS).state is S.UNDONE
    assert store.stored_aliases() == ()
    with sqlite3.connect(tmp_path / "stockthread.db") as raw:
        forgotten = raw.execute(
            "SELECT actor, alias_id, alias_kind, alias_record FROM audit_log WHERE event = 'alias_forgotten'"
        ).fetchall()
    assert forgotten == [("human", alias.id, alias.kind.value, alias.record)]
    assert running.post_receipts() == 1
    undone = json.dumps(running.slack.posted[-1]["blocks"], ensure_ascii=False)  # type: ignore[attr-defined]
    assert "Undone" in undone
    assert "Learned" not in undone


def test_a_confirmed_text_the_post_does_not_say_teaches_nothing(store: Store) -> None:
    clicked(store)
    running = worker(store, FakeInvenTree(), said="took the two blue APs to my truck")  # the model's "Norvane 40"
    running.work_due()
    assert store.request(KEY).state is S.VERIFIED
    assert store.stored_aliases() == ()


def test_the_aliases_a_plan_taught_are_found_by_its_revision_until_another_plan_teaches_the_text(
    store: Store,
) -> None:
    clicked(store)
    worker(store, FakeInvenTree()).work_due()
    [first] = store.stored_aliases()
    assert store.aliases_taught_by(POST, 1) == (first,)
    assert store.aliases_taught_by(POST, 2) == ()
    other = PostKey(WORKSPACE, CHANNEL, "1700000050.000100")
    assert store.aliases_taught_by(other, 1) == ()


# --- From a post to a learned alias, and the next post applied with no click (M3 DoD, against the fake InvenTree) ---


class Thread(FakeSlack):
    def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "ts": kwargs["ts"]}

    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "messages": []}


class Clock:
    def __init__(self) -> None:
        self.now = 1700000001.0

    def __call__(self) -> datetime:
        return datetime.fromtimestamp(self.now, UTC)


def message(ts: str, text: str) -> str:
    event = {"type": "message", "channel": CHANNEL, "user": SAM, "text": text, "ts": ts}
    return json.dumps(
        {"type": "event_callback", "team_id": WORKSPACE, "api_app_id": APP, "event_id": f"Ev{ts}", "event": event}
    )


def press(store: Store, posted: dict[str, Any], ts: str, element: dict[str, Any], user: str, at: str) -> None:
    payload = {
        "type": "block_actions",
        "api_app_id": APP,
        "team": {"id": WORKSPACE},
        "user": {"id": user, "team_id": WORKSPACE},
        "channel": {"id": CHANNEL},
        "container": {"type": "message", "message_ts": ts, "channel_id": CHANNEL, "is_ephemeral": False},
        "message": {"type": "message", "ts": ts, "thread_ts": posted["thread_ts"], "text": posted["text"]},
        "actions": [{**element, "type": "button", "action_ts": at}],
    }
    assert store.enqueue_event(f"click:{at}", "interactive", json.dumps(payload))


def buttons(posted: dict[str, Any]) -> list[dict[str, Any]]:
    return [element for block in posted["blocks"] if block["type"] == "actions" for element in block["elements"]]


def test_a_confirmed_whole_word_take_out_waits_for_apply_once_and_then_applies_with_no_click(store: Store) -> None:
    inventree = FakeInvenTree()
    inventree.items[59] = [1, 10, Decimal(2)]
    clock = Clock()

    def stock(pairs: Any) -> dict[tuple[int, int], list[StockItem]]:
        return {
            pair: [
                StockItem(pk, quantity, STATUS_OK, Decimal(0))
                for pk, (part, location, quantity) in sorted(inventree.items.items())
                if (part, location) == pair
            ]
            for pair in pairs
        }

    running = worker(store, inventree, said=None)
    running.slack = Thread()
    running.clock = clock
    running.stock = stock
    running.catalog = lambda: replace(
        CATALOG,
        part_aliases=store.aliases(AliasKind.PART),
        location_aliases=store.aliases(AliasKind.LOCATION),
        vehicles={SAM: 11},
    )
    running.approve = lambda post: approve_by_policy(store, OWNER, post, ON)
    running.approve_click = lambda post, revision, user, at: approve_by_click(
        store, OWNER, post, revision, user, ON, at=at, expiry_hours=24
    )
    posted: list[dict[str, Any]] = running.slack.posted

    def settle(seconds: float = 30) -> None:
        clock.now += seconds
        for _ in range(4):
            running.step()

    text = "took 2 norvane 40 from main stock to truck 1"
    first = PostKey(WORKSPACE, CHANNEL, "1700000001.000100")
    assert store.enqueue_event("Ev1:event", "event", message(first.ts, text))
    settle()
    # Asked first: "norvane 40" is a whole-word match, never an automatic one.
    assert store.current_plan(first) is None
    [question] = [element for element in buttons(posted[-1]) if element.get("text", {}).get("text") == "Yes"]
    press(store, posted[-1], f"1700000400.{len(posted):06d}", question, SAM, f"{clock.now:.6f}")
    settle()
    plan = store.current_plan(first)
    assert plan is not None
    assert [row.state for row in plan.requests] == [S.PLANNED]  # the answer makes it wait for Apply
    [apply] = [element for element in buttons(posted[-1]) if element["action_id"] == "apply"]
    press(store, posted[-1], f"1700000400.{len(posted):06d}", apply, LUIS, f"{clock.now:.6f}")
    settle()
    assert store.request(RequestKey(first, 1, 1)).state is S.VERIFIED
    assert [(a.text, a.record) for a in store.aliases(AliasKind.PART)] == [("norvane 40", 1)]
    assert "Learned from this Apply: “norvane 40” means Norvane Disc 40 Access Point." in json.dumps(
        posted[-1]["blocks"], ensure_ascii=False
    )
    # The second time, the same words apply with no question and no click.
    second = PostKey(WORKSPACE, CHANNEL, f"{clock.now + 1:.6f}")
    assert store.enqueue_event("Ev2:event", "event", message(second.ts, text))
    settle()
    settle()
    assert store.request(RequestKey(second, 1, 1)).state is S.VERIFIED
    assert store.plan(second, 1).approved_by is None
    assert [sent.body["items"] for sent in inventree.sent][-1] == [{"pk": 59, "quantity": "2", "merge": False}]


def test_only_the_posters_own_messages_say_a_text(store: Store) -> None:
    clicked(store)
    running = worker(store, FakeInvenTree())

    def messages(post: PostKey, changes: int) -> tuple[list[MessageInput], str, bool, dict[str, str]]:
        said = [MessageInput(post.ts, "took the two blue APs to my truck"), MessageInput("1700000002.000100", SAID)]
        return said, SAM, False, {post.ts: SAM, "1700000002.000100": LUIS}

    running._messages = messages  # type: ignore[method-assign,assignment]
    running.work_due()
    assert store.request(KEY).state is S.VERIFIED
    assert store.stored_aliases() == ()  # "Norvane 40" is in Luis's reply, not in Sam's post


def test_an_edit_or_deletion_after_the_write_forgets_what_the_post_taught(store: Store, tmp_path: Path) -> None:
    """The edit may take back the words the alias was learned from; the stock it moved stays moved until undone."""
    clicked(store)
    running = worker(store, FakeInvenTree())
    running.work_due()
    [alias] = store.stored_aliases()
    store.note_revision(OWNER, POST, slack_event_id=None)
    assert store.stored_aliases() == ()
    assert store.request(KEY).state is S.VERIFIED
    with sqlite3.connect(tmp_path / "stockthread.db") as raw:
        forgotten = raw.execute("SELECT actor, alias_id FROM audit_log WHERE event = 'alias_forgotten'").fetchall()
    assert forgotten == [("ingest", alias.id)]


def test_an_edit_noted_while_the_lesson_is_read_teaches_nothing(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    """The edit lands after the post's messages were read and before the alias is stored: forgetting it at the edit
    would find nothing yet, so learning refuses."""
    clicked(store)

    def catalog() -> Catalog:
        store.note_revision(OWNER, POST)
        return CATALOG

    running = worker(store, FakeInvenTree(), catalog)
    with caplog.at_level(logging.WARNING):
        running.work_due()
    assert store.request(KEY).state is S.VERIFIED
    assert store.stored_aliases() == ()
    assert "an alias of plan revision 1 not learned (StateConflict)" in caplog.text


def test_a_post_edited_before_its_dry_run_teaches_what_that_dry_run_read(store: Store) -> None:
    """The dry run read the post after the edit, so the lesson is read at the same point and is stored: only an edit
    noted after the text was read forgets or refuses it."""
    store.place_message(OWNER, POST, user_id=SAM, thread_ts=None, window_seconds=5)
    store.note_revision(OWNER, POST)
    through = store.post_changes_through(POST)
    assert through > 0
    extraction = Extraction("rules", (TAKE.movement,))
    record = _record(extraction, [TAKE], late=False, poster=SAM, messages=(), max_quantity=500)
    result = json.loads(json.dumps(record))
    made = plan_of(result)
    assert made is not None
    store.finish_dry_run(
        OWNER, POST, DryRunOutcome.REPLIED, "1700000100.000100", result=result, plan=NewPlan(*made), changes=through
    )
    store.approve_plan(OWNER, POST, 1, actor=Actor.HUMAN, approver=LUIS)
    read: list[int] = []
    running = worker(store, FakeInvenTree())
    messages = running._messages

    def reading(post: PostKey, changes: int) -> Any:
        read.append(changes)
        return messages(post, changes)

    running._messages = reading  # type: ignore[method-assign,assignment]
    running.work_due()
    assert store.request(KEY).state is S.VERIFIED
    assert read == [through]
    assert [alias.text for alias in store.stored_aliases()] == ["Norvane 40"]
