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
from stockthread.config import ConfigError, load_settings
from stockthread.inventree import InvenTreeError
from stockthread.run import check_setup, run
from stockthread.setup_check import exit_code

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
    args = parser.parse_args(argv)

    if args.command == "check-config":
        return check_config(args.config)
    if args.command == "check-setup":
        return check_setup_command(args.config)
    if args.command == "run":
        return run_command(args.config)
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


if __name__ == "__main__":
    raise SystemExit(main())
