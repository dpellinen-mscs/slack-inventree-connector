"""Confirmed aliases in the store (ADR 0012): only a verified request teaches one, a later confirmation replaces it."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from stockthread.catalog import with_aliases
from stockthread.core.marker import AttemptKind
from stockthread.core.matching import Alias, Catalog, PartRecord, match_part
from stockthread.core.model import MatchRule, Operation, PostKey, RequestKey
from stockthread.core.requests import Actor, RequestState
from stockthread.core.write_request import write_notes
from stockthread.store import AliasKind, LockLost, StateConflict, Store

S = RequestState
POST = PostKey("T0ACME", "C0STOCK", "1727600000.000100")
KEY = RequestKey(POST, 1, 1)
OWNER = "test-instance"
PART, LOCATION = AliasKind.PART, AliasKind.LOCATION


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store.open(tmp_path / "stockthread.db")
    opened.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    opened.create_post(POST)
    opened.create_plan(POST, "0" * 64, (Operation.TRANSFER,))
    yield opened
    opened.close()


def verify(store: Store) -> None:
    store.transition(KEY, expected=S.PLANNED, target=S.APPROVED, actor=Actor.HUMAN)
    marker = store.next_marker(KEY, AttemptKind.APPLY)
    store.begin_attempt(
        KEY,
        AttemptKind.APPLY,
        expected=S.APPROVED,
        actor=Actor.APPLIER,
        snapshot={},
        request_body={"items": [], "location": 4, "notes": write_notes(marker, "U0SAM")},
        lock_owner=OWNER,
    )
    store.transition(KEY, expected=S.APPLYING, target=S.APPLIED, actor=Actor.APPLIER)
    store.transition(KEY, expected=S.APPLIED, target=S.VERIFIED, actor=Actor.APPLIER)


def test_a_verified_confirmation_stores_an_alias_with_its_rivals(store: Store) -> None:
    verify(store)
    store.learn_alias(OWNER, PART, " Disc 40 ", 1, rivals=[1, 2, 1], learned_from=KEY)
    store.learn_alias(OWNER, LOCATION, "my truck", 11, rivals=[11], learned_from=KEY, user="U0SAM")
    assert store.aliases(PART) == (Alias("Disc 40", 1, rivals=frozenset({1, 2})),)
    assert store.aliases(LOCATION) == (Alias("my truck", 11, "U0SAM", frozenset({11})),)


@pytest.mark.parametrize("state", [S.PLANNED, S.APPROVED, S.APPLIED])
def test_only_a_verified_request_teaches_an_alias(store: Store, state: RequestState) -> None:
    if state is not S.PLANNED:
        store.transition(KEY, expected=S.PLANNED, target=S.APPROVED, actor=Actor.HUMAN)
    if state is S.APPLIED:
        marker = store.next_marker(KEY, AttemptKind.APPLY)
        store.begin_attempt(
            KEY, AttemptKind.APPLY, expected=S.APPROVED, actor=Actor.APPLIER, snapshot={},
            request_body={"items": [], "location": 4, "notes": write_notes(marker, "U0SAM")}, lock_owner=OWNER,
        )  # fmt: skip
        store.transition(KEY, expected=S.APPLYING, target=S.APPLIED, actor=Actor.APPLIER)
    with pytest.raises(StateConflict):
        store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[1], learned_from=KEY)
    assert store.aliases(PART) == ()


def test_a_later_confirmation_of_the_same_words_replaces_the_alias(store: Store) -> None:
    verify(store)
    store.learn_alias(OWNER, PART, "Disc 40", 1, rivals=[1], learned_from=KEY)
    store.learn_alias(OWNER, PART, "disc  40", 7, rivals=[1, 7], learned_from=KEY)
    store.learn_alias(OWNER, PART, "disc 40", 9, rivals=[9], learned_from=KEY, user="U0SAM")
    assert store.aliases(PART) == (
        Alias("disc  40", 7, rivals=frozenset({1, 7})),
        Alias("disc 40", 9, "U0SAM", frozenset({9})),
    )
    assert store.aliases(LOCATION) == ()


def test_learning_an_alias_needs_the_lock_and_is_audited(store: Store, tmp_path: Path) -> None:
    verify(store)
    with pytest.raises(LockLost):
        store.learn_alias("another-instance", PART, "disc 40", 1, rivals=[1], learned_from=KEY)
    with pytest.raises(ValueError, match="at least one word"):
        store.learn_alias(OWNER, PART, " -- ", 1, rivals=[1], learned_from=KEY)
    with pytest.raises(KeyError):
        store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[1], learned_from=RequestKey(POST, 1, 2))
    store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[1], learned_from=KEY)
    raw = sqlite3.connect(tmp_path / "stockthread.db")
    events = raw.execute("SELECT event, request_id, alias_id FROM audit_log WHERE event = 'alias_learned'").fetchall()
    raw.close()
    assert events == [("alias_learned", 1, 1)]


def test_the_worker_s_catalog_carries_the_aliases_stored_now(store: Store) -> None:
    parts = (PartRecord(1, "AP-NV-D40", "Norvane Disc 40 Access Point", ""),)
    catalog = with_aliases(lambda: Catalog(parts=parts, locations=()), store)
    assert match_part("disc 40", catalog()).rule is MatchRule.WORDS
    verify(store)
    store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[1], learned_from=KEY)
    assert match_part("disc 40", catalog()).rule is MatchRule.ALIAS
