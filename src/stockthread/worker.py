"""The worker: from the durable queue to a dry-run reply in the thread (design section 4, "Worker").

Two steps, both run only by the holder of the instance lock:

1. `drain` claims stored events. It runs the ingest checks again (the settings may have changed since the
   event was stored), and puts each new message in its post. Nothing is extracted yet, because a sibling
   message may still be on its way.
2. `reply_due` takes each post that is due: its newest message is older than the sibling window plus a
   grace period, and its thread root is in it (or has had time to arrive). It extracts, validates and
   matches, closes the post to siblings, posts the dry-run reply in the thread, and records the outcome.
   A post that is not a movement, or that the extractor declines, gets no reply.

Nothing here writes to InvenTree. Message text is read from the stored raw events and never logged. Edits,
deletions and clicks are M2 (plan revisions and review). Until then every edit and deletion is noted by
message key, in whatever order Slack delivers it, and a post that holds a noted message and has not had its
dry run is withheld: its original text would be wrong.

Every finished post keeps a record of what was decided and why (`Store.dry_run`): the extractor, the
candidates, every match with its rule and ids, and the review reasons. The record is the decision the reply
shows: a reply whose Slack call is slow to return is not decided again, and its post is not finished (not
given up on, not withheld) before the call has ended.
"""

from __future__ import annotations

import json
import logging
import re
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from stockthread.config import Settings
from stockthread.core import extract_rules
from stockthread.core.grouping import seconds
from stockthread.core.matching import Catalog, match_location, match_part
from stockthread.core.model import (
    Extraction,
    Match,
    MatchStatus,
    MessageInput,
    PostKey,
    ReasonCode,
    ReasonSource,
    ReviewReason,
)
from stockthread.core.validate import validate
from stockthread.slack.ingest import Deleted, Drop, Edited, NewMessage, Revised, ingest_event, revised_message
from stockthread.slack.replies import MovementView, dry_run_reply, gate_reasons
from stockthread.store import DryRunOutcome, EventStatus, LockLost, Store, WaitingPost

log = logging.getLogger(__name__)

GRACE_SECONDS = 10
"""Added to the sibling window before a post is due: delivery takes time, and clocks differ."""
ROOT_WAIT_SECONDS = 6 * 60
"""A post keyed on a thread root that has not arrived waits this long after the root, for Slack's retries
(after about 1 and 5 minutes). A root that never comes (a bot's post, or one older than the connector) is
then left out."""
LATE_AFTER_SECONDS = 10 * 60
"""A post answered this long after its newest message carries the review reason late_ingest."""
RETRY_FOR_SECONDS = 15 * 60
"""How long posting a reply is retried, with backoff, before the post is finished without one."""
MAX_BACKOFF_SECONDS = 60
HEARTBEAT_SECONDS = 10.0
"""The worker beats the instance lock at most this often, before each batch and each post, so one long step
(Slack or InvenTree slow to answer) never lets the lock look stale."""
MAX_POSTS_PER_STEP = 20
DRAIN_BATCH = 50
MAX_DRAIN_BATCHES = 20
"""A step drains at most this many batches. While events are still pending no post is due, so a backlog
never splits siblings."""

_SLACK_TS = re.compile(r"\d{1,10}\.\d{6}")


class SlackPoster(Protocol):
    """The one Web API call the worker makes (slack_sdk's `WebClient`)."""

    def chat_postMessage(
        self,
        *,
        channel: str,
        thread_ts: str,
        text: str,
        blocks: Sequence[dict[str, Any]],
        unfurl_links: bool,
        unfurl_media: bool,
    ) -> Any: ...


class PostStillRunning(Exception):
    """A reply was handed to Slack and the call has not returned yet. It may still be sent: the next try for
    the same thread takes its result (or its error) and sends nothing again (run.DeadlinePoster)."""


class _AlreadyStored:
    """The listener stored the event once; the worker runs the checks again without deduplicating."""

    def first_time(self, event_id: str) -> bool:
        return True


@dataclass
class _Failures:
    first: Decimal
    count: int
    next_try: Decimal


@dataclass
class _Sent:
    """A reply handed to Slack, with the decision it shows. While its call is still running the post is
    neither decided again nor finished: what is recorded is what was sent."""

    extractor: str
    decided: Mapping[str, Any]
    body: Mapping[str, Any]
    checks: int = 0
    next_check: Decimal = Decimal(0)


@dataclass
class Worker:
    settings: Settings
    store: Store
    owner: str
    """The instance lock owner. Every store write checks it."""
    slack: SlackPoster
    catalog: Callable[[], Catalog]
    """The current catalog snapshot. A failure here is an outage: it propagates, and the post waits."""
    clock: Callable[[], datetime]
    """Must return an aware datetime."""
    stopping: Callable[[], bool] = lambda: False
    """Checked between posts, so a stop request does not wait for a whole backlog."""
    _failures: dict[PostKey, _Failures] = field(default_factory=dict)
    _running: dict[PostKey, _Sent] = field(default_factory=dict)
    """Replies whose Slack call has not returned. Nothing else is posted until it has."""
    _beat_at: float = float("-inf")
    _quiet_until: Decimal = Decimal(0)
    """Slack rate-limited a reply: nothing is posted before this time."""

    def beat(self, *, now: bool = False) -> None:
        """Beat the instance lock's heartbeat, at most every HEARTBEAT_SECONDS unless `now`. Raises LockLost."""
        if now or time.monotonic() - self._beat_at >= HEARTBEAT_SECONDS:
            self.store.heartbeat(self.owner)
            self._beat_at = time.monotonic()

    def step(self) -> int:
        """Drain the queue and, once it is empty, reply to the posts that are due. Returns how much was done."""
        done, empty = self.drain()
        return done + self.reply_due() if empty else done

    def drain(self) -> tuple[int, bool]:
        """Handle pending events: how many, and whether the queue is empty now."""
        done = 0
        for _ in range(MAX_DRAIN_BATCHES):
            self.beat()
            events = self.store.claim_events(self.owner, limit=DRAIN_BATCH)
            for event in events:
                try:
                    status, outcome = self._handle_event(event.id, event.kind, event.body)
                except LockLost:
                    raise
                except Exception as error:
                    _log_failure("event", event.id, error)
                    status, outcome = EventStatus.FAILED, _code(error)
                self.store.finish_event(self.owner, event.id, status, outcome)
            done += len(events)
            if len(events) < DRAIN_BATCH:
                return done, True
        return done, False

    def _handle_event(self, event_id: int, kind: str, body: str | None) -> tuple[EventStatus, str]:
        if kind != "event":
            return EventStatus.DROPPED, "clicks_not_handled_yet"
        if body is None:
            return EventStatus.FAILED, "body_expired"
        try:
            payload = json.loads(body)
        except ValueError:
            return EventStatus.FAILED, "body_not_json"
        # Noted first, and by message key: the message itself may arrive after its edit. Read leniently, from
        # the raw event: the ingest checks drop some edits (a deleted root becomes a tombstone).
        found = revised_message(payload, self.settings.slack)
        if found is not None:
            changed = PostKey(self.settings.slack.workspace_id, *found)
            self.store.note_revision(self.owner, changed, event_id)
            post = self.store.waiting_post_of(changed)
            if post is not None and post not in self._running:  # a reply on its way is settled by its call
                # Changed before its dry run: reading the original text would be wrong. Revisions are M2.
                self._finish(post, DryRunOutcome.WITHHELD, result={"withheld": "changed"})
                return EventStatus.DONE, "withheld_changed_post"
        decision = ingest_event(payload, self.settings.slack, _AlreadyStored())
        if isinstance(decision, Drop):
            return EventStatus.DROPPED, decision.reason.value
        workspace = self.settings.slack.workspace_id
        if isinstance(decision, Edited | Deleted | Revised):
            return EventStatus.DROPPED, "revisions_not_handled_yet"
        if not isinstance(decision, NewMessage):
            return EventStatus.DROPPED, "not_a_message"
        message = decision.message
        try:
            self.store.place_message(
                self.owner,
                PostKey(workspace, message.channel, message.ts),
                user_id=message.user,
                thread_ts=message.thread_ts,
                window_seconds=self.settings.slack.group_window_seconds,
                slack_event_id=event_id,
            )
        except ValueError:
            return EventStatus.FAILED, "bad_message"  # a timestamp not in Slack's form, or no user
        return EventStatus.DONE, ""

    def reply_due(self) -> int:
        now = self._now()
        window = self.settings.slack.group_window_seconds
        done = 0
        for post in self.store.posts_waiting_for_dry_run():
            sent = self._running.get(post.key)
            if sent is not None:
                if sent.next_check <= now and not self.stopping():
                    self.beat()
                    self._post(post.key, now, sent)
                    done += 1
                continue
            if self._running:
                continue  # one call at a time: a reply is still on its way
            if seconds(post.newest_ts) + window + GRACE_SECONDS >= now:
                continue  # a sibling may still come
            if not post.has_key_message and seconds(post.key.ts) + ROOT_WAIT_SECONDS >= now:
                continue  # the thread root may still come
            failures = self._failures.get(post.key)
            if failures is not None and failures.next_try > now:
                continue
            if self.stopping() or done >= MAX_POSTS_PER_STEP or now < self._quiet_until:
                break  # the rest wait for the next step, or until Slack takes replies again
            self.beat()
            self._reply(post, now)
            done += 1
        return done

    def _reply(self, post: WaitingPost, now: Decimal) -> None:
        key = post.key
        age = now - seconds(post.newest_ts)
        read = None
        if self.store.post_has_revision(key):
            why = "changed"  # an edit or a deletion, even one that arrived before the message it changes
        elif age > self.settings.slack.plan_expiry_hours * 3600:
            why = "too_old"
        else:
            read = self._messages(key)
            why = "unreadable"
        if read is None:
            log.info("post %s: withheld (%s)", key.ts, why)
            self._finish(key, DryRunOutcome.WITHHELD, result={"withheld": why})
            return
        messages, poster = read
        try:
            extraction = extract_rules.extract(messages)
        except Exception as error:
            self._broken(key, error)
            return

        def record(views: Sequence[MovementView], late: bool) -> dict[str, Any]:
            return _record(
                extraction,
                views,
                late=late,
                poster=poster,
                messages=messages,
                max_quantity=self.settings.policy.max_quantity,
            )

        if extraction.declined or not extraction.movements:
            log.info("post %s: no reply (%s)", key.ts, "declined" if extraction.declined else "not a movement")
            self._finish(key, DryRunOutcome.SILENT, extraction.extractor, record((), False))
            return
        catalog = self.catalog()
        self.beat()  # a catalog read can take a while
        late = age > LATE_AFTER_SECONDS
        try:
            views = self._views(extraction, messages, poster, catalog, late)
            decided = record(views, late)
            body = dry_run_reply(
                views,
                {part.pk: part.name for part in catalog.parts},
                {location.pk: location.path for location in catalog.locations},
            )
        except Exception as error:
            self._broken(key, error, extraction.extractor)
            return
        # Closed first: a sibling that arrives from here on starts its own post. A crash after posting and
        # before the outcome is recorded posts the reply again at restart; a duplicate beats a silent drop.
        self.store.close_siblings(self.owner, key)
        self._post(key, now, _Sent(extraction.extractor, decided, body))

    def _post(self, key: PostKey, now: Decimal, sent: _Sent) -> None:
        """Post the reply, or take the result of the call that is still posting it, and record what it shows."""
        # The store has just checked that the lock is still ours (an instance that lost it while it was
        # blocked never posts). The beat makes the heartbeat fresh, so the post has the whole stale window: the
        # poster must return well within it (run.DeadlinePoster).
        self.beat(now=True)
        try:
            response = self.slack.chat_postMessage(
                channel=key.channel_id,
                thread_ts=key.ts,
                text=sent.body["text"],
                blocks=sent.body["blocks"],
                unfurl_links=False,
                unfurl_media=False,
            )
        except PostStillRunning:
            # Not a failure yet: the call may still send. The post stays open, with this decision, until the
            # call has ended; giving up now could leave a reply in Slack that the record denies.
            sent.checks += 1
            sent.next_check = now + min(2 ** min(sent.checks, 16), MAX_BACKOFF_SECONDS)
            self._running[key] = sent
            log.warning("post %s: reply still on its way to Slack, check %d", key.ts, sent.checks)
            return
        except Exception as error:  # any Slack or network error: retry with backoff, then give up
            self._running.pop(key, None)
            self._post_failed(key, now, error, sent.extractor, sent.decided)
            return
        self._running.pop(key, None)
        self._failures.pop(key, None)
        reply_ts = response.get("ts")
        valid = isinstance(reply_ts, str) and _SLACK_TS.fullmatch(reply_ts)
        self.store.finish_dry_run(
            self.owner,
            key,
            DryRunOutcome.REPLIED,
            reply_ts if valid else None,
            extractor=sent.extractor,
            result=sent.decided,
        )

    def _views(
        self,
        extraction: Extraction,
        messages: Sequence[MessageInput],
        poster: str,
        catalog: Catalog,
        late: bool,
    ) -> list[MovementView]:
        text = "\n".join(message.text for message in messages)
        lateness = (ReviewReason(ReasonCode.LATE_INGEST, ReasonSource.RULE),) if late else ()
        return [
            MovementView(
                movement=movement,
                items=[_part(item.item_text, catalog, poster) for item in movement.line_items],
                source=_location(movement.source_text, catalog, poster),
                destination=_location(movement.destination_text, catalog, poster),
                reasons=lateness
                + validate(
                    movement,
                    text,
                    max_quantity=self.settings.policy.max_quantity,
                    post_flags=extraction.post_flags,
                ),
            )
            for movement in extraction.movements
        ]

    def _post_failed(
        self, key: PostKey, now: Decimal, error: Exception, extractor: str, record: Mapping[str, Any]
    ) -> None:
        failures = self._failures.get(key) or _Failures(first=now, count=0, next_try=now)
        failures.count += 1
        asked = _retry_after(error)
        if asked:
            self._quiet_until = now + asked  # for every post, not only this one
        failures.next_try = now + max(min(2 ** min(failures.count, 16), MAX_BACKOFF_SECONDS), asked)
        if now < failures.first + RETRY_FOR_SECONDS:
            failures.next_try = min(failures.next_try, failures.first + RETRY_FOR_SECONDS)  # one last try on time
        self._failures[key] = failures
        log.warning("post %s: reply failed (%s), attempt %d", key.ts, type(error).__name__, failures.count)
        if now - failures.first >= RETRY_FOR_SECONDS:
            self._failures.pop(key)
            self._finish(key, DryRunOutcome.GAVE_UP, extractor, record)

    def _broken(self, key: PostKey, error: Exception, extractor: str | None = None) -> None:
        """The pure pipeline raised on this post: it gets no reply, so it cannot stop every later post."""
        _log_failure("post", key.ts, error)
        self._finish(key, DryRunOutcome.FAILED, extractor, {"error": _code(error)})

    def _finish(
        self,
        key: PostKey,
        outcome: DryRunOutcome,
        extractor: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        self.store.close_siblings(self.owner, key)
        self.store.finish_dry_run(self.owner, key, outcome, extractor=extractor, result=result)

    def _messages(self, key: PostKey) -> tuple[Sequence[MessageInput], str] | None:
        """The post's messages as the extractor takes them, and the author of the first one.

        None when any message cannot be read again (its body expired, it was grouped before the worker kept
        the link, or the ingest checks now drop it): a post is never read with a message left out.
        """
        messages: list[MessageInput] = []
        authors: list[str] = []
        for ts, body in self.store.post_bodies(key):
            try:
                payload = json.loads(body) if body else None
            except ValueError:
                return None
            decision = ingest_event(payload, self.settings.slack, _AlreadyStored()) if payload else None
            if not isinstance(decision, NewMessage) or decision.message.ts != ts:
                return None
            message = decision.message
            authors.append(message.user)
            messages.append(MessageInput(message.ts, message.text, message.edited_ts, message.file_ids))
        if not messages:
            return None
        return messages, authors[0]

    def _now(self) -> Decimal:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("the worker clock must return an aware datetime")
        return Decimal(str(now.timestamp()))


def _part(text: str | None, catalog: Catalog, poster: str) -> Match:
    return match_part(text, catalog, poster) if text else Match(MatchStatus.UNMATCHED)


def _location(text: str | None, catalog: Catalog, poster: str) -> Match | None:
    return match_location(text, catalog, poster) if text else None


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


def _record(
    extraction: Extraction,
    views: Sequence[MovementView],
    *,
    late: bool,
    poster: str,
    messages: Sequence[MessageInput],
    max_quantity: int,
) -> dict[str, Any]:
    """What the dry run decided, for the audit trail: candidates as extracted, matches (InvenTree ids and the
    rule), and every reason that kept a movement from "could apply". It quotes the post: stored, never logged."""
    return {
        "declined": extraction.declined,
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
                "items": [
                    {
                        "item_text": item.item_text,
                        "quantity": _number(item.quantity),
                        "unit": item.unit,
                        "pack_size": _number(item.pack_size),
                        "serials": list(item.serials),
                        "part": _match(part),
                    }
                    for item, part in zip(view.movement.line_items, view.items, strict=True)
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


def _retry_after(error: Exception) -> int:
    """The seconds a rate-limited Slack call asks to wait (its Retry-After header), or 0. The worker waits
    between steps and never inside a call, so the instance lock keeps its heartbeat."""
    headers: Any = getattr(getattr(error, "response", None), "headers", None) or {}
    try:
        stated = next(value for name, value in dict(headers).items() if str(name).lower() == "retry-after")
        return min(max(int(stated), 0), RETRY_FOR_SECONDS)
    except (StopIteration, TypeError, ValueError):
        return 0


def _code(error: Exception) -> str:
    """A short outcome code for an error: its class name only, never its message."""
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(error).__name__).lower()
    return "error_" + re.sub(r"[^a-z0-9_]", "", name)[:58]


def _log_failure(what: str, which: object, error: Exception) -> None:
    """The error class and where it was raised. No message and no traceback: either could quote the post."""
    frames = traceback.extract_tb(error.__traceback__)
    where = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else "?"
    log.error("%s %s: failed (%s at %s)", what, which, type(error).__name__, where)
