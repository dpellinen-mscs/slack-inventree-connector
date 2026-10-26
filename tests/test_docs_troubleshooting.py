"""docs/troubleshooting.md quotes log lines; each must still be one the connector can write."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from stockthread.slack.ingest import DropReason

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "docs" / "troubleshooting.md").read_text()
LOG_PREFIXES = ("slack ", "post ", "stock writes are ", "cannot start", "another instance", "catch-up:", "vehicles:")
ELIDED = "\x00"
"""What the page writes as "...": any text the log line holds there."""


def quoted_log_lines() -> list[str]:
    return [text for text in re.findall(r"`([^`]+)`", DOC) if text.startswith(LOG_PREFIXES)]


def log_patterns() -> list[re.Pattern[str]]:
    """Every string constant in the source as a pattern: `%s`, `%d` and the like match any text, as does "...". An
    f-string's replacement fields match any text too."""
    patterns = []
    for path in sorted((ROOT / "src" / "stockthread").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.JoinedStr):
                parts = [p.value if isinstance(p, ast.Constant) else ELIDED for p in node.values]
                text = "".join(str(part) for part in parts)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
            else:
                continue
            escaped = re.sub(r"%(?:\\\.)?\d*[sdf]", ".*", re.escape(text).replace(re.escape(ELIDED), ".*"))
            if len(re.sub(r"\.\*", "", escaped)) >= 12 and not escaped.startswith(".*"):  # "%s" alone fits anything
                patterns.append(re.compile(escaped))
    return patterns


def test_the_page_quotes_log_lines() -> None:
    assert len(quoted_log_lines()) >= 15


def test_each_dropped_reason_is_one_ingest_gives() -> None:
    reasons = re.findall(r"`slack (?:event|interactive) dropped: (\w+)`", DOC)
    assert reasons
    assert set(reasons) <= {reason.value for reason in DropReason}


def test_each_quoted_log_line_is_written_by_the_source() -> None:
    """A quoted line, with "..." standing for what varies, is what some log call's text gives."""
    patterns = log_patterns()
    missing = []
    for line in quoted_log_lines():
        if line.startswith("slack "):
            continue  # "slack %s dropped: %s": the reason is checked against DropReason
        concrete = line.replace("...", "x")
        if not any(pattern.fullmatch(concrete) for pattern in patterns):
            missing.append(line)
    assert missing == []
