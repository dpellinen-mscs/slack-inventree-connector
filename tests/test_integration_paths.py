# SPDX-License-Identifier: MIT
"""The real-server job runs for every module its tests import: a change to one reaches a real InvenTree."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = (ROOT / ".github" / "workflows" / "integration.yml").read_text()

# What decides a real stock write: its quantity, its source, its request, and whether it may go at all.
WRITE_PATH = tuple(
    f"stockthread.{name}"
    for name in (
        "applier",
        "apply",
        "gate",
        "inverse",
        "picking",
        "places",
        "plan",
        "plan_record",
        "precondition",
        "readback",
        "views",
        "writer",
    )
)
# `config` imports this for an endpoint check. No integration test reads a post through a model, so a change here would
# spend six jobs on nothing they exercise; what a model reads still passes `views` and `gate` before any write.
READING_ONLY = ("stockthread.llm",)


def _paths(section: str) -> list[str]:
    block = re.search(rf"^  {section}:\n(?:    .*\n|      .*\n)+", WORKFLOW, re.MULTILINE)
    assert block is not None, section
    return re.findall(r'^      - "([^"]+)"$', block[0], re.MULTILINE)


def _is_module(name: str) -> bool:
    base = ROOT / "src" / Path(*name.split("."))
    return base.is_dir() or base.with_suffix(".py").exists()


def _imports(path: Path) -> set[str]:
    """Every stockthread module `path` imports, however written: `from stockthread import x` names module `x`."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            named = {f"{node.module}.{alias.name}" for alias in node.names}
            modules.update({name for name in named if _is_module(name)} or {node.module})
    return {module for module in modules if module.startswith("stockthread.")}


def _imported() -> set[str]:
    """Every stockthread module tests/integration imports."""
    modules: set[str] = set()
    for path in (ROOT / "tests" / "integration").rglob("*.py"):
        modules |= _imports(path)
    return modules


def _write_path() -> set[str]:
    """`WRITE_PATH` and all it imports, short of `READING_ONLY`."""
    seen: set[str] = set()
    waiting = list(WRITE_PATH)
    while waiting:
        module = waiting.pop()
        if module in seen or module.startswith(READING_ONLY):
            continue
        seen.add(module)
        base = ROOT / "src" / Path(*module.split("."))
        waiting.extend(_imports(base / "__init__.py" if base.is_dir() else base.with_suffix(".py")))
    return seen


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


def test_every_module_a_real_write_depends_on_triggers_them() -> None:
    missing = {_source(module) for module in _write_path()} - set(_paths("pull_request"))
    assert not missing, f"add these to .github/workflows/integration.yml: {sorted(missing)}"


def test_every_named_path_exists() -> None:
    for path in _paths("pull_request"):
        if "*" not in path:
            assert (ROOT / path).exists(), path
