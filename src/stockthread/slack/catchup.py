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

A channel's events are stored together, once its history and its threads are read (up to MAX_PAGES pages are
held in memory meanwhile). The worker answers a post from the history as soon as the queue is empty, so an
event stored while its siblings or replies were still being read would be answered without them.

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
from stockthread.slack.ingest import SLACK_TS, ingest_event
from stockthread.slack.listener import dump
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


class _Accepted:
    """`ingest.SeenEvents` for one message of a sweep: if the ingest checks accept it, it joins the events
    that are stored together once the channel is read. The store then leaves out the events it already has."""

    def __init__(self, events: list[tuple[str, str]], body: str) -> None:
        self._events = events
        self._body = body

    def first_time(self, event_id: str) -> bool:
        self._events.append((event_id, self._body))
        return True


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
        # Nothing is stored before the whole channel is read: see `Store.store_sweep`.
        found: dict[str, Mapping[str, Any]] = {}
        threads: list[str] = []
        for message in _pages(
            lambda cursor: self.client.conversations_history(**bounds, limit=PAGE_SIZE, cursor=cursor)
        ):
            if floor < seconds(message["ts"]) <= latest:
                found[message["ts"]] = message
            if _moved_since(message, swept_to - OVERLAP_SECONDS):
                threads.append(message["ts"])
        within = {"channel": channel, "oldest": _ts(floor), "latest": upto}
        unread: list[str] = []
        for index, root in enumerate(threads):
            if self.stopping():
                raise _Stopped  # nothing is stored and the channel is not marked: the next sweep reads it
            try:
                replies = {
                    reply["ts"]: reply
                    for reply in _pages(
                        lambda cursor, root=root: self.client.conversations_replies(  # type: ignore[misc]
                            **within, ts=root, limit=PAGE_SIZE, cursor=cursor
                        )
                    )
                    if floor < seconds(reply["ts"]) <= latest
                }
            except (SlackApiError, _Incomplete) as error:
                code = error.response.get("error", "an error") if isinstance(error, SlackApiError) else str(error)
                log.warning("catch-up: channel %s: a thread could not be read (%s)", channel, code)
                # Slack asks for a pause: the threads not read yet wait for the next sweep too.
                unread += threads[index:] if code == "ratelimited" else [root]
                if code == "ratelimited":
                    break
                continue
            for ts, reply in replies.items():  # the root comes with them, a second time
                found.setdefault(ts, reply)
        # A thread that could not be read is left out whole, so its root is not answered without its replies.
        # The rest of the channel is stored, and the mark stays where it is: the next sweep reads the thread.
        left_out = set(unread)
        accepted: list[tuple[str, str]] = []
        for ts in sorted(found, key=seconds):  # oldest first, as Slack would have delivered them
            if ts not in left_out and found[ts].get("thread_ts") not in left_out:
                self._offer(channel, found[ts], accepted)
        return self.store.store_sweep(workspace, channel, None if unread else upto, accepted)

    def _offer(self, channel: str, message: Mapping[str, Any], accepted: list[tuple[str, str]]) -> None:
        """Accept the message as an event unless the connector has it, or the ingest checks drop it."""
        key = PostKey(self.settings.workspace_id, channel, message["ts"])
        if self.store.message_placed(key):
            payload = self._change(key, message)
            if payload is None:
                return
        else:
            payload = history_event(self.settings, channel, message)
        ingest_event(payload, self.settings, _Accepted(accepted, dump(payload)))

    def _change(self, key: PostKey, message: Mapping[str, Any]) -> dict[str, Any] | None:
        """The message is known. If the history shows another edit than the stored event and the noted edits
        do (or a tombstone: a deleted thread root), that edit as an event: the worker notes it as it does an
        edit Slack delivers, and reads the post as edited, in its dry run or its next revision."""
        post = self.store.post_of(key)
        if post is None:
            return None
        now = _edit_stamp(message)
        tombstone = message.get("subtype") == "tombstone"
        if not tombstone and (now is None or now in self._known_stamps(post, key.ts)):
            return None
        return changed_event(self.settings, key.channel_id, message, "tombstone" if tombstone else str(now))

    def _known_stamps(self, post: PostKey, ts: str) -> set[str]:
        """The edit stamps of the message in the event it was stored from and in its noted edits: an edit
        Slack delivered is not noted a second time from the history."""
        bodies = [body for stored_ts, body in self.store.post_bodies(post) if stored_ts == ts]
        bodies += self.store.post_change_bodies(post, self.store.post_changes_through(post)).get(ts, [])
        stamps: set[str] = set()
        for body in bodies:
            try:
                payload = json.loads(body) if body else None
            except ValueError:
                continue
            event = payload.get("event") if isinstance(payload, Mapping) else None
            if isinstance(event, Mapping) and event.get("subtype") == "message_changed":
                event = event.get("message")
            stamp = _edit_stamp(event) if isinstance(event, Mapping) else None
            if stamp is not None:
                stamps.add(stamp)
        return stamps


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
