"""Command line entry point. It only reports the version until the service exists."""

import argparse

from stockthread import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stockthread", description="Slack to InvenTree stock movements.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
