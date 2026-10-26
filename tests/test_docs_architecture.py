"""docs/architecture.md maps every module, and the layout rules it states hold."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from stockthread.slack.catchup import SWEEP_SECONDS

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "stockthread"
DOC = (ROOT / "docs" / "architecture.md").read_text()


def _modules() -> set[str]:
    """Every module but a package `__init__.py` that holds only its docstring."""
    found = set()
    for path in PACKAGE.rglob("*.py"):
        body = ast.parse(path.read_text()).body
        if path.name == "__init__.py" and len(body) <= 1:
            continue
        found.add(path.relative_to(PACKAGE).as_posix())
    return found


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
    return names


def test_the_module_map_names_every_module_once() -> None:
    rows = re.findall(r"^\| `([^`]+\.py)` \|", DOC, re.MULTILINE)
    assert len(rows) == len(set(rows))
    assert set(rows) == _modules()


def test_every_module_the_text_names_exists() -> None:
    named = set(re.findall(r"`((?:core|llm|slack|store)/\w+\.py|\w+\.py)", DOC)) - {"__main__.py"}
    assert named <= _modules() | {"__main__.py"}


def test_the_core_imports_only_the_standard_library_and_itself() -> None:
    for path in (PACKAGE / "core").glob("*.py"):
        for name in _imports(path):
            top = name.split(".")[0]
            assert name.startswith("stockthread.core") or (top in sys.stdlib_module_names and top != "sqlite3"), (
                path.name,
                name,
            )


def test_only_apply_sends_through_the_writer() -> None:
    """`writer.send` is called in apply.py alone; the other modules that import the writer only open it."""
    for path in PACKAGE.rglob("*.py"):
        sends = [
            node
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "send"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "writer"
        ]
        assert not sends or path.name == "apply.py", path


def test_the_catch_up_interval_is_the_one_the_code_uses() -> None:
    assert f"then every {SWEEP_SECONDS // 60} minutes" in DOC


def test_the_links_resolve() -> None:
    for link in re.findall(r"\]\(([^)#]+)\)", DOC):
        assert (ROOT / "docs" / link).exists(), link
