"""Catch-up: find the messages Slack never delivered (design section 4, "Catch-up after downtime").

Slack does not replay the events of a time the connector was down or disconnected. A sweep reads the history
of every allowlisted channel, and the replies of the threads that moved, and stores each message the connector
does not know as an event in the durable queue, in the shape of the event Slack would have sent. From there it
is an event like any other: the worker runs the ingest checks, groups it and answers it. The event id starts
with `history:`, and a post that holds such a message carries the review reason `late_ingest`.

A sweep never reads further back than `slack.catch_up_hours`, nor than the first sweep of the channel, so a new
installation does not answer what was posted before it. It also notes the edit of a message whose post still
waits for its dry run. It does not see deletions: a deleted message is simply absent from the history.

Known limits. A reply under a root older than `slack.catch_up_hours` is not found. A message that joins a
post after its dry run (a thread root found here after its reply was answered alone, for one) gets no second
reply until plan revisions exist (M2), as a late thread reply does not. The mark is this host's time: a clock
more than a few minutes ahead of Slack's can hide a reply from the next sweep.

Nothing here needs the instance lock: a sweep only stores what it read from Slack, as the listener does. It
runs on its own thread (`run.py`), so a slow Slack call holds back nothing else. Message text is never logged.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from slack_sdk.errors import SlackApiError

from stockthread.config import SlackSettings
from stockthread.core.grouping import seconds
from stockthread.core.model import PostKey
from stockthread.slack.ingest import SLACK_TS, Edited, NewMessage, Revised, ingest_event
from stockthread.slack.listener import Enqueue, dump
from stockthread.store import Store

log = logging.getLogger(__name__)

EVENT_ID_PREFIX = "history:"
SWEEP_SECONDS = 15 * 60
"""Between two sweeps. A sweep also runs right after the connection to Slack is made."""
SETTLE_SECONDS = 120
"""A later sweep leaves the newest messages alone for this long: their events may still be on their way, and
a message taken from the history is answered as late."""
OVERLAP_SECONDS = 5 * 60
"""A thread is read again when its newest reply is this close to the point the last sweep ended at."""
PAGE_SIZE = 200
MAX_PAGES = 250
"""For one listing. A channel with more messages than this in the window is not marked as swept."""


class HistoryClient(Protocol):
    """The two Web API calls a sweep makes (slack_sdk's `WebClient`)."""

    def conversations_history(
        self, *, channel: str, oldest: str, latest: str, limit: int, cursor: str | None
    ) -> Any: ...

    def conversations_replies(
        self, *, channel: str, ts: str, oldest: str, latest: str, limit: int, cursor: str | None
    ) -> Any: ...


class _Stopped(Exception):
    """A stop was asked for in the middle of a channel."""


class _Incomplete(Exception):
    """A listing did not end, or an answer was not in Slack's form: the channel is swept again later."""


def is_catch_up(event_id: str) -> bool:
    """Whether an event was made from the channel history. Slack's own event ids start with `Ev`."""
    return event_id.startswith(EVENT_ID_PREFIX)


def history_event(settings: SlackSettings, channel: str, message: Mapping[str, Any]) -> dict[str, Any]:
    """A message from the history, as the event Slack would have sent for it."""
    return {
        "type": "event_callback",
        "team_id": settings.workspace_id,
        "api_app_id": settings.app_id,
        "event_id": f"{EVENT_ID_PREFIX}{channel}:{message['ts']}",
        "event": {**message, "type": "message", "channel": channel},
    }


def changed_event(settings: SlackSettings, channel: str, message: Mapping[str, Any], stamp: str) -> dict[str, Any]:
    """The edit of a message, as far as the history shows it: the message as it is now, not as it was."""
    return {
        "type": "event_callback",
        "team_id": settings.workspace_id,
        "api_app_id": settings.app_id,
        "event_id": f"{EVENT_ID_PREFIX}{channel}:{message['ts']}:changed:{stamp}",
        "event": {"type": "message", "subtype": "message_changed", "channel": channel, "message": dict(message)},
    }


@dataclass
class CatchUp:
    settings: SlackSettings
    store: Store
    """A connection of the thread that sweeps."""
    client: HistoryClient
    clock: Callable[[], datetime]
    """Must return an aware datetime."""
    stopping: Callable[[], bool] = lambda: False
    """Checked between channels and between threads, so a stop does not wait for a whole sweep."""

    def sweep(self, latest: Decimal | None = None) -> int:
        """Read every allowlisted channel up to `latest` (by default, SETTLE_SECONDS ago). Returns how many
        events were stored. A channel that cannot be read is logged and left for the next sweep."""
        hours = self.settings.catch_up_hours
        if hours <= 0:
            return 0
        now = Decimal(str(self.clock().timestamp()))
        latest = now - SETTLE_SECONDS if latest is None else latest
        stored = 0
        for channel in self.settings.channels:
            if self.stopping():
                break
            try:
                found = self._sweep_channel(channel, now - hours * 3600, latest)
            except SlackApiError as error:
                # Only Slack's error code: the whole response is not ours to log.
                code = error.response.get("error", "an error")
                hint = ": invite the app to the channel" if code == "not_in_channel" else ""
                log.warning("catch-up: channel %s: Slack answered %s%s", channel, code, hint)
                continue
            except _Incomplete as error:
                log.warning("catch-up: channel %s: %s", channel, error)
                continue
            except _Stopped:
                break
            except Exception as error:
                # The network, the disk, or an answer cut short. The class only: a message could quote a post.
                log.warning("catch-up: channel %s: failed (%s)", channel, type(error).__name__)
                continue
            if found:
                log.info("catch-up: channel %s: %d messages or edits Slack had not delivered", channel, found)
            stored += found
        return stored

    def _sweep_channel(self, channel: str, window_start: Decimal, latest: Decimal) -> int:
        workspace = self.settings.workspace_id
        upto = _ts(latest)
        mark = self.store.catch_up_mark(workspace, channel)
        if mark is None:
            self.store.mark_caught_up(workspace, channel, upto)
            log.info("catch-up: channel %s: watched from now on; earlier posts are left alone", channel)
            return 0
        since, swept_to = seconds(mark[0]), seconds(mark[1])
        if latest <= swept_to:
            return 0
        floor = max(window_start, since)
        # The listing reaches behind the channel's first sweep, for the thread roots there: a reply made
        # since is read, under a root that is not. Only what is newer than `floor` is ever stored.
        bounds = {"channel": channel, "oldest": _ts(window_start), "latest": upto}
        stored = 0
        threads: list[str] = []
        for message in _pages(
            lambda cursor: self.client.conversations_history(**bounds, limit=PAGE_SIZE, cursor=cursor)
        ):
            stored += self._offer(channel, message, floor, latest)
            if _moved_since(message, swept_to - OVERLAP_SECONDS):
                threads.append(message["ts"])
        within = {"channel": channel, "oldest": _ts(floor), "latest": upto}
        for root in threads:
            if self.stopping():
                raise _Stopped  # the channel is not marked: the threads left are read by the next sweep
            replies = _pages(
                lambda cursor, root=root: self.client.conversations_replies(  # type: ignore[misc]
                    **within, ts=root, limit=PAGE_SIZE, cursor=cursor
                )
            )
            for reply in replies:  # the root comes with them; it is offered twice and stored once
                stored += self._offer(channel, reply, floor, latest)
        self.store.mark_caught_up(workspace, channel, upto)
        return stored

    def _offer(self, channel: str, message: Mapping[str, Any], floor: Decimal, latest: Decimal) -> int:
        """Store the message as an event unless the connector has it, or the ingest checks drop it."""
        if not floor < seconds(message["ts"]) <= latest:
            return 0
        key = PostKey(self.settings.workspace_id, channel, message["ts"])
        if self.store.message_placed(key):
            return self._offer_change(key, message)
        payload = history_event(self.settings, channel, message)
        decision = ingest_event(payload, self.settings, Enqueue(self.store, dump(payload)))
        return 1 if isinstance(decision, NewMessage) else 0

    def _offer_change(self, key: PostKey, message: Mapping[str, Any]) -> int:
        """The message is known. If its post still waits for its dry run and the history shows another edit
        than the stored event does (or a tombstone: a deleted thread root), store that edit as an event: the
        worker then withholds the post, as it does for an edit Slack delivers."""
        post = self.store.waiting_post_of(key)
        if post is None:
            return 0
        now = _edit_stamp(message)
        tombstone = message.get("subtype") == "tombstone"
        if not tombstone and (now is None or now == self._stored_stamp(post, key.ts)):
            return 0
        payload = changed_event(self.settings, key.channel_id, message, "tombstone" if tombstone else str(now))
        decision = ingest_event(payload, self.settings, Enqueue(self.store, dump(payload)))
        return 1 if isinstance(decision, Edited | Revised) else 0

    def _stored_stamp(self, post: PostKey, ts: str) -> str | None:
        """The edit stamp of the message in the event it was stored from."""
        for stored_ts, body in self.store.post_bodies(post):
            if stored_ts != ts:
                continue
            try:
                payload = json.loads(body) if body else None
            except ValueError:
                return None
            event = payload.get("event") if isinstance(payload, Mapping) else None
            return _edit_stamp(event) if isinstance(event, Mapping) else None
        return None


def _ts(value: Decimal) -> str:
    return format(value, ".6f")


def _edit_stamp(message: Mapping[str, Any]) -> str | None:
    edited = message.get("edited")
    stamp = edited.get("ts") if isinstance(edited, Mapping) else None
    return stamp if isinstance(stamp, str) else None


def _moved_since(message: Mapping[str, Any], point: Decimal) -> bool:
    """Whether the message is a thread root with a reply newer than `point`. A root that does not say when
    its newest reply was made counts."""
    if not message.get("reply_count") and "latest_reply" not in message:
        return False
    newest = message.get("latest_reply")
    if not isinstance(newest, str) or not SLACK_TS.fullmatch(newest):
        return True
    return seconds(newest) > point


def _pages(call: Callable[[str | None], Any]) -> Iterator[Mapping[str, Any]]:
    """Every message of a paged listing. Raises _Incomplete rather than pass on half a listing as whole."""
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        response = call(cursor)
        messages = response.get("messages")
        if not isinstance(messages, list):
            raise _Incomplete("an answer without a list of messages")
        for message in messages:
            if not isinstance(message, Mapping) or not isinstance(message.get("ts"), str):
                raise _Incomplete("a message without a timestamp")
            if not SLACK_TS.fullmatch(message["ts"]):
                raise _Incomplete("a timestamp not in Slack's form")
            yield message
        metadata = response.get("response_metadata")
        cursor = metadata.get("next_cursor") if isinstance(metadata, Mapping) else None
        if not response.get("has_more") or not isinstance(cursor, str) or not cursor:
            if response.get("has_more"):
                raise _Incomplete("more messages, and no cursor to read them with")
            return
    raise _Incomplete(f"more than {MAX_PAGES * PAGE_SIZE} messages in the window: lower slack.catch_up_hours")
