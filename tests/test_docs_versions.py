# SPDX-License-Identifier: MIT
"""docs/versions.md names the versions CI tests: the InvenTree matrix, the tested API range, and Python."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from stockthread.inventree import MAX_API_VERSION, MIN_API_VERSION

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "docs" / "versions.md").read_text()
INTEGRATION = (ROOT / ".github" / "workflows" / "integration.yml").read_text()
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()


def _list(workflow: str, key: str) -> set[str]:
    [found] = re.findall(rf"^\s+{key}: \[([^\]]*)\]", workflow, re.MULTILINE)
    return {value.strip().strip('"') for value in found.split(",")}


def _rows() -> list[tuple[str, str, str]]:
    return re.findall(r"^\| (\d+\.\d+\.\d+) \| (\d+) \| (\w+) \|", DOC, re.MULTILINE)


def test_the_inventree_rows_are_the_integration_matrix() -> None:
    included = set(re.findall(r"- inventree: \"([\d.]+)\"\s+db: (\w+)", INTEGRATION))
    matrix = {(version, db) for version in _list(INTEGRATION, "inventree") for db in _list(INTEGRATION, "db")}
    named = {"sqlite": "SQLite", "postgres": "PostgreSQL"}
    assert {(version, database) for version, _, database in _rows()} == {
        (version, named[db]) for version, db in matrix | included
    }


def test_the_api_range_is_the_one_the_connector_checks() -> None:
    assert f"API {MIN_API_VERSION} to {MAX_API_VERSION}" in DOC
    apis = {int(api) for _, api, _ in _rows()}
    assert min(apis) == MIN_API_VERSION
    assert max(apis) == MAX_API_VERSION


def test_python_is_the_ci_matrix_and_the_package_floor() -> None:
    pythons = sorted(_list(CI, "python"), key=lambda version: tuple(map(int, version.split("."))))
    assert f"CI runs the unit tests and the eval corpus on {' and '.join(pythons)}." in DOC
    floor = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["requires-python"]
    assert f"Python {floor.removeprefix('>=')} or newer" in DOC


def test_the_manifests_named_exist() -> None:
    for path in re.findall(r"`(manifests/[^`]+)`", DOC):
        assert (ROOT / path).is_file(), path
