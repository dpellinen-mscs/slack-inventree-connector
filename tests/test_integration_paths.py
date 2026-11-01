# SPDX-License-Identifier: MIT
"""The real-server job runs for every module its tests import: a change to one reaches a real InvenTree."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = (ROOT / ".github" / "workflows" / "integration.yml").read_text()


def _paths(section: str) -> list[str]:
    block = re.search(rf"^  {section}:\n(?:    .*\n|      .*\n)+", WORKFLOW, re.MULTILINE)
    assert block is not None, section
    return re.findall(r'^      - "([^"]+)"$', block[0], re.MULTILINE)


def _imported() -> set[str]:
    """Every stockthread module tests/integration imports by name, the bare package excepted."""
    modules: set[str] = set()
    for path in (ROOT / "tests" / "integration").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
            elif isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
    return {module for module in modules if module.startswith("stockthread.")}


def _source(module: str) -> str:
    """The file of `module`, relative to the repo. A package counts as its `db.py` when that holds its code."""
    base = ROOT / "src" / Path(*module.split("."))
    if base.is_dir():
        return str((base / "db.py").relative_to(ROOT))
    return str(base.with_suffix(".py").relative_to(ROOT))


def test_a_pull_request_and_a_push_watch_the_same_paths() -> None:
    assert _paths("pull_request") == _paths("push")


def test_every_module_the_integration_tests_import_triggers_them() -> None:
    assert _imported()
    missing = {_source(module) for module in _imported()} - set(_paths("pull_request"))
    assert not missing, f"add these to .github/workflows/integration.yml: {sorted(missing)}"


def test_every_named_path_exists() -> None:
    for path in _paths("pull_request"):
        if "*" not in path:
            assert (ROOT / path).exists(), path
