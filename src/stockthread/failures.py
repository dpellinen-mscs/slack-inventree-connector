# SPDX-License-Identifier: MIT
"""How the worker sorts the errors it catches: one that may pass by itself (`is_transient`), an outage of what it reads
or writes through (`is_outage`), the wait a rate-limited Slack call asks for, and the short codes and descriptions
it records and logs. A code or description never holds an error's message unless the connector wrote it."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from stockthread.inventree import InvenTreeError
from stockthread.plan_record import NotRebuilt
from stockthread.store import StateConflict


def retry_after(error: Exception, most: int) -> int:
    """The seconds a rate-limited Slack call asks to wait (its Retry-After header), at most `most`, or 0. The worker
    waits between steps and never inside a call, so the instance lock keeps its heartbeat."""
    headers: Any = getattr(getattr(error, "response", None), "headers", None) or {}
    try:
        stated = next(value for name, value in dict(headers).items() if str(name).lower() == "retry-after")
        return min(max(int(stated), 0), most)
    except (StopIteration, TypeError, ValueError):
        return 0


def is_outage(error: Exception) -> bool:
    """A failure of what the worker reads or writes through, not of the request or receipt itself: InvenTree, the
    network, the disk. It is likely to fail every other one too, so they all wait."""
    return isinstance(error, InvenTreeError | TimeoutError) or is_transient(error)


def described(error: Exception) -> str:
    """The class, and for the connector's own errors their message (keys, states and fixed text only)."""
    if isinstance(error, NotRebuilt | ValueError | AssertionError | StateConflict):
        return f"{type(error).__name__}: {error}"
    return type(error).__name__


def is_transient(error: Exception) -> bool:
    """A failure that may pass by itself: the database busy or locked, a full disk, an I/O or network error. Any
    other error is a bug or a payload that will fail the same way again."""
    return isinstance(error, sqlite3.OperationalError | OSError)


def error_code(error: Exception) -> str:
    """A short outcome code for an error: its class name only, never its message."""
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(error).__name__).lower()
    return "error_" + re.sub(r"[^a-z0-9_]", "", name)[:58]


def slack_code(error: Exception) -> str:
    """Slack's error code when Slack answered with one (`is_archived`, `channel_not_found`), else `error_code`."""
    response: Any = getattr(error, "response", None)
    try:
        code = response.get("error") if response is not None else None
    except (AttributeError, TypeError):
        code = None
    return code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", code) else error_code(error)
