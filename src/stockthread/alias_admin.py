"""What an admin sees of the confirmed aliases (`stockthread aliases`, ADR 0012): a list, an export and a removal.

Alias text and user ids come from Slack, so they are printed quoted, with every character that is not printable
(control characters, DEL, C1 codes, bidi overrides) escaped: none reaches the terminal as itself.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from stockthread.store import AliasKind, StoredAlias

EXPORT_FORMAT = "stockthread-aliases"
EXPORT_VERSION = 1


def _quoted(text: str) -> str:
    quoted = json.dumps(text, ensure_ascii=False)
    return "".join(char if char.isprintable() else f"\\u{ord(char):04x}" for char in quoted)


def describe(alias: StoredAlias) -> str:
    """One alias on one line: its id, text, record, whose posts use it, and where it was learned."""
    whose = "everyone's posts" if alias.user is None else f"posts by {_quoted(alias.user)} only"
    if alias.learned_from is None:
        source = "typed by an admin"
    else:
        post = alias.learned_from.post
        source = f"learned {alias.updated_at} from post {post.channel_id} {post.ts}"
    return f"{alias.id:>5}  {_quoted(alias.text)} -> {alias.kind.value} {alias.record}, {whose}; {source}"


def alias_lines(aliases: Sequence[StoredAlias]) -> list[str]:
    """The lines of `stockthread aliases list`: part aliases, then location aliases."""
    if not aliases:
        return ["no aliases stored: one is learned when a confirmed plan is applied and verified"]
    lines: list[str] = []
    for kind in AliasKind:
        of_kind = [alias for alias in aliases if alias.kind is kind]
        if of_kind:
            lines.append(f"{kind.value} aliases:")
            lines.extend(describe(alias) for alias in of_kind)
    lines.append(
        "`stockthread trail <channel> <ts>` shows a post's trail; `stockthread aliases remove <id>` forgets one"
    )
    return lines


def export(aliases: Sequence[StoredAlias]) -> dict[str, Any]:
    """Every alias as JSON, for a backup or a review: `stockthread aliases export`."""
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "aliases": [
            {
                "id": alias.id,
                "kind": alias.kind.value,
                "text": alias.text,
                "record": alias.record,
                "user": alias.user,
                "rivals": None if alias.rivals is None else sorted(alias.rivals),
                "learned_from": None
                if alias.learned_from is None
                else {
                    "workspace": alias.learned_from.post.workspace_id,
                    "channel": alias.learned_from.post.channel_id,
                    "ts": alias.learned_from.post.ts,
                    "revision": alias.learned_from.revision,
                    "request": alias.learned_from.sequence,
                },
                "created_at": alias.created_at,
                "updated_at": alias.updated_at,
            }
            for alias in aliases
        ],
    }
