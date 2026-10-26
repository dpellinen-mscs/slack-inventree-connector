"""Command line entry point."""

import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
from pathlib import Path

from stockthread import __version__
from stockthread.alias_admin import (
    alias_lines,
    describe,
    export,
    named,
    quoted,
    refused,
    rivals,
    shadowed,
    unmatchable,
)
from stockthread.config import ConfigError, load_settings
from stockthread.core.matching import same_words
from stockthread.inventree import InvenTreeError
from stockthread.run import check_setup, inventree_catalog, run
from stockthread.setup_check import exit_code
from stockthread.store import AliasKind, SchemaTooNew, SchemaTooOld, Store
from stockthread.trail import TrailError, message_key, post_trail

DEFAULT_CONFIG = "stockthread.toml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stockthread", description="Slack to InvenTree stock movements.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")
    check = commands.add_parser(
        "check-config", help="validate the settings file and the secrets, and print the effective settings"
    )
    check.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("STOCKTHREAD_CONFIG", DEFAULT_CONFIG)),
        help=f"settings file (default: $STOCKTHREAD_CONFIG or {DEFAULT_CONFIG})",
    )
    setup = commands.add_parser(
        "check-setup",
        help="read InvenTree and the local database, and report what to fix before the first dry run",
    )
    setup.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("STOCKTHREAD_CONFIG", DEFAULT_CONFIG)),
        help=f"settings file (default: $STOCKTHREAD_CONFIG or {DEFAULT_CONFIG})",
    )
    serve = commands.add_parser("run", help="connect to Slack and post dry runs until stopped (Ctrl-C)")
    serve.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("STOCKTHREAD_CONFIG", DEFAULT_CONFIG)),
        help=f"settings file (default: $STOCKTHREAD_CONFIG or {DEFAULT_CONFIG})",
    )
    trail = commands.add_parser(
        "trail",
        help="print what the connector saw and decided about one post, and who answered what (reads the database)",
    )
    trail.add_argument("message", help="a Slack link to any message of the post, or its channel id")
    trail.add_argument("ts", nargs="?", help="the message timestamp, when MESSAGE is a channel id")
    trail.add_argument("--text", action="store_true", help="also print the words each line was read from")
    trail.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("STOCKTHREAD_CONFIG", DEFAULT_CONFIG)),
        help=f"settings file (default: $STOCKTHREAD_CONFIG or {DEFAULT_CONFIG})",
    )
    aliases = commands.add_parser("aliases", help="list, export, add or remove aliases (reads the database)")
    alias_actions = aliases.add_subparsers(dest="action")
    config = argparse.ArgumentParser(add_help=False)
    config.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("STOCKTHREAD_CONFIG", DEFAULT_CONFIG)),
        help=f"settings file (default: $STOCKTHREAD_CONFIG or {DEFAULT_CONFIG})",
    )
    alias_actions.add_parser("list", parents=[config], help="print every alias, and the post it was learned from")
    alias_actions.add_parser("export", parents=[config], help="print every alias as JSON")
    remove = alias_actions.add_parser(
        "remove", parents=[config], help="forget aliases: the next post is matched as if they were never learned"
    )
    remove.add_argument("ids", nargs="+", type=int, metavar="ID", help="an alias id, as `aliases list` prints it")
    add = alias_actions.add_parser(
        "add",
        parents=[config],
        help="type an alias by hand: posts that say TEXT name RECORD, for everyone (checked against InvenTree)",
    )
    add.add_argument("kind", choices=[kind.value for kind in AliasKind], help="what the text names")
    add.add_argument("text", help='the words posts use, e.g. "ubiquity psu"')
    add.add_argument("record", type=int, help="the part or stock location id, as in its InvenTree page's address")
    add.add_argument("--yes", action="store_true", help="store it; without this, only show what would be stored")
    args = parser.parse_args(argv)

    if args.command == "check-config":
        return check_config(args.config)
    if args.command == "check-setup":
        return check_setup_command(args.config)
    if args.command == "run":
        return run_command(args.config)
    if args.command == "trail":
        return trail_command(args.config, args.message, args.ts, text=args.text)
    if args.command == "aliases":
        if args.action is None:
            aliases.print_help()
            return 0
        if args.action == "add":
            return add_alias_command(args.config, AliasKind(args.kind), args.text, args.record, store=args.yes)
        return aliases_command(args.config, args.action, getattr(args, "ids", []))
    parser.print_help()
    return 0


def run_command(path: Path) -> int:
    try:
        settings = load_settings(path, os.environ)
    except ConfigError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop = threading.Event()
    install_stop_handlers(stop)
    return run(settings, stop)


def install_stop_handlers(stop: threading.Event) -> None:
    """The first SIGINT, SIGTERM or SIGHUP asks for a clean stop; a second one stops at once.

    `stop.set()` runs on its own thread: the main thread may hold the event's lock inside `stop.wait()`,
    and a signal handler that waits for that lock would never return.
    """

    def handler(signum: int, _frame: object) -> None:
        if stop.is_set():
            signal.signal(signum, signal.SIG_DFL)
            raise KeyboardInterrupt
        threading.Thread(target=stop.set, daemon=True).start()

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, handler)


def check_config(path: Path) -> int:
    try:
        settings = load_settings(path, os.environ)
    except ConfigError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    print(json.dumps(settings.effective(), indent=2))
    print(settings.writes_line())
    print(settings.llm_line())
    return 0


def check_setup_command(path: Path) -> int:
    """Print one line per finding; 1 when something must be fixed, 2 when the check could not run."""
    try:
        settings = load_settings(path, os.environ)
    except ConfigError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    try:
        findings = check_setup(settings)
    except (InvenTreeError, sqlite3.Error) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    for finding in findings:
        print(f"{finding.level.value.upper():5} {finding.text}")
    return exit_code(findings)


def trail_command(path: Path, reference: str, ts: str | None, *, text: bool) -> int:
    """Print the trail of the post that holds the message; 1 when there is none, 2 when it cannot be read."""
    try:
        settings = load_settings(path, os.environ)
    except ConfigError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    try:
        key = message_key(settings.slack.workspace_id, reference, ts)
        store = Store.open_read_only(settings.storage.database)
    except (TrailError, FileNotFoundError, SchemaTooNew, SchemaTooOld, sqlite3.Error) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    try:
        lines = post_trail(store, key, text=text)
    except TrailError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
    print("\n".join(lines))
    return 0


def aliases_command(path: Path, action: str, ids: list[int]) -> int:
    """List, export or remove aliases; 1 when an id names no alias, 2 when the database cannot be read."""
    try:
        settings = load_settings(path, os.environ)
    except ConfigError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    try:
        # Read-only first: a missing database is not created, and one of another schema is not migrated.
        store = Store.open_read_only(settings.storage.database)
        if action == "remove":
            store.close()
            store = Store.open(settings.storage.database)
    except (FileNotFoundError, SchemaTooNew, SchemaTooOld, sqlite3.Error) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    try:
        if action == "list":
            print("\n".join(alias_lines(store.stored_aliases())))
            return 0
        if action == "export":
            print(json.dumps(export(store.stored_aliases()), indent=2))  # ASCII: Slack text is escaped
            return 0
        missing = False
        for alias_id in ids:
            try:
                print(f"removed {describe(store.remove_alias(alias_id)).lstrip()}")
            except KeyError:
                print(f"ERROR no alias {alias_id}", file=sys.stderr)
                missing = True
        return 1 if missing else 0
    except sqlite3.Error as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()


def add_alias_command(path: Path, kind: AliasKind, text: str, record: int, *, store: bool) -> int:
    """Show, and with `store` store, an alias an admin typed (#174, ADR 0021), once InvenTree says its record can be
    matched; 1 when it is refused, 2 when the database or InvenTree cannot be read. Without `store` nothing changes:
    the record's name is shown first, so a wrong id shows before any post uses it."""
    try:
        settings = load_settings(path, os.environ)
    except ConfigError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    if why := refused(kind, text):
        print(f"ERROR not an alias: {why}", file=sys.stderr)
        return 1
    try:
        reader = Store.open_read_only(settings.storage.database)  # never create or migrate one
        try:
            replaced = [
                alias
                for alias in reader.stored_aliases()
                if alias.kind is kind and alias.user is None and same_words(alias.text, text)
            ]
        finally:
            reader.close()
        catalog = inventree_catalog(settings)()
    except (FileNotFoundError, SchemaTooNew, SchemaTooOld, sqlite3.Error, InvenTreeError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    stale = unmatchable(catalog, kind, record)
    why = f"{kind.value} {record} {stale}" if stale else shadowed(catalog, kind, text)
    if why is None and any(alias.learned_from is None for alias in replaced):
        typed = next(alias for alias in replaced if alias.learned_from is None)
        why = f"alias {typed.id} already names this text; `stockthread aliases remove {typed.id}` first"
    if why is not None:
        print(f"ERROR not added: {why}", file=sys.stderr)
        return 1
    target = f"{kind.value} {record}, {named(catalog, kind, record)}"
    also = rivals(catalog, kind, text, record)
    if not store:
        print(f"would add {quoted(text.strip())} -> {target}, for everyone's posts")
        if also:
            print(also)
        for alias in replaced:
            print(f"would replace the learned {describe(alias).lstrip()}")
        print("nothing stored: run it again with --yes to store it")
        return 0
    try:
        writer = Store.open(settings.storage.database)
    except (SchemaTooNew, SchemaTooOld, sqlite3.Error) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    try:
        added = writer.add_alias(kind, text, record)
    except ValueError as exc:  # another admin added the same text meanwhile
        print(f"ERROR not added: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    finally:
        writer.close()
    print(f"added {describe(added).lstrip()}: {named(catalog, kind, record)}")
    if also:
        print(also)
    for alias in replaced:
        print(f"replaced the learned {describe(alias).lstrip()}")
    print("posts read from now on use it; `stockthread aliases remove <id>` forgets it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
