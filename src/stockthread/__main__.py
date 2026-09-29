"""Command line entry point."""

import argparse
import json
import os
import sys
from pathlib import Path

from stockthread import __version__
from stockthread.config import ConfigError, load_settings

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
    args = parser.parse_args(argv)

    if args.command == "check-config":
        return check_config(args.config)
    parser.print_help()
    return 0


def check_config(path: Path) -> int:
    try:
        settings = load_settings(path, os.environ)
    except ConfigError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    print(json.dumps(settings.effective(), indent=2))
    print(settings.writes_line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
