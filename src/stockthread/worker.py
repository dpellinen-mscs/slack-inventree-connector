"""The worker: from the durable queue to a dry-run reply in the thread (design section 4, "Worker").

Two steps, both run only by the holder of the instance lock:

1. `drain` claims stored events. It runs the ingest checks again (the settings may have changed since the
   event was stored), and puts each new message in its post. Nothing is extracted yet, because a sibling
   message may still be on its way.
2. `reply_due` takes each post whose newest message is older than the sibling window. It extracts, validates
   and matches, closes the post to siblings, posts the dry-run reply in the thread, and records the outcome.
   A post that is not a movement, or that the extractor declines, gets no reply.

Nothing here writes to InvenTree. Message text is read from the stored raw events and never logged. Edits,
deletions and clicks are M2 (plan revisions and review), so they are finished as dropped with a short code.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from stockthread.config import Settings
from stockthread.core import extract_rules
from stockthread.core.grouping import seconds
from stockthread.core.matching import Catalog, match_location, match_part
from stockthread.core.model import Match, MatchStatus, MessageInput, PostKey
from stockthread.core.validate import validate
from stockthread.slack.ingest import Drop, NewMessage, ingest_event
from stockthread.slack.replies import MovementView, dry_run_reply
from stockthread.store import EventStatus, LockLost, Store

log = logging.getLogger(__name__)

MAX_REPLY_FAILURES = 5
"""A post whose reply fails this many times in a row is finished without one, so a permanent error (the bot
is not in the channel) cannot loop. The failures are counted in memory: a restart tries again."""


class SlackPoster(Protocol):
    def chat_postMessage(self, **kwargs: Any) -> Any: ...  # slack_sdk's name


class _AlreadyStored:
    """The listener stored the event once; the worker runs the checks again without deduplicating."""

    def first_time(self, event_id: str) -> bool:
        return True


@dataclass
class Worker:
    settings: Settings
    store: Store
    owner: str
    """The instance lock owner. Every store write checks it."""
    slack: SlackPoster
    catalog: Callable[[], Catalog]
    """The current catalog snapshot, read once per post."""
    clock: Callable[[], datetime]
    _failures: dict[PostKey, int] = field(default_factory=dict)

    def step(self) -> int:
        """Drain the queue, then reply to the posts that are due. Returns how much was done."""
        return self.drain() + self.reply_due()

    def drain(self, limit: int = 50) -> int:
        events = self.store.claim_events(self.owner, limit=limit)
        for event in events:
            status, outcome = self._handle_event(event.id, event.kind, event.body)
            self.store.finish_event(self.owner, event.id, status, outcome)
        return len(events)

    def _handle_event(self, event_id: int, kind: str, body: str | None) -> tuple[EventStatus, str]:
        if kind != "event":
            return EventStatus.DROPPED, "clicks_not_handled_yet"
        if body is None:
            return EventStatus.FAILED, "body_expired"
        try:
            payload = json.loads(body)
        except ValueError:
            return EventStatus.FAILED, "body_not_json"
        decision = ingest_event(payload, self.settings.slack, _AlreadyStored())
        if isinstance(decision, Drop):
            return EventStatus.DROPPED, decision.reason.value
        if not isinstance(decision, NewMessage):
            return EventStatus.DROPPED, "revisions_not_handled_yet"  # an edit or a deletion: M2
        message = decision.message
        try:
            self.store.place_message(
                self.owner,
                PostKey(self.settings.slack.workspace_id, message.channel, message.ts),
                user_id=message.user,
                thread_ts=message.thread_ts,
                window_seconds=self.settings.slack.group_window_seconds,
                slack_event_id=event_id,
            )
        except ValueError:
            return EventStatus.FAILED, "bad_message"  # a timestamp not in Slack's form, or no user
        return EventStatus.DONE, ""

    def reply_due(self) -> int:
        now = Decimal(str(self.clock().timestamp()))
        window = self.settings.slack.group_window_seconds
        done = 0
        for key, newest in self.store.posts_waiting_for_dry_run():
            if seconds(newest) + window >= now:
                continue  # a sibling may still come
            try:
                self._reply(key)
            except LockLost:
                raise
            except Exception as error:
                # One post that breaks the pipeline must not stop every later post: it gets no reply.
                log.exception("post %s: dry run failed (%s)", key.ts, type(error).__name__)
                self.store.close_siblings(self.owner, key)
                self.store.finish_dry_run(self.owner, key, None)
            done += 1
        return done

    def _reply(self, key: PostKey) -> None:
        read = self._messages(key)
        extraction = extract_rules.extract(read[0]) if read else None
        if read is None or extraction is None or extraction.declined or not extraction.movements:
            why = "unreadable" if extraction is None else "declined" if extraction.declined else "not a movement"
            log.info("post %s: no reply (%s)", key.ts, why)
            self.store.close_siblings(self.owner, key)
            self.store.finish_dry_run(self.owner, key, None)
            return
        messages, poster = read
        catalog = self.catalog()
        text = "\n".join(message.text for message in messages)
        views = [
            MovementView(
                movement=movement,
                items=[_part(item.item_text, catalog, poster) for item in movement.line_items],
                source=_location(movement.source_text, catalog, poster),
                destination=_location(movement.destination_text, catalog, poster),
                reasons=validate(
                    movement,
                    text,
                    max_quantity=self.settings.policy.max_quantity,
                    post_flags=extraction.post_flags,
                ),
            )
            for movement in extraction.movements
        ]
        body = dry_run_reply(
            views,
            {part.pk: part.name for part in catalog.parts},
            {location.pk: location.path for location in catalog.locations},
        )
        # Closed first: a sibling that arrives from here on starts its own post. A crash after posting and
        # before `finish_dry_run` posts the reply again at restart; a duplicate beats a silent drop.
        self.store.close_siblings(self.owner, key)
        try:
            response = self.slack.chat_postMessage(
                channel=key.channel_id, thread_ts=key.ts, unfurl_links=False, unfurl_media=False, **body
            )
            reply_ts = response["ts"]
        except Exception as error:  # any Slack or network error: retry, then give up
            failures = self._failures.get(key, 0) + 1
            self._failures[key] = failures
            log.warning("post %s: reply failed (%s), attempt %d", key.ts, type(error).__name__, failures)
            if failures >= MAX_REPLY_FAILURES:
                self._failures.pop(key)
                self.store.finish_dry_run(self.owner, key, None)
            return
        self._failures.pop(key, None)
        self.store.finish_dry_run(self.owner, key, reply_ts)

    def _messages(self, key: PostKey) -> tuple[Sequence[MessageInput], str] | None:
        """The post's messages as the extractor takes them, and the author of the first one.

        None when any message cannot be read again (its body expired, it was grouped before the worker kept
        the link, or the ingest checks now drop it): a post is never read with a message left out.
        """
        messages: list[MessageInput] = []
        authors: list[str] = []
        for ts, body in self.store.post_bodies(key):
            decision = ingest_event(json.loads(body), self.settings.slack, _AlreadyStored()) if body else None
            if not isinstance(decision, NewMessage) or decision.message.ts != ts:
                return None
            message = decision.message
            authors.append(message.user)
            messages.append(MessageInput(message.ts, message.text, message.edited_ts, message.file_ids))
        if not messages:
            return None
        return messages, authors[0]


def _part(text: str | None, catalog: Catalog, poster: str) -> Match:
    return match_part(text, catalog, poster) if text else Match(MatchStatus.UNMATCHED)


def _location(text: str | None, catalog: Catalog, poster: str) -> Match | None:
    return match_location(text, catalog, poster) if text else None
