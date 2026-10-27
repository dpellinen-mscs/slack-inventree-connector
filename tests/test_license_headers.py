# SPDX-License-Identifier: MIT
"""Every tracked Python and shell file names its license in an SPDX line, and it is the one in LICENSE."""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADER = "# SPDX-License-Identifier: MIT"


def _sources() -> list[str]:
    git = shutil.which("git")
    assert git, "this test lists the tracked files with git"
    args = [git, "ls-files", "*.py", "*.sh"]
    listed = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=True)  # noqa: S603 (fixed args)
    return listed.stdout.split()


def _header_line(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    return lines[0] if lines else ""


def test_every_source_file_starts_with_the_spdx_line() -> None:
    sources = _sources()
    assert len(sources) > 100, "git ls-files found too few files to be this repo"
    missing = [name for name in sources if _header_line((ROOT / name).read_text()) != HEADER]
    assert missing == [], f"add {HEADER!r} as the first line (after any shebang): {missing}"


def test_the_header_reads_the_shebang_first() -> None:
    assert _header_line(f"#!/usr/bin/env bash\n{HEADER}\n") == HEADER
    assert _header_line(f"{HEADER}\n#!/usr/bin/env bash\n") == HEADER
    assert _header_line("#!/usr/bin/env bash\n") == ""
    assert _header_line("") == ""


def test_the_spdx_id_matches_license_and_pyproject() -> None:
    assert (ROOT / "LICENSE").read_text().startswith("MIT License\n")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["license"] in ("MIT", {"text": "MIT"})
