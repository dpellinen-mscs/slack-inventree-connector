"""docs/configuration.md lists every setting of `config.Settings` with its default, and nothing else."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from stockthread.config import SECRET_ENV, Secrets, Settings

PAGE = Path(__file__).parent.parent / "docs" / "configuration.md"
ROW = re.compile(r"^\| `([a-z_.]+)` \| (.+?) \| ")
SECRET_ROW = re.compile(r"^\| `([A-Z_]+)` \| ")


def documented() -> dict[str, str]:
    """Setting -> its default column, from the table of settings."""
    lines = PAGE.read_text(encoding="utf-8").split("## Settings\n", 1)[1].split("\n## ", 1)[0].splitlines()
    return {match[1]: match[2] for line in lines if (match := ROW.match(line))}


def settings(model: type[BaseModel], prefix: str = "") -> dict[str, Any]:
    """Every leaf setting of `model`, by its dotted name, with its default, or None when it is required."""
    found: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        if model is Settings and name == "secrets":
            continue
        kind = field.annotation
        nested = next(
            (a for a in getattr(kind, "__args__", (kind,)) if isinstance(a, type) and issubclass(a, BaseModel)), None
        )
        if nested is not None:
            found.update(settings(nested, f"{prefix}{name}."))
        else:
            found[f"{prefix}{name}"] = None if field.is_required() else field.get_default(call_default_factory=True)
    return found


def toml(value: Any) -> str:
    """A default as the page writes it: TOML, in backticks."""
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, tuple | list):
        return "`[" + ", ".join(json.dumps(item) for item in value) + "]`"
    if isinstance(value, dict):
        return "`{}`" if not value else f"`{json.dumps(value)}`"
    return f"`{json.dumps(value)}`"


def test_every_setting_is_documented_and_nothing_else() -> None:
    assert sorted(documented()) == sorted(settings(Settings))


def test_every_default_is_the_code_s() -> None:
    page = documented()
    for name, default in settings(Settings).items():
        if default is None:
            assert page[name].startswith("required"), name
        else:
            assert page[name] == toml(default), name


def test_every_secret_variable_is_documented() -> None:
    lines = PAGE.read_text(encoding="utf-8").split("## Secrets", 1)[1].split("\n## ", 1)[0].splitlines()
    assert sorted(match[1] for line in lines if (match := SECRET_ROW.match(line))) == sorted(SECRET_ENV)
    assert sorted(field for field, _ in SECRET_ENV.values()) == sorted(Secrets.model_fields)
