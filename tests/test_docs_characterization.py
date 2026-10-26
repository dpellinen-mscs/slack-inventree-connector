"""docs/design.md section 11 names every characterization test, and every name it gives is a test."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESIGN = (ROOT / "docs" / "design.md").read_text()


def _section() -> str:
    start = DESIGN.index("### Characterization tests")
    return DESIGN[start : DESIGN.index("\n## 12.", start)]


def _defined() -> set[str]:
    names = set()
    for path in (ROOT / "tests" / "integration").glob("test_*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test_char_"):
                names.add(node.name)
    return names


def test_the_section_names_every_characterization_test() -> None:
    named = set(re.findall(r"`(test_char_\w+)`", _section()))
    assert named == _defined()


def test_the_table_names_each_test_once() -> None:
    rows = re.findall(r"^\| `(test_char_\w+)` \|", _section(), re.MULTILINE)
    assert rows
    assert len(rows) == len(set(rows))
