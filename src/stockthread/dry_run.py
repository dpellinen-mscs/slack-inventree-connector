# SPDX-License-Identifier: MIT
"""The dry-run record: what a reply decided, as the store keeps it for the audit trail (`Store.dry_run`), and what
the worker reads back from it: the reading that stands, whether a revision reads the post the same way, and the
extraction a revision that only adds answers keeps."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from stockthread.core.model import (
    CandidateMovement,
    Extraction,
    Flag,
    LineItem,
    Match,
    MessageInput,
    MovementType,
    PostFlag,
    ReasonCode,
)
from stockthread.core.resolve import SourcePick
from stockthread.slack.replies import MovementView, gate_reasons
from stockthread.store import DryRunOutcome
from stockthread.store.db import DryRunRecord


def standing_row(history: Sequence[DryRunRecord]) -> tuple[str | None, set[int]]:
    """The reading that stands and the revisions of the newest dry runs in a row that read the post that way; None
    and no revisions when the newest record kept no reading (a notice, a silent or failed one, an expired record)."""
    reading: str | None = None
    row: set[int] = set()
    for found in reversed(history):
        if found.outcome not in (DryRunOutcome.REPLIED, DryRunOutcome.UNCHANGED) or not found.result:
            break
        digest = reading_digest(found.result)
        if reading is not None and digest != reading:
            break
        reading = digest
        row.add(found.revision)
    return reading, row


def _picked(pick: SourcePick) -> list[dict[str, Any]]:
    return [{"item": take.item, "quantity": _number(take.quantity)} for take in pick.takes]


def _match(match: Match | None) -> dict[str, Any] | None:
    if match is None:
        return None
    return {
        "status": match.status.value,
        "rule": None if match.rule is None else match.rule.value,
        "record": match.record,
        "candidates": list(match.candidates),
    }


def _number(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def dry_run_record(
    extraction: Extraction,
    views: Sequence[MovementView],
    *,
    late: bool,
    poster: str,
    messages: Sequence[MessageInput],
    max_quantity: int,
    llm_unavailable: bool = False,
    llm_no_reading: bool = False,
) -> dict[str, Any]:
    """What the dry run decided, for the audit trail: candidates as extracted, matches (InvenTree ids and the
    rule), and every reason that kept a movement from "could apply". It quotes the post: stored, never logged."""
    return {
        "declined": extraction.declined,
        "llm_unavailable": llm_unavailable,  # declined by rule, and the model was never heard
        "llm_no_reading": llm_no_reading,  # declined by rule, and the model refused or did not finish
        "post_flags": sorted(flag.value for flag in extraction.post_flags),
        "late": late,
        "poster": poster,  # personal aliases match by poster
        "messages": [message.ts for message in messages],
        "max_quantity": max_quantity,
        "movements": [
            {
                "type": view.movement.movement_type.value,
                "flags": sorted(flag.value for flag in view.movement.flags),
                "source_text": view.movement.source_text,
                "source": _match(view.source),
                "destination_text": view.movement.destination_text,
                "destination": _match(view.destination),
                **{f"{side}_named": view.named[side] for side in ("source", "destination") if side in view.named},
                "items": [
                    {
                        "item_text": item.item_text,
                        "quantity": _number(item.quantity),
                        "unit": item.unit,
                        "pack_size": _number(item.pack_size),
                        "serials": list(item.serials),
                        "part": _match(part),
                        **({"named": view.named[line]} if line in view.named else {}),
                        # Only once stock was read: a record without it digests as records did before.
                        **({"stock": None if pick is None else _picked(pick)} if view.stock else {}),
                        # Only for a length in its part's own unit (`MovementView.units`), likewise.
                        **({"part_units": units} if units is not None else {}),
                    }
                    for line, (item, part, pick, units) in enumerate(
                        zip(
                            view.movement.line_items,
                            view.items,
                            view.stock or [None] * len(view.items),
                            view.units or [None] * len(view.items),
                            strict=True,
                        )
                    )
                ],
                "reasons": [
                    {"code": reason.code.value, "source": reason.source.value, "detail": reason.detail}
                    for reason in view.reasons
                ],
                "gate": sorted([code.value, detail] for code, detail in gate_reasons(view)),
            }
            for view in views
        ],
    }


def reading_of(result: Mapping[str, Any]) -> Any:
    """The movements, matches and reasons a record decided. Lateness is left out: it depends on when the post was
    read, not on what the thread says."""
    movements = json.loads(json.dumps(result.get("movements")))
    for movement in movements or ():
        movement["reasons"] = [r for r in movement["reasons"] if r["code"] != ReasonCode.LATE_INGEST.value]
        movement["gate"] = [g for g in movement["gate"] if g[0] != ReasonCode.LATE_INGEST.value]
    return movements


def reading_digest(result: Mapping[str, Any]) -> str:
    """What a record read, before any answer: its `reading`, or for a record without one (none applied an answer
    then) the digest of its movements."""
    stored = result.get("reading")
    if isinstance(stored, str):
        return stored
    canonical = json.dumps(reading_of(result), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def same_reading(standing: Mapping[str, Any], decided: Mapping[str, Any]) -> bool:
    """Whether a revision decided the same movements, matches and reasons as the reply that stands, with the same
    answers. A new answer always posts a reply, even one that changes nothing: it asks the next question."""

    def answered(result: Mapping[str, Any]) -> tuple[Any, ...]:
        return (result.get("answers") or [], result.get("ignored") or [], result.get("rejected"))

    return bool(standing) and reading_of(standing) == reading_of(decided) and answered(standing) == answered(decided)


def kept_reading(history: Sequence[DryRunRecord], read: Sequence[str], changes: int) -> Extraction | None:
    """The extraction of the newest dry run, when it read the same messages with the same changes as now: only
    answers are new. None otherwise, or when it kept no reading (a notice, a silent or failed one, an expired
    record)."""
    if not history:
        return None
    last = history[-1]
    result = last.result
    if (
        last.outcome not in (DryRunOutcome.REPLIED, DryRunOutcome.UNCHANGED)
        or not result
        or not last.extractor
        or last.changes_through != changes
        or result.get("messages") != list(read)
        or not result.get("movements")
    ):
        return None
    extracted = result.get("extracted") or [movement["type"] for movement in result["movements"]]
    return Extraction(
        last.extractor,
        tuple(
            CandidateMovement(
                MovementType(kind),
                tuple(
                    LineItem(
                        item["item_text"],
                        _decimal(item["quantity"]),
                        item["unit"],
                        _decimal(item["pack_size"]),
                        tuple(item["serials"]),
                    )
                    for item in movement["items"]
                ),
                movement["source_text"],
                movement["destination_text"],
                frozenset(Flag(flag) for flag in movement["flags"]),
            )
            for kind, movement in zip(extracted, result["movements"], strict=True)
        ),
        frozenset(PostFlag(flag) for flag in result["post_flags"]),
    )


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)
