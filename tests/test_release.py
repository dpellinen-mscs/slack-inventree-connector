# SPDX-License-Identifier: MIT
"""The version agrees in `pyproject.toml`, the package and the changelog (docs/releasing.md)."""

from __future__ import annotations

import datetime
import re
import tomllib
from pathlib import Path

from stockthread import __version__

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = (ROOT / "CHANGELOG.md").read_text()
RELEASED = re.compile(r"\[(\d+)\.(\d+)\.(\d+)\] - (\d{4}-\d{2}-\d{2})")


def _headings() -> list[str]:
    return re.findall(r"^## (.*)$", CHANGELOG, re.MULTILINE)


def _released() -> list[tuple[tuple[int, int, int], datetime.date]]:
    found = []
    for heading in _headings()[1:]:
        match = RELEASED.fullmatch(heading)
        assert match, f"not a release heading: {heading!r}"
        major, minor, patch, day = match.groups()
        found.append(((int(major), int(minor), int(patch)), datetime.date.fromisoformat(day)))
    return found


def test_the_package_and_pyproject_agree() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == __version__


def test_the_changelog_starts_with_unreleased() -> None:
    assert _headings()[0] == "[Unreleased]"


def test_releases_are_newest_first() -> None:
    released = _released()
    assert [version for version, _ in released] == sorted({version for version, _ in released}, reverse=True)
    assert [day for _, day in released] == sorted((day for _, day in released), reverse=True)


def test_the_version_is_the_newest_release() -> None:
    released = _released()
    newest = ".".join(map(str, released[0][0])) if released else "0.0.0"
    assert __version__ == newest
