# SPDX-License-Identifier: MIT
"""The audit trail of one post, for an operator (`stockthread trail`): what the connector saw, why it decided
what it decided, and who answered what (design section 8).

Read only: it never changes the database. By default it prints ids, codes and InvenTree primary keys, never
words of the post, so its output can be pasted into an issue. `text=True` adds the words each line was read
from, as stored. Every character that is not printable (control characters, DEL, C1 codes, bidi overrides), which
only words from Slack can hold, is printed escaped as `\\uXXXX`: none reaches the terminal as itself.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from stockthread.core.model import PostKey
from stockthread.printable import printable
from stockthread.store import Store

_PERMALINK = re.compile(r"/archives/(?P<channel>[A-Z0-9]+)/p(?P<seconds>\d{10})(?P<micros>\d{6})(?:[?#].*)?$")
_TS = re.compile(r"^\d{10}\.\d{6}$")


class TrailError(Exception):
    """The message cannot be found, or the reference to it cannot be read."""


def message_key(workspace_id: str, reference: str, ts: str | None = None) -> PostKey:
    """The key of the message a Slack permalink names, or of `reference` as a channel id with `ts`. A
    permalink's `thread_ts` query names the thread, not the message, so it is ignored."""
    if ts is None:
        found = _PERMALINK.search(reference)
        if found is None:
            raise TrailError("give a Slack message link, or a channel id and a message timestamp")
        return PostKey(workspace_id, found["channel"], f"{found['seconds']}.{found['micros']}")
    if not re.fullmatch(r"[CG][A-Z0-9]{2,}", reference) or not _TS.fullmatch(ts):
        raise TrailError("a channel id looks like C0123ABCD, and a timestamp like 1790000000.000100")
    return PostKey(workspace_id, reference, ts)


def post_trail(store: Store, message: PostKey, *, text: bool = False) -> list[str]:
    """The lines of the trail of the post that holds `message`, oldest first."""
    key = store.post_of(message)
    if key is None:
        raise TrailError("no post holds that message: it was not seen, or not in an allowlisted channel")
    lines = [
        f"post {key.channel_id} {key.ts} (workspace {key.workspace_id})",
        f"messages: {', '.join(store.post_messages(key))}",
    ]
    history = store.dry_run_history(key)
    if not history:
        lines.append("no dry run yet")
    for record in history:
        lines.append(
            f"dry run {record.revision}: {record.outcome.value}"
            f"{'' if record.extractor is None else f', read by {record.extractor}'}"
            f"{'' if record.reply_ts is None else f', posted {record.reply_ts}'}"
        )
        if record.result is None:
            lines.append("  no record (none was stored, or retention expired it)")
        else:
            lines.extend(f"  {line}" for line in _decided(record.result, text=text))
    for item in store.questions(key):
        question = item.question
        lines.append(
            f"question {item.id} (dry run {item.revision}): {question.get('ask')} for {question.get('reason')}"
            f"{_where(question)}{', approvers only' if item.approver_only else ''}: {item.status.value}"
        )
        answered = store.answer(item.id)
        if answered is not None:
            answer, user = answered
            lines.append(f"  answered by {user}: {_compact(answer)}")
    lines.append("audit log:")
    lines.extend(
        f"  {at} {actor} {event}{_states(before, after)}" for at, actor, event, before, after in store.audit(key)
    )
    return [printable(line) for line in lines]


def _decided(result: Mapping[str, Any], *, text: bool) -> list[str]:
    lines = []
    read = [
        name
        for name, on in (
            ("declined by the rules", result.get("declined")),
            ("the model was not reached", result.get("llm_unavailable")),
            ("the model gave no reading", result.get("llm_no_reading")),
            ("late", result.get("late")),
        )
        if on
    ]
    if result.get("post_flags"):
        read.append(f"post flags {', '.join(result['post_flags'])}")
    if read:
        lines.append("; ".join(read))
    for index, movement in enumerate(result.get("movements") or (), start=1):
        lines.append(
            f"movement {index}: {movement.get('type')}"
            f"{_flags(movement)}, from {_side(movement, 'source', text)}, to {_side(movement, 'destination', text)}"
        )
        for number, item in enumerate(movement.get("items") or (), start=1):
            quantity = item.get("quantity") or "?"
            unit = f" {item['unit']}" if item.get("unit") else ""
            said = item.get("item_text")
            words = f" {json.dumps(said, ensure_ascii=False)}" if text and said is not None else ""
            serials = f", serials {', '.join(item['serials'])}" if item.get("serials") else ""
            lines.append(f"  line {number}: {quantity}{unit} of{words} {_matched(item.get('part'), 'part')}{serials}")
            if "stock" in item:
                stock = item["stock"]
                picked = "none" if stock is None else ", ".join(f"item {t['item']} x {t['quantity']}" for t in stock)
                lines.append(f"    stock: {picked}")
        # The gate holds every reason that keeps the movement from "could apply": those the rules and the model
        # raised (`reasons`, with who raised them) and those the matches and missing fields show.
        sources: dict[tuple[str, str], list[str]] = {}
        for reason in movement.get("reasons") or ():
            sources.setdefault((reason["code"], reason["detail"]), []).append(reason["source"])
        gate = movement.get("gate") or ()
        if not gate:
            lines.append("  no review reason")
        for code, detail in gate:
            raised = f" ({', '.join(sorted(sources[code, detail]))})" if (code, detail) in sources else ""
            lines.append(f"  review: {code}{raised}{f': {detail}' if detail else ''}")
    answers = [
        f"{name} {', '.join(map(str, ids))}"
        for name, ids in (("applied", result.get("answers")), ("ignored", result.get("ignored")))
        if ids
    ]
    if result.get("rejected") is not None:
        answers.append(f"rejected by {result['rejected']}")
    if answers:
        lines.append(f"answers (question ids): {'; '.join(answers)}")
    return lines


def _flags(movement: Mapping[str, Any]) -> str:
    return f" [{', '.join(movement['flags'])}]" if movement.get("flags") else ""


def _side(movement: Mapping[str, Any], side: str, text: bool) -> str:
    words = movement.get(f"{side}_text")
    said = f"{json.dumps(words, ensure_ascii=False)} " if text and words is not None else ""
    return f"{said}{_matched(movement.get(side), 'location')}"


def _matched(match: Mapping[str, Any] | None, kind: str) -> str:
    if match is None:
        return "(not stated)"
    if match.get("record") is not None:
        return f"{kind} {match['record']} ({match.get('status')}, {match.get('rule')})"
    candidates = match.get("candidates") or ()
    listed = f": candidates {', '.join(map(str, candidates))}" if candidates else ""
    return f"(no {kind}, {match.get('status')}{listed})"


def _where(question: Mapping[str, Any]) -> str:
    parts = []
    if question.get("movement") is not None:
        parts.append(f"movement {question['movement'] + 1}")
    if question.get("line") is not None:
        parts.append(f"line {question['line'] + 1}")
    if question.get("side"):
        parts.append(str(question["side"]))
    if question.get("candidates"):
        parts.append(f"(choices {', '.join(map(str, question['candidates']))})")
    if question.get("detail"):
        parts.append(f"({question['detail']})")
    return f", {' '.join(parts)}" if parts else ""


def _states(before: str | None, after: str | None) -> str:
    return "" if before is None and after is None else f" {before or '-'} -> {after or '-'}"


def _compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(", ", ": "), ensure_ascii=False)
