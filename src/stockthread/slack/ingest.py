"""Ingest: decide what to do with one Slack payload, before anything else sees it (design section 4).

Pure: the transport (Socket Mode or HTTP, through Bolt) verifies the request and hands over
the parsed payload. This module checks the workspace, the app, the channel allowlist, the
message subtype, bots and duplicates, and turns what is left into plain values. Payloads are
untrusted input, so a field of the wrong type drops the payload instead of raising.

Every drop has a reason, so the audit trail can say why a post got no reply.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from stockthread.config import SlackSettings

POST_SUBTYPES = frozenset({None, "file_share", "thread_broadcast"})
"""Message subtypes a post can come from. Joins, topic changes, tombstones and the like are not posts."""
ALLOWED_SUBTYPES = POST_SUBTYPES | {"message_changed", "message_deleted"}
ACTIONS = frozenset({"apply", "reject", "answer", "undo"})
"""The buttons the connector posts. A click on anything else is dropped."""
APPROVER_ACTIONS = frozenset({"apply"})
"""Buttons only an approver may click. For the others (answers, corrections, undo) the poster may click too,
and only the stored button row knows the poster, so review checks those (design section 4, "Authorization")."""


class DropReason(StrEnum):
    BOT = "bot"
    CHANNEL_NOT_ALLOWED = "channel_not_allowed"
    WRONG_WORKSPACE = "wrong_workspace"
    WRONG_APP = "wrong_app"
    SUBTYPE_NOT_ALLOWED = "subtype_not_allowed"
    DUPLICATE_EVENT = "duplicate_event"
    NOT_AUTHORIZED = "not_authorized"
    NOT_A_MESSAGE = "not_a_message"
    UNCHANGED = "unchanged"
    """A message_changed without an edit, such as a link preview being added."""
    UNKNOWN_ACTION = "unknown_action"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class Drop:
    reason: DropReason


@dataclass(frozen=True)
class Challenge:
    """The URL verification handshake of the HTTP transport."""

    challenge: str


@dataclass(frozen=True)
class SlackMessage:
    channel: str
    ts: str
    user: str
    text: str
    thread_ts: str | None = None
    """The post this message replies to, if it is a thread reply."""
    file_ids: tuple[str, ...] = ()
    edited_ts: str | None = None


@dataclass(frozen=True)
class NewMessage:
    event_id: str
    message: SlackMessage


@dataclass(frozen=True)
class Edited:
    """An edit of an earlier message. It never writes by itself: it makes a new plan revision, or a review item."""

    event_id: str
    message: SlackMessage


@dataclass(frozen=True)
class Deleted:
    """A deletion of an earlier message. It never undoes stock by itself."""

    event_id: str
    channel: str
    ts: str


@dataclass(frozen=True)
class Revised:
    """An edit or deletion the checks cannot read in full: a tombstone (a deleted thread root), an edit without
    a stamp, or one without a user. Kept all the same, so the post it changes is never read from its old text."""

    event_id: str
    channel: str
    ts: str


@dataclass(frozen=True)
class Click:
    """A button click. Review checks the value against the stored button, channel and message, and the user."""

    user: str
    channel: str
    message_ts: str
    thread_ts: str | None
    action_id: str
    value: str


Decision = Drop | Challenge | NewMessage | Edited | Deleted | Revised | Click


class SeenEvents(Protocol):
    def first_time(self, event_id: str) -> bool:
        """Record the event id, and say whether it was new. Slack retries an event with the same id.

        Record it durably, before Slack is acknowledged: nothing else remembers an accepted event.
        """


class RecentEvents:
    """Event ids seen by this process, the most recent `size`. For tests: the listener uses the durable queue."""

    def __init__(self, size: int = 10_000) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._size = size

    def first_time(self, event_id: str) -> bool:
        if event_id in self._seen:
            self._seen.move_to_end(event_id)
            return False
        self._seen[event_id] = None
        if len(self._seen) > self._size:
            self._seen.popitem(last=False)
        return True


class _Malformed(Exception):
    pass


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise _Malformed
    return value


def _optional(value: Any) -> str | None:
    return None if value is None else _text(value)


def _object(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _Malformed
    return value


def unwrap_socket(envelope: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The payload of a Socket Mode envelope for events or interactivity, or None for other envelopes."""
    if envelope.get("type") not in ("events_api", "interactive"):
        return None
    payload = envelope.get("payload")
    return payload if isinstance(payload, Mapping) else None


def ingest_event(payload: object, settings: SlackSettings, seen: SeenEvents) -> Decision:
    """Decide what to do with an Events API payload.

    An accepted event is recorded in `seen` at once; a drop is never recorded. An edit or deletion that the
    checks cannot read in full is accepted as `Revised`. So `seen` must be the durable
    queue (a retry is then run from the queue, not from Slack), or a failed handling loses Slack's retry.
    """
    try:
        decision = _event(_object(payload), settings)
    except _Malformed:
        decision = Drop(DropReason.MALFORMED)
    if isinstance(decision, Drop) and decision.reason in _MAY_HIDE_A_REVISION:
        decision = _revised(payload, settings) or decision
    if isinstance(decision, NewMessage | Edited | Deleted | Revised) and not seen.first_time(decision.event_id):
        return Drop(DropReason.DUPLICATE_EVENT)
    return decision


def ingest_action(payload: object, settings: SlackSettings) -> Decision:
    """Decide what to do with an interactivity payload (a button click)."""
    try:
        return _action(_object(payload), settings)
    except _Malformed:
        return Drop(DropReason.MALFORMED)


def _is_bot(message: Mapping[str, Any]) -> bool:
    return message.get("subtype") == "bot_message" or any(
        message.get(key) is not None for key in ("bot_id", "bot_profile", "app_id")
    )


def _event(payload: Mapping[str, Any], settings: SlackSettings) -> Decision:
    kind = payload.get("type")
    if kind == "url_verification":
        return Challenge(_text(payload.get("challenge")))
    if kind != "event_callback":
        return Drop(DropReason.NOT_A_MESSAGE)
    if payload.get("team_id") != settings.workspace_id:
        return Drop(DropReason.WRONG_WORKSPACE)
    if payload.get("api_app_id") != settings.app_id:
        return Drop(DropReason.WRONG_APP)
    event = _object(payload.get("event"))
    if event.get("type") != "message":
        return Drop(DropReason.NOT_A_MESSAGE)
    channel = _text(event.get("channel"))
    if channel not in settings.channels:
        return Drop(DropReason.CHANNEL_NOT_ALLOWED)
    subtype = _optional(event.get("subtype"))
    inner = _object(event.get("message")) if subtype == "message_changed" else event
    previous = event.get("previous_message")
    if _is_bot(event) or _is_bot(inner) or (previous is not None and _is_bot(_object(previous))):
        return Drop(DropReason.BOT)
    if subtype not in ALLOWED_SUBTYPES or (inner is not event and _optional(inner.get("subtype")) not in POST_SUBTYPES):
        return Drop(DropReason.SUBTYPE_NOT_ALLOWED)  # also an edit that turns into a tombstone or a join
    for part in (event, inner):
        # In a shared channel, the poster may belong to another organization.
        team = part.get("user_team", part.get("team"))
        if team is not None and team != settings.workspace_id:
            return Drop(DropReason.WRONG_WORKSPACE)
    event_id = _text(payload.get("event_id"))
    if subtype == "message_deleted":
        return Deleted(event_id, channel, _text(event.get("deleted_ts")))
    message = _message(channel, inner)
    if subtype == "message_changed":
        if message.edited_ts is None or (previous is not None and _same_post(message, _object(previous))):
            return Drop(DropReason.UNCHANGED)
        return Edited(event_id, message)
    return NewMessage(event_id, message)


_MAY_HIDE_A_REVISION = frozenset({DropReason.MALFORMED, DropReason.SUBTYPE_NOT_ALLOWED, DropReason.UNCHANGED})
"""Drops that come after the workspace, app, channel and bot checks, so a payload from elsewhere stays dropped."""
_SLACK_TS = re.compile(r"\d{1,10}\.\d{6}")


def revised_message(payload: object, settings: SlackSettings) -> tuple[str, str] | None:
    """The channel and ts of the message that an event edits or deletes, or None. A lenient reading, for
    events the strict checks drop as well as the ones they accept.

    An edit counts when the text or the files differ from before, when the message became a tombstone (a
    deleted thread root), or when the event does not say what was there before. A link preview or a new
    thread reply changes neither.
    """
    if not isinstance(payload, Mapping) or payload.get("type") != "event_callback":
        return None
    if payload.get("team_id") != settings.workspace_id or payload.get("api_app_id") != settings.app_id:
        return None
    event = payload.get("event")
    if not isinstance(event, Mapping):
        return None
    channel, subtype = event.get("channel"), event.get("subtype")
    if event.get("type") != "message" or not isinstance(channel, str) or channel not in settings.channels:
        return None
    previous = event.get("previous_message")
    if subtype == "message_deleted":
        ts = event.get("deleted_ts") or (previous.get("ts") if isinstance(previous, Mapping) else None)
    elif subtype == "message_changed":
        inner = event.get("message")
        if not isinstance(inner, Mapping):
            return None
        ts = inner.get("ts")
        unchanged = isinstance(previous, Mapping) and _shown(inner) == _shown(previous)
        if unchanged and inner.get("subtype") != "tombstone":
            return None
    else:
        return None
    if not isinstance(ts, str) or not _SLACK_TS.fullmatch(ts):
        return None
    return channel, ts


def _revised(payload: object, settings: SlackSettings) -> Revised | None:
    found = revised_message(payload, settings)
    event_id = payload.get("event_id") if isinstance(payload, Mapping) else None
    if found is None or not isinstance(event_id, str) or not event_id:
        return None
    return Revised(event_id, *found)


def _shown(message: Mapping[str, Any]) -> tuple[object, list[object]]:
    files = message.get("files")
    shown = [file.get("id") for file in files if isinstance(file, Mapping)] if isinstance(files, list) else []
    return message.get("text"), shown


def _same_post(message: SlackMessage, previous: Mapping[str, Any]) -> bool:
    """True when nothing a reader sees changed: a thread reply or link preview on a post edited earlier.

    Slack keeps the old `edited` block on every later message_changed, so the stamp alone is not enough.
    """
    try:
        before = _message(message.channel, previous)
    except _Malformed:
        return False
    return (before.edited_ts, before.text, before.file_ids) == (message.edited_ts, message.text, message.file_ids)


def _message(channel: str, event: Mapping[str, Any]) -> SlackMessage:
    files = event.get("files", ())
    if not isinstance(files, list | tuple):
        raise _Malformed
    edited = event.get("edited")
    return SlackMessage(
        channel=channel,
        ts=_text(event.get("ts")),
        user=_text(event.get("user")),
        text=_text(event.get("text", "")),
        thread_ts=_optional(event.get("thread_ts")),
        file_ids=tuple(_text(_object(file).get("id")) for file in files),
        edited_ts=_optional(_object(edited).get("ts")) if edited is not None else None,
    )


def _action(payload: Mapping[str, Any], settings: SlackSettings) -> Decision:
    if payload.get("type") != "block_actions":
        return Drop(DropReason.NOT_A_MESSAGE)
    if _object(payload.get("team")).get("id") != settings.workspace_id:
        return Drop(DropReason.WRONG_WORKSPACE)
    if payload.get("api_app_id") != settings.app_id:
        return Drop(DropReason.WRONG_APP)
    channel = _text(_object(payload.get("channel")).get("id"))
    if channel not in settings.channels:
        return Drop(DropReason.CHANNEL_NOT_ALLOWED)
    clicker = _object(payload.get("user"))
    user = _text(clicker.get("id"))
    if clicker.get("team_id", settings.workspace_id) != settings.workspace_id:
        return Drop(DropReason.WRONG_WORKSPACE)  # a user of another organization in a shared channel
    actions = payload.get("actions")
    if not isinstance(actions, list) or len(actions) != 1:
        raise _Malformed
    action = _object(actions[0])
    action_id = _text(action.get("action_id"))
    if action_id not in ACTIONS or action.get("type") != "button":
        return Drop(DropReason.UNKNOWN_ACTION)
    if action_id in APPROVER_ACTIONS and user not in settings.approvers:
        return Drop(DropReason.NOT_AUTHORIZED)
    message = _object(payload.get("message"))
    container = _object(payload.get("container"))
    if (
        container.get("type") != "message"
        or container.get("channel_id") != channel
        or container.get("message_ts") != message.get("ts")
    ):
        raise _Malformed  # a click in a modal, or a payload whose parts disagree
    return Click(
        user=user,
        channel=channel,
        message_ts=_text(message.get("ts")),
        thread_ts=_optional(message.get("thread_ts")),
        action_id=action_id,
        value=_text(action.get("value")),
    )
