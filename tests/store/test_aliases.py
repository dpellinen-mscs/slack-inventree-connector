"""Confirmed aliases in the store (ADR 0012): only a verified request teaches one, a later confirmation replaces it."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from stockthread import __main__ as cli
from stockthread.alias_admin import EXPORT_FORMAT, alias_lines
from stockthread.catalog import with_aliases
from stockthread.core.marker import AttemptKind
from stockthread.core.matching import Alias, Catalog, LocationRecord, PartRecord, match_part
from stockthread.core.model import MatchRule, Operation, PostKey, RequestKey
from stockthread.core.requests import Actor, RequestState
from stockthread.core.write_request import write_notes
from stockthread.inventree import InvenTreeError
from stockthread.store import AliasKind, LockLost, StateConflict, Store, StoredAlias

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
    store.learn_alias(OWNER, PART, " Disc 40 ", 1, rivals=[1, 2, 1], learned_from=KEY, read_through=0)
    store.learn_alias(OWNER, LOCATION, "my truck", 11, rivals=[11], learned_from=KEY, read_through=0, user="U0SAM")
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
        store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[1], learned_from=KEY, read_through=0)
    assert store.aliases(PART) == ()


def test_a_later_confirmation_of_the_same_words_replaces_the_alias(store: Store) -> None:
    verify(store)
    store.learn_alias(OWNER, PART, "Disc 40", 1, rivals=[1], learned_from=KEY, read_through=0)
    store.learn_alias(OWNER, PART, "disc  40", 7, rivals=[1, 7], learned_from=KEY, read_through=0)
    store.learn_alias(OWNER, PART, "disc 40", 9, rivals=[9], learned_from=KEY, read_through=0, user="U0SAM")
    assert store.aliases(PART) == (
        Alias("disc  40", 7, rivals=frozenset({1, 7})),
        Alias("disc 40", 9, "U0SAM", frozenset({9})),
    )
    assert store.aliases(LOCATION) == ()


def test_learning_an_alias_needs_the_lock_and_is_audited(store: Store, tmp_path: Path) -> None:
    verify(store)
    with pytest.raises(LockLost):
        store.learn_alias("another-instance", PART, "disc 40", 1, rivals=[1], learned_from=KEY, read_through=0)
    with pytest.raises(ValueError, match="at least one word"):
        store.learn_alias(OWNER, PART, " -- ", 1, rivals=[1], learned_from=KEY, read_through=0)
    with pytest.raises(KeyError):
        store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[1], learned_from=RequestKey(POST, 1, 2), read_through=0)
    store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[1], learned_from=KEY, read_through=0)
    raw = sqlite3.connect(tmp_path / "stockthread.db")
    events = raw.execute("SELECT event, request_id, alias_id FROM audit_log WHERE event = 'alias_learned'").fetchall()
    raw.close()
    assert events == [("alias_learned", 1, 1)]


def test_the_worker_s_catalog_carries_the_aliases_stored_now(store: Store) -> None:
    parts = (PartRecord(1, "AP-NV-D40", "Norvane Disc 40 Access Point", ""),)
    catalog = with_aliases(lambda: Catalog(parts=parts, locations=()), store)
    assert match_part("disc 40", catalog()).rule is MatchRule.WORDS
    verify(store)
    store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[1], learned_from=KEY, read_through=0)
    assert match_part("disc 40", catalog()).rule is MatchRule.ALIAS


def test_aliases_the_matcher_ignores_are_logged_once_until_the_list_changes(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """M4 DoD: an alias that points to an inactive part or a deleted location is ignored and reported."""
    verify(store)
    store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[1], learned_from=KEY, read_through=0)
    store.learn_alias(OWNER, LOCATION, "the yard", 5, rivals=[5], learned_from=KEY, read_through=0)
    snapshots = iter([(), (), PARTS, (PartRecord(1, "AP-NV-D40", "Norvane Disc 40", active=False),)])
    catalog = with_aliases(lambda: Catalog(parts=next(snapshots), locations=()), store)
    caplog.set_level("WARNING", logger="stockthread.catalog")

    seen: list[str] = []

    def warned() -> list[str]:
        messages = [record.getMessage() for record in caplog.records]
        seen.extend(messages)
        caplog.clear()
        return messages

    assert match_part("disc 40", catalog()).status.value == "unmatched"  # ignored, not matched to a missing part
    assert warned() == [
        "aliases: stored aliases name location 5, part 1, which cannot be matched now: they are ignored"
        " (`stockthread check-setup` lists them by id)"
    ]
    catalog()
    assert warned() == []  # the same list: said once
    catalog()  # part 1 is back
    (message,) = warned()
    assert "name location 5, which" in message
    assert match_part("disc 40", catalog()).status.value == "unmatched"  # inactive again
    assert "part 1" in warned()[0]
    assert len(seen) == 3
    assert all("disc" not in message for message in seen)


def test_a_confirmation_replaces_every_alias_the_matcher_takes_for_its_text(store: Store) -> None:
    """Plurals fold: "patch cables" and "patch cable" are one alias, or the question would never stop (review #142)."""
    verify(store)
    store.learn_alias(OWNER, PART, "patch cable", 1, rivals=[1], learned_from=KEY, read_through=0)
    store.learn_alias(OWNER, PART, "patch cables", 7, rivals=[1, 7], learned_from=KEY, read_through=0)
    assert store.aliases(PART) == (Alias("patch cables", 7, rivals=frozenset({1, 7})),)
    parts = (PartRecord(1, "PC-1", "Patch Cable 1m", ""), PartRecord(7, "PC-3", "Patch Cable 3m", ""))
    catalog = with_aliases(lambda: Catalog(parts=parts, locations=()), store)
    assert match_part("patch cable", catalog()).record == 7


def test_learning_again_is_audited_with_the_alias(store: Store, tmp_path: Path) -> None:
    verify(store)
    store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[1], learned_from=KEY, read_through=0)
    store.learn_alias(OWNER, PART, "Disc 40", 2, rivals=[1, 2], learned_from=KEY, read_through=0)
    raw = sqlite3.connect(tmp_path / "stockthread.db")
    events = raw.execute("SELECT event, alias_id FROM audit_log WHERE alias_id IS NOT NULL ORDER BY id").fetchall()
    raw.close()
    assert events == [("alias_learned", 1), ("alias_learned", 1)]


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        (
            "INSERT OR REPLACE INTO aliases (id, kind, text, words, record, created_at, updated_at)"
            " VALUES (1, 'part', 'x', 'x', 2, 'now', 'now')",
            "aliases are replaced by UPDATE only",
        ),
        (
            "INSERT INTO aliases (kind, text, words, record, rivals, created_at, updated_at)"
            " VALUES ('part', 'y', 'y', 2, '[\"x\", 1.5]', 'now', 'now')",
            "rivals must be record ids",
        ),
        ("UPDATE aliases SET rivals = '[0]'", "rivals must be record ids"),
    ],
)
def test_the_database_keeps_aliases_whole(store: Store, tmp_path: Path, statement: str, message: str) -> None:
    verify(store)
    store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[1], learned_from=KEY, read_through=0)
    raw = sqlite3.connect(tmp_path / "stockthread.db", isolation_level=None)
    with pytest.raises(sqlite3.IntegrityError, match=message):
        raw.execute(statement)
    raw.close()
    assert store.aliases(PART) == (Alias("disc 40", 1, rivals=frozenset({1})),)


# The admin's view: list, export and remove (M4)

PARTS = (PartRecord(1, "AP-NV-D40", "Norvane Disc 40 Access Point", ""),)


def test_the_stored_aliases_say_where_each_was_learned(store: Store) -> None:
    verify(store)
    store.learn_alias(OWNER, LOCATION, "my truck", 11, rivals=[11], learned_from=KEY, read_through=0, user="U0SAM")
    store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[2, 1], learned_from=KEY, read_through=0)
    first, second = store.stored_aliases()
    assert (first.id, first.kind, first.text, first.record, first.user) == (2, PART, "disc 40", 1, None)
    assert (first.rivals, first.learned_from) == (frozenset({1, 2}), KEY)
    assert (second.id, second.kind, second.user, second.learned_from) == (1, LOCATION, "U0SAM", KEY)
    assert first.created_at == first.updated_at


def test_removing_an_alias_restores_the_earlier_match_and_is_audited(store: Store, tmp_path: Path) -> None:
    """DoD (M4): removing an alias restores the earlier behavior, at the worker's next catalog read."""
    catalog = with_aliases(lambda: Catalog(parts=PARTS, locations=()), store)
    verify(store)
    store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[1], learned_from=KEY, read_through=0)
    assert match_part("disc 40", catalog()).rule is MatchRule.ALIAS
    removed = store.remove_alias(1)
    assert (removed.text, removed.record, removed.learned_from) == ("disc 40", 1, KEY)
    assert match_part("disc 40", catalog()).rule is MatchRule.WORDS
    assert store.stored_aliases() == ()
    with pytest.raises(KeyError):
        store.remove_alias(1)
    raw = sqlite3.connect(tmp_path / "stockthread.db")
    events = raw.execute(
        "SELECT actor, event, post_id, request_id, alias_id FROM audit_log WHERE alias_id IS NOT NULL ORDER BY id"
    ).fetchall()
    raw.close()
    assert events == [("applier", "alias_learned", 1, 1, 1), ("admin", "alias_removed", 1, 1, 1)]
    assert ("admin", "alias_removed") in [(actor, event) for _, actor, event, _, _ in store.audit(POST)]


def test_an_admin_removes_an_alias_while_the_connector_holds_the_lock(store: Store, tmp_path: Path) -> None:
    verify(store)
    store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[1], learned_from=KEY, read_through=0)
    other = Store.open(tmp_path / "stockthread.db")  # the command line's own connection, without the lock
    try:
        other.remove_alias(1)
    finally:
        other.close()
    assert store.aliases(PART) == ()
    store.learn_alias(
        OWNER, PART, "disc 40", 1, rivals=[1], learned_from=KEY, read_through=0
    )  # the same words may be taught again
    assert store.aliases(PART) == (Alias("disc 40", 1, rivals=frozenset({1})),)


def test_an_alias_an_admin_typed_has_no_source(store: Store, tmp_path: Path) -> None:
    raw = sqlite3.connect(tmp_path / "stockthread.db", isolation_level=None)
    raw.execute(
        "INSERT INTO aliases (kind, text, words, record, created_at, updated_at)"
        " VALUES ('location', 'yard', 'yard', 5, 'then', 'then')"
    )
    raw.close()
    (typed,) = store.stored_aliases()
    assert (typed.learned_from, typed.rivals) == (None, None)
    assert "typed by an admin" in alias_lines([typed])[1]
    assert store.remove_alias(typed.id) == typed
    events = [(actor, event) for _, actor, event, _, _ in store.audit(POST)]
    assert ("admin", "alias_removed") not in events  # no post taught it
    raw = sqlite3.connect(tmp_path / "stockthread.db")
    rows = raw.execute("SELECT actor, event, post_id, request_id FROM audit_log WHERE alias_id = ?", (typed.id,))
    assert rows.fetchall() == [("admin", "alias_removed", None, None)]
    raw.close()


def test_a_removed_alias_s_id_is_never_given_again(store: Store) -> None:
    """The audit log and `aliases remove` name aliases by id: a new alias never takes a removed one's (review)."""
    verify(store)
    store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[1], learned_from=KEY, read_through=0)
    store.learn_alias(OWNER, PART, "disc 50", 2, rivals=[2], learned_from=KEY, read_through=0)
    store.remove_alias(2)
    store.learn_alias(OWNER, PART, "red cable", 3, rivals=[3], learned_from=KEY, read_through=0)
    assert [(alias.id, alias.text) for alias in store.stored_aliases()] == [(1, "disc 40"), (3, "red cable")]


def test_the_list_quotes_text_from_slack() -> None:
    hostile = StoredAlias(
        3, PART, 'disc\x1b[2J\x7f\x9b\u202e "40" é', 1, "U0\x85SAM\U000e0041", None, KEY, "then", "now"
    )
    (heading, line, hint) = alias_lines([hostile])
    assert heading == "part aliases:"
    assert line.isprintable()
    assert line == (
        '    3  "disc\\u001b[2J\\u007f\\u009b\\u202e \\"40\\" é" -> part 1, posts by "U0\\u0085SAM\\udb40\\udc41" only;'
        " learned now from post C0STOCK 1727600000.000100"
    )
    assert "aliases remove" in hint
    assert alias_lines([]) == ["no aliases stored: one is learned when a confirmed plan is applied and verified"]


CONFIG = """
[slack]
workspace_id = "T0ACME"
app_id = "A0STOCK1"

[inventree]
url = "http://inventree.local:8000"

[storage]
database = "{database}"
"""


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-1111-2222-synthetic-bot-token")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-1-A0STOCK1-synthetic-app-token")
    monkeypatch.setenv("INVENTREE_TOKEN", "inv-synthetic-token-0123456789")
    written = tmp_path / "stockthread.toml"
    written.write_text(CONFIG.format(database=tmp_path / "stockthread.db"), encoding="utf-8")
    return written


def aliases(config: Path, *args: str) -> int:
    return cli.main(["aliases", *args[:1], "--config", str(config), *args[1:]])


def test_the_command_lists_exports_and_removes(store: Store, config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    verify(store)
    store.learn_alias(OWNER, PART, "Disc 40", 1, rivals=[1, 2], learned_from=KEY, read_through=0)
    store.learn_alias(OWNER, LOCATION, "my truck", 11, rivals=[11], learned_from=KEY, read_through=0, user="U0SAM")
    assert aliases(config, "list") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "part aliases:"
    assert out[1].startswith('    1  "Disc 40" -> part 1, everyone\'s posts; learned ')
    assert out[1].endswith(" from post C0STOCK 1727600000.000100")
    assert out[2] == "location aliases:"
    assert out[3].startswith('    2  "my truck" -> location 11, posts by "U0SAM" only; learned ')

    assert aliases(config, "export") == 0
    exported = json.loads(capsys.readouterr().out)
    assert (exported["format"], exported["version"]) == (EXPORT_FORMAT, 1)
    assert [(a["id"], a["kind"], a["text"], a["record"], a["user"], a["rivals"]) for a in exported["aliases"]] == [
        (1, "part", "Disc 40", 1, None, [1, 2]),
        (2, "location", "my truck", 11, "U0SAM", [11]),
    ]
    assert exported["aliases"][0]["learned_from"] == {
        "workspace": "T0ACME", "channel": "C0STOCK", "ts": "1727600000.000100", "revision": 1, "request": 1,
    }  # fmt: skip

    assert aliases(config, "remove", "2", "9") == 1  # 9 is no alias; 2 is still removed
    captured = capsys.readouterr()
    assert captured.out.startswith('removed 2  "my truck" -> location 11')
    assert captured.err == "ERROR no alias 9\n"
    assert [alias.id for alias in store.stored_aliases()] == [1]
    assert aliases(config, "remove", "1") == 0
    assert store.stored_aliases() == ()


def test_the_command_reads_but_never_creates_or_migrates(
    config: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for action in ("list",), ("export",), ("remove", "1"):
        assert aliases(config, *action) == 2
    assert not (tmp_path / "stockthread.db").exists()
    Store.open(tmp_path / "stockthread.db").close()
    raw = sqlite3.connect(tmp_path / "stockthread.db", isolation_level=None)
    raw.execute("DROP TABLE apply_buttons")  # back to schema 20, which `Store.open` would migrate
    raw.execute("DROP TABLE aliases")
    raw.execute("ALTER TABLE audit_log DROP COLUMN alias_id")
    raw.execute("PRAGMA user_version = 20")
    assert aliases(config, "remove", "1") == 2
    assert raw.execute("PRAGMA user_version").fetchone() == (20,)  # still not migrated
    assert raw.execute("SELECT count(*) FROM sqlite_schema WHERE name = 'aliases'").fetchone() == (0,)
    raw.close()
    assert capsys.readouterr().err.count("ERROR ") == 4
    assert cli.main(["aliases"]) == 0
    assert "list" in capsys.readouterr().out


def test_a_confirmation_never_takes_over_an_alias_an_admin_typed(store: Store) -> None:
    store._db.execute(
        "INSERT INTO aliases (kind, text, words, record, created_at, updated_at)"
        " VALUES ('part', 'Disc 40', 'disc 40', 1, '2026-10-01', '2026-10-01')"
    )
    verify(store)
    store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[1], learned_from=KEY, read_through=0)
    (typed,) = store.stored_aliases()
    assert (typed.text, typed.learned_from, typed.rivals) == ("Disc 40", None, None)  # an undo cannot forget it


def test_an_edit_of_a_post_forgets_every_alias_it_taught_and_no_other(store: Store, tmp_path: Path) -> None:
    other = PostKey(POST.workspace_id, POST.channel_id, "1700000999.000100")
    store.create_post(other)
    store.create_plan(other, "1" * 64, (Operation.TRANSFER,))
    verify(store)
    other_key = RequestKey(other, 1, 1)
    store.transition(other_key, expected=S.PLANNED, target=S.APPROVED, actor=Actor.HUMAN)
    marker = store.next_marker(other_key, AttemptKind.APPLY)
    store.begin_attempt(
        other_key, AttemptKind.APPLY, expected=S.APPROVED, actor=Actor.APPLIER, snapshot={},
        request_body={"items": [], "location": 4, "notes": write_notes(marker, "U0SAM")}, lock_owner=OWNER,
    )  # fmt: skip
    store.transition(other_key, expected=S.APPLYING, target=S.APPLIED, actor=Actor.APPLIER)
    store.transition(other_key, expected=S.APPLIED, target=S.VERIFIED, actor=Actor.APPLIER)
    store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[1], learned_from=KEY, read_through=0)
    store.learn_alias(OWNER, LOCATION, "my truck", 11, rivals=[11], learned_from=KEY, read_through=0, user="U0SAM")
    store.learn_alias(OWNER, PART, "disc 60", 2, rivals=[2], learned_from=other_key, read_through=0)
    store.note_revision(OWNER, POST, slack_event_id=None)
    assert [alias.text for alias in store.stored_aliases()] == ["disc 60"]
    with sqlite3.connect(tmp_path / "stockthread.db") as raw:
        events = raw.execute("SELECT actor FROM audit_log WHERE event = 'alias_forgotten'").fetchall()
    assert events == [("ingest",), ("ingest",)]


def test_no_alias_is_learned_from_a_reading_an_edit_came_after(store: Store) -> None:
    verify(store)
    store.note_revision(OWNER, POST, slack_event_id=None)
    through = store.post_changes_through(POST)
    with pytest.raises(StateConflict, match="edited or deleted after the lesson was read"):
        store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[1], learned_from=KEY, read_through=through - 1)
    assert store.stored_aliases() == ()
    store.learn_alias(OWNER, PART, "disc 40", 1, rivals=[1], learned_from=KEY, read_through=through)
    assert [alias.text for alias in store.stored_aliases()] == ["disc 40"]


def test_an_admin_adds_an_alias_that_replaces_a_learned_one_and_is_audited(store: Store, tmp_path: Path) -> None:
    """#174: an admin adds deliberate aliases by hand. One stands for everyone's posts, is never rechecked (no
    rivals), and takes over a learned alias for the same text as the matcher reads it."""
    verify(store)
    store.learn_alias(OWNER, PART, "patch cable", 1, rivals=[1, 2], learned_from=KEY, read_through=0)
    added = store.add_alias(PART, " patch cables ", 2)
    assert (added.text, added.record, added.user, added.rivals, added.learned_from) == (
        "patch cables",
        2,
        None,
        None,
        None,
    )
    assert store.aliases(PART) == (Alias("patch cables", 2),)
    with sqlite3.connect(tmp_path / "stockthread.db") as raw:
        events = raw.execute(
            "SELECT actor, event, alias_id, request_id IS NOT NULL FROM audit_log"
            " WHERE alias_id IS NOT NULL ORDER BY id"
        ).fetchall()
    assert events == [
        ("applier", "alias_learned", 1, True),
        ("admin", "alias_replaced", 1, True),
        ("admin", "alias_added", added.id, False),
    ]
    store.learn_alias(OWNER, PART, "patch cable", 1, rivals=[1], learned_from=KEY, read_through=0)
    assert store.aliases(PART) == (Alias("patch cables", 2),)  # no confirmation takes it over


def test_an_admin_alias_never_replaces_another_admin_alias_or_a_per_user_one(store: Store) -> None:
    verify(store)
    store.learn_alias(OWNER, LOCATION, "the yard", 11, rivals=[11], learned_from=KEY, read_through=0, user="U0SAM")
    store.add_alias(LOCATION, "the yard", 5)
    with pytest.raises(ValueError, match="alias 2 already names this text; `stockthread aliases remove 2` first"):
        store.add_alias(LOCATION, "The Yard", 6)
    assert [(alias.text, alias.record, alias.user) for alias in store.stored_aliases()] == [
        ("the yard", 11, "U0SAM"),
        ("the yard", 5, None),
    ]
    with pytest.raises(ValueError, match="at least one word"):
        store.add_alias(PART, " - ", 1)
    with pytest.raises(ValueError, match="at least one word"):
        store.add_alias(PART, "disc", 0)


ADMIN_CATALOG = Catalog(
    parts=(
        PartRecord(1, "D40", "Norvane Disc 40 Access Point"),
        PartRecord(2, "PSU-24", "Power Supply 24V"),
        PartRecord(3, "OLD", "Old Bridge", active=False),
        PartRecord(4, "TPL", "Bridge Template", template=True),
    ),
    locations=(
        LocationRecord(10, "Warehouse"),
        LocationRecord(11, "Warehouse/Main Stock"),
        LocationRecord(12, "Yard/Shelf A"),
        LocationRecord(13, "Shop/Shelf A"),
    ),
)


@pytest.fixture
def inventree(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """`aliases add` reads the catalog from InvenTree: here, ADMIN_CATALOG; the list counts the reads."""
    reads: list[int] = []

    def loader(settings: object) -> Callable[[], Catalog]:
        def read() -> Catalog:
            reads.append(1)
            return ADMIN_CATALOG

        return read

    monkeypatch.setattr(cli, "inventree_catalog", loader)
    return reads


def test_the_command_shows_what_it_would_add_and_stores_nothing_without_yes(
    store: Store, config: Path, inventree: list[int], capsys: pytest.CaptureFixture[str]
) -> None:
    verify(store)
    store.learn_alias(OWNER, PART, "ubiquity psus", 1, rivals=[1], learned_from=KEY, read_through=0)
    before = store.stored_aliases()
    assert aliases(config, "add", "part", "ubiquity psu", "2") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == 'would add "ubiquity psu" -> part 2, "Power Supply 24V" (IPN "PSU-24"), for everyone\'s posts'
    assert out[1].startswith('would replace the learned 1  "ubiquity psus" -> part 1, everyone\'s posts; learned ')
    assert out[2] == "nothing stored: run it again with --yes to store it"
    assert store.stored_aliases() == before
    assert aliases(config, "add", "part", "ubiquity psu", "2", "--yes") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith('added 2  "ubiquity psu" -> part 2')
    assert out[1].startswith('replaced the learned 1  "ubiquity psus" -> part 1')
    assert [(alias.text, alias.record) for alias in store.stored_aliases()] == [("ubiquity psu", 2)]


def test_the_command_adds_an_alias_an_admin_typed(
    store: Store, config: Path, inventree: list[int], capsys: pytest.CaptureFixture[str]
) -> None:
    assert aliases(config, "add", "part", "ubiquity psu", "2", "--yes") == 0
    out = capsys.readouterr().out.splitlines()
    assert (
        out[0]
        == 'added 1  "ubiquity psu" -> part 2, everyone\'s posts; typed by an admin: "Power Supply 24V" (IPN "PSU-24")'
    )
    assert "aliases remove <id>" in out[1]
    assert aliases(config, "add", "location", "back room", "11", "--yes") == 0
    assert 'location 11, everyone\'s posts; typed by an admin: "Warehouse/Main Stock"' in capsys.readouterr().out
    assert [(a.kind, a.text, a.record) for a in store.stored_aliases()] == [
        (PART, "ubiquity psu", 2),
        (LOCATION, "back room", 11),
    ]
    with_store = with_aliases(lambda: ADMIN_CATALOG, store)()
    assert match_part("ubiquity psus", with_store).record == 2
    assert match_part("ubiquity psus", with_store).rule is MatchRule.ALIAS


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (("part", "my psu", "2"), 'ERROR not an alias: "my" means another thing for another poster or on another day'),
        (("part", "40", "1"), "ERROR not an alias: it has no word with a letter other than an article"),
        (
            ("part", "psu", "9"),
            "ERROR not added: part 9 is not an active part in InvenTree (made inactive, or deleted)",
        ),
        (
            ("part", "psu", "3"),
            "ERROR not added: part 3 is not an active part in InvenTree (made inactive, or deleted)",
        ),
        (("part", "psu", "4"), "ERROR not added: part 4 is a template part (it holds no stock)"),
        (("location", "shop", "1"), "ERROR not added: location 1 is not a stock location in InvenTree"),
        (("part", "the", "1"), "ERROR not an alias: it has no word with a letter other than an article"),
        (
            ("location", "the vans", "11"),
            "ERROR not an alias: a vehicle word alone names another vehicle once there are two;"
            " give the vehicle's name or number",
        ),
        (
            ("part", "power supply 24v", "1"),
            "ERROR not added: it is the name of part 2, which the matcher tries before any alias",
        ),
        (
            ("part", "Power Supply 24V", "1"),
            "ERROR not added: it is the name of part 2, which the matcher tries before any alias",
        ),
        (
            ("location", "Main Stock", "10"),
            "ERROR not added: it is the name of location 11, which the matcher tries before any alias",
        ),
        (
            ("location", "Shelf A", "10"),
            "ERROR not added: it is the name of location 12, 13, which the matcher tries before any alias",
        ),
    ],
)
def test_the_command_refuses_an_alias_that_would_be_wrong_or_never_used(
    store: Store,
    config: Path,
    inventree: list[int],
    capsys: pytest.CaptureFixture[str],
    args: tuple[str, ...],
    error: str,
) -> None:
    assert aliases(config, "add", *args, "--yes") == 1
    assert capsys.readouterr().err == error + "\n"
    assert store.stored_aliases() == ()


def test_the_command_refuses_a_second_admin_alias_and_never_creates_a_database(
    config: Path, tmp_path: Path, inventree: list[int], capsys: pytest.CaptureFixture[str]
) -> None:
    assert aliases(config, "add", "part", "my psu", "2", "--yes") == 1  # the text is refused before anything is read
    assert aliases(config, "add", "part", "psu", "2", "--yes") == 2
    assert not (tmp_path / "stockthread.db").exists()
    assert inventree == []  # the database is looked for before InvenTree is read
    Store.open(tmp_path / "stockthread.db").close()
    assert aliases(config, "add", "part", "psu", "2", "--yes") == 0
    assert aliases(config, "add", "part", "PSU", "1") == 1  # refused in the dry run too
    assert capsys.readouterr().err.endswith(
        "ERROR not added: alias 1 already names this text; `stockthread aliases remove 1` first\n"
    )


def test_the_command_fails_with_2_when_inventree_cannot_be_read(
    store: Store, config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def loader(settings: object) -> Callable[[], Catalog]:
        def read() -> Catalog:
            raise InvenTreeError("the server answered 503")

        return read

    monkeypatch.setattr(cli, "inventree_catalog", loader)
    assert aliases(config, "add", "part", "psu", "2", "--yes") == 2
    assert capsys.readouterr().err == "ERROR the server answered 503\n"
    assert store.stored_aliases() == ()


def test_an_edit_of_a_post_never_forgets_an_alias_an_admin_typed(store: Store) -> None:
    verify(store)
    store.add_alias(PART, "ubiquity psu", 2)
    store.note_revision(OWNER, POST, slack_event_id=None)
    assert store.aliases(PART) == (Alias("ubiquity psu", 2),)
