"""`stockthread trail`: one post's dry runs, every review reason, its questions and the audit log, read only."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from stockthread import __main__ as cli
from stockthread.core.model import PostKey
from stockthread.store import DryRunOutcome, SchemaTooNew, SchemaTooOld, Store
from stockthread.store import db as store_db
from stockthread.trail import TrailError, message_key, post_trail

OWNER = "instance-a"
WORKSPACE = "T0ACME0001"
POST = PostKey(WORKSPACE, "C0STOCK001", "1790000000.000100")

SPLIT = {
    "declined": False,
    "post_flags": ["needs_review"],
    "late": True,
    "movements": [
        {
            "type": "transfer",
            "flags": [],
            "source_text": "main stock",
            "source": {"status": "matched", "rule": "exact", "record": 10, "candidates": [10]},
            "destination_text": None,
            "destination": None,
            "items": [
                {
                    "item_text": "Disc 40",
                    "quantity": "5",
                    "unit": None,
                    "serials": [],
                    "part": {"status": "matched", "rule": "alias", "record": 1, "candidates": [1]},
                    "stock": [{"item": 44, "quantity": "3"}, {"item": 45, "quantity": "2"}],
                }
            ],
            "reasons": [
                {"code": "quantity_doubt", "source": "model", "detail": ""},
                {"code": "quantity_doubt", "source": "rule", "detail": ""},
                {"code": "stock_split", "source": "rule", "detail": "line 1: 2 items"},
            ],
            "gate": [["destination_missing", ""], ["quantity_doubt", ""], ["stock_split", "line 1: 2 items"]],
        }
    ],
}


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


def read(path: Path, message: PostKey = POST, *, text: bool = False) -> list[str]:
    reader = Store.open_read_only(path)
    try:
        return post_trail(reader, message, text=text)
    finally:
        reader.close()


@pytest.mark.parametrize(
    ("reference", "ts", "expected"),
    [
        ("https://acme-fiber.slack.com/archives/C0STOCK001/p1790000000000100", None, "1790000000.000100"),
        (
            "https://acme-fiber.slack.com/archives/C0STOCK001/p1790000030000200?thread_ts=1790000000.000100&cid=C0STOCK001",
            None,
            "1790000030.000200",
        ),  # a reply's link names the reply; its thread's post is found from it
        ("C0STOCK001", "1790000000.000100", "1790000000.000100"),
    ],
)
def test_a_message_is_named_by_its_link_or_its_channel_and_timestamp(
    reference: str, ts: str | None, expected: str
) -> None:
    assert message_key(WORKSPACE, reference, ts) == PostKey(WORKSPACE, "C0STOCK001", expected)


@pytest.mark.parametrize(
    ("reference", "ts"),
    [
        ("https://acme-fiber.slack.com/archives/C0STOCK001", None),
        ("C0STOCK001", None),
        ("C0STOCK001", "1790000000"),
        ("C0STOCK001/x", "1790000000.000100"),
        ("U0SAM0001", "1790000000.000100"),
    ],
)
def test_anything_else_is_refused(reference: str, ts: str | None) -> None:
    with pytest.raises(TrailError):
        message_key(WORKSPACE, reference, ts)


def test_every_reason_of_the_gate_is_shown_with_who_raised_it(store: Store, path: Path) -> None:
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, "1790000020.000100", extractor="llm:m", result=SPLIT)
    lines = read(path)
    assert lines[2:10] == [
        "dry run 1: replied, read by llm:m, posted 1790000020.000100",
        "  late; post flags needs_review",
        "  movement 1: transfer, from location 10 (matched, exact), to (not stated)",
        "    line 1: 5 of part 1 (matched, alias)",
        "      stock: item 44 x 3, item 45 x 2",
        "    review: destination_missing",  # shown by the view, raised by no extractor
        "    review: quantity_doubt (model, rule)",
        "    review: stock_split (rule): line 1: 2 items",
    ]
    assert "Disc" not in "\n".join(lines)
    assert "main stock" not in "\n".join(lines)
    assert '5 of "Disc 40" part 1' in "\n".join(read(path, text=True))


def test_a_post_without_a_dry_run_or_a_record_says_so(store: Store, path: Path) -> None:
    assert "no dry run yet" in read(path)
    store.finish_dry_run(OWNER, POST, DryRunOutcome.SILENT)
    assert read(path)[2:4] == ["dry run 1: silent", "  no record (none was stored, or retention expired it)"]


def test_any_message_of_the_post_finds_it(store: Store, path: Path) -> None:
    reply = PostKey(WORKSPACE, POST.channel_id, "1790000030.000200")
    store.place_message(OWNER, reply, user_id="U0SAM0001", thread_ts=POST.ts, window_seconds=5)
    assert read(path, reply)[:2] == [
        f"post {POST.channel_id} {POST.ts} (workspace {WORKSPACE})",
        f"messages: {POST.ts}, {reply.ts}",
    ]
    with pytest.raises(TrailError):
        read(path, PostKey(WORKSPACE, POST.channel_id, "1790000099.000100"))


def test_the_read_only_store_changes_nothing_and_refuses_writes(store: Store, path: Path) -> None:
    reader = Store.open_read_only(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.create_post(PostKey(WORKSPACE, POST.channel_id, "1790000099.000100"))
    finally:
        reader.close()


def test_the_read_only_store_wants_a_database_at_this_versions_schema(tmp_path: Path, path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Store.open_read_only(tmp_path / "none.db")
    older = tmp_path / "older.db"
    sqlite3.connect(older).close()
    with pytest.raises(SchemaTooOld):
        Store.open_read_only(older)
    Store.open(path).close()
    with sqlite3.connect(path) as raw:
        raw.execute(f"PRAGMA user_version = {len(store_db._migrations()) + 1}")
    with pytest.raises(SchemaTooNew):
        Store.open_read_only(path)


CONFIG = """
[slack]
workspace_id = "T0ACME0001"
app_id = "A0STOCK1"

[inventree]
url = "http://inventree.local:8000"

[storage]
database = "{database}"
"""

ENV = {
    "SLACK_BOT_TOKEN": "xoxb-1111-2222-synthetic-bot-token",
    "SLACK_APP_TOKEN": "xapp-1-A0STOCK1-synthetic-app-token",
    "INVENTREE_TOKEN": "inv-synthetic-token-0123456789",
}


@pytest.fixture
def config(tmp_path: Path, path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    written = tmp_path / "stockthread.toml"
    written.write_text(CONFIG.format(database=path), encoding="utf-8")
    return written


def run(config: Path, *args: str) -> int:
    return cli.main(["trail", "--config", str(config), *args])


def test_the_command_prints_the_trail(store: Store, config: Path, capsys: pytest.CaptureFixture[Any]) -> None:
    link = "https://acme-fiber.slack.com/archives/C0STOCK001/p1790000000000100"
    assert run(config, link) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"post {POST.channel_id} {POST.ts} (workspace {WORKSPACE})\n")
    assert "planner post_created" in out
    for secret in ENV.values():
        assert secret not in out


@pytest.mark.usefixtures("store")
def test_the_command_says_why_it_printed_nothing(
    config: Path, path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[Any]
) -> None:
    assert run(config, "C0STOCK001", "1790000099.000100") == 1  # no such post
    assert run(config, "C0STOCK001") == 2  # no timestamp
    assert run(config, "--text", "C0STOCK001", POST.ts) == 0
    config.write_text(CONFIG.format(database=tmp_path / "none.db"), encoding="utf-8")
    assert run(config, "C0STOCK001", POST.ts) == 2
    assert not (tmp_path / "none.db").exists()  # nothing is created
    assert capsys.readouterr().err.count("ERROR ") == 3
