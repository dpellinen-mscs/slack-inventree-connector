"""The worker: from the durable queue to a dry-run reply in the thread (design section 4, "Worker").

Two steps, both run only by the holder of the instance lock:

1. `drain` claims stored events. It runs the ingest checks again (the settings may have changed since the
   event was stored), and puts each new message in its post. Nothing is extracted yet, because a sibling
   message may still be on its way.
2. `reply_due` takes each post that is due: its newest message is older than the sibling window plus a
   grace period, and its thread root is in it (or has had time to arrive). It extracts, validates and
   matches, closes the post to siblings, posts the dry-run reply in the thread, and records the outcome.
   A post that is not a movement, or that the extractor declines, gets no reply.

The rule-based extractor reads every post first. A post it declines goes to the LLM extractor when one is
configured (ADR 0011), and to nobody otherwise. The model's candidates pass the same validator and matcher.
A provider that gives no answer is an outage: the post waits and is tried again with backoff, and after
RETRY_FOR_SECONDS it is finished without a reply, as it would be with no model. An answer that is no reading
of the post (a refusal, a cut-off answer) is not an outage: that post alone is finished without a reply, at
once. A step makes at most one model call, and makes it last: after every reply that needs no call, and to
the waiting post asked least recently, so neither one slow call nor one post whose calls fail holds the
others back. The reading is kept and its reply is posted in the next step, after the queue has been drained:
an edit that came during the call makes the post read again.

Nothing here writes to InvenTree. Message text is read from the stored raw events and never logged. Clicks
are M2 (review). Every edit and deletion is noted by message key, in whatever order Slack delivers it, and a
post is read as its messages are now: each one as its newest edit shows it (by the edit's own timestamp, not
by arrival), and a deleted one left out. A post with a change that cannot be read (a tombstone, an edit
without a stamp, a body that expired) is never read from its original text: it is withheld as `changed`.

A message that joins a post after its dry run (a thread reply, or a root that catch-up found later), or an
edit or deletion of one of its messages noted after it was decided, makes the post due again, as revision 2,
3, ... It is read whole, as it is now, and:
- reads as the reply that stands: recorded as `unchanged`, nothing posted;
- reads as a different movement: a revised dry run is posted in the thread;
- can no longer be read as a movement (declined, withheld, failed) while a reply stands: a notice that the
  reply above no longer stands is posted (`retracted`). Without a standing reply, nothing is posted.
A revision is measured for expiry from the post's first message, not the new one: the movement is that old.

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
from stockthread.core.questions import choice_json, choices, pick_question, question_json
from stockthread.core.validate import validate
from stockthread.llm.extract import NoReading, ProviderError
from stockthread.slack.catchup import is_catch_up
from stockthread.slack.ingest import (
    Click,
    Decision,
    Deleted,
    Drop,
    Edited,
    NewMessage,
    Revised,
    ingest_action,
    ingest_event,
    posted_by_app,
    revised_message,
)
from stockthread.slack.replies import (
    ANSWER_ACTION,
    MovementView,
    click_feedback,
    dry_run_reply,
    gate_reasons,
    question_closed,
    question_message,
    retraction_notice,
    with_question,
    without_question,
)
from stockthread.store import (
    Answered,
    Button,
    DryRunOutcome,
    EventStatus,
    LockLost,
    NewQuestion,
    QuestionStatus,
    ReviewItem,
    Store,
    WaitingPost,
    new_button_id,
)

log = logging.getLogger(__name__)

GRACE_SECONDS = 10
"""Added to the sibling window before a post is due: delivery takes time, and clocks differ."""
ROOT_WAIT_SECONDS = 6 * 60
"""A post keyed on a thread root that has not arrived waits this long after the root, for Slack's retries
(after about 1 and 5 minutes). A root that never comes (a bot's post, or one older than the connector) is
then left out."""
LATE_AFTER_SECONDS = 10 * 60
"""A post answered this long after its newest message carries the review reason late_ingest. So does a post
with a message that catch-up found in the channel history, whatever its age."""
RETRY_FOR_SECONDS = 15 * 60
"""How long posting a reply, or asking the LLM provider, is retried with backoff before the post is finished
without a reply."""
MAX_BACKOFF_SECONDS = 60
TAKE_DOWN_SPACING_SECONDS = 2
"""One reply is edited to take a closed question's buttons down at most this often: two calls each, within the
50 a minute that `conversations.replies` and `chat.update` each allow."""
TAKE_DOWN_TRIES = 3
"""Failed attempts on one question before the connector gives up on taking its buttons down."""
TAKE_DOWN_BACKOFF_SECONDS = 60
"""After a failed attempt no buttons are taken down for this long (doubled for the question's each further
failure), or for as long as Slack asks, if longer."""
NOTE_QUIET_SECONDS = 60
"""After a note to a clicker fails, no note is sent for this long: a slow Slack never holds the queue up."""
HEARTBEAT_SECONDS = 10.0
"""The worker beats the instance lock at most this often, before each batch and each post, so one long step
(Slack or InvenTree slow to answer) never lets the lock look stale."""
MAX_POSTS_PER_STEP = 20
DRAIN_BATCH = 50
MAX_DRAIN_BATCHES = 20
"""A step drains at most this many batches. While events are still pending no post is due, so a backlog
never splits siblings."""

_SLACK_TS = re.compile(r"\d{1,10}\.\d{6}")
_UNAVAILABLE = "unavailable"
_NO_READING = "no_reading"


class SlackPoster(Protocol):
    """The Web API calls the worker makes (slack_sdk's `WebClient`)."""

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

    def chat_postEphemeral(self, *, channel: str, user: str, thread_ts: str, text: str) -> Any: ...

    def conversations_replies(
        self, *, channel: str, ts: str, oldest: str, latest: str, inclusive: bool, limit: int
    ) -> Any: ...

    def chat_update(self, *, channel: str, ts: str, text: str, blocks: Sequence[dict[str, Any]]) -> Any: ...


class PostExtractor(Protocol):
    """The LLM extractor (`llm.extract.LLMExtractor`). Raises ProviderError when its provider gives no answer,
    and its subclass NoReading when the answer is no reading of this post."""

    def extract(self, messages: Sequence[MessageInput]) -> Extraction: ...


class PosterBusy(Exception):
    """Another thread's call is still running, and this one was not sent. An ordinary failure: try again."""


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


@dataclass(frozen=True)
class _Feedback:
    """A note for the clicker alone, sent once the click is finished."""

    channel: str
    user: str
    thread_ts: str
    text: str


@dataclass
class _Waiting:
    """A post the rules decline, waiting for the LLM provider."""

    first: Decimal
    asked_at: Decimal = Decimal(0)
    """When the provider was last asked about this post."""
    count: int = 0
    next_look: Decimal = Decimal(0)
    """The post is not looked at before this time: the provider is left alone until then."""


@dataclass
class _Sent:
    """A reply handed to Slack, with the decision it shows. While its call is still running the post is
    neither decided again nor finished: what is recorded is what was sent."""

    extractor: str | None
    decided: Mapping[str, Any] | None
    body: Mapping[str, Any]
    revision: int = 1
    read: tuple[str, ...] | None = None
    """The timestamps of the messages the decision read (None: all the post holds when it is recorded)."""
    changes: int | None = None
    """The newest noted edit or deletion the decision knew of (None: the newest when it is recorded)."""
    outcome: DryRunOutcome = DryRunOutcome.REPLIED
    """REPLIED, or RETRACTED for a notice that the reply above no longer stands."""
    checks: int = 0
    next_check: Decimal = Decimal(0)
    question: NewQuestion | None = None
    """The question the reply asks, stored with its buttons once the reply is posted."""


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
    llm: PostExtractor | None = None
    """Reads the posts the rule-based extractor declines. None: those posts get no reply."""
    _llm_read: dict[PostKey, tuple[tuple[MessageInput, ...], Extraction]] = field(default_factory=dict)
    """What the model read, and the messages as it read them, kept until the post is finished: a reply that is
    tried again is not read again. A post that has gained, lost or changed a message since is."""
    _llm_quiet_until: Decimal = Decimal(0)
    """The provider gave no answer: it is not asked again, for any post, before this time."""
    _llm_failed: int = 0
    """Calls in a row without an answer, whatever the post: the backoff grows with it."""
    _llm_waiting: dict[PostKey, _Waiting] = field(default_factory=dict)
    _failures: dict[PostKey, _Failures] = field(default_factory=dict)
    _running: dict[PostKey, _Sent] = field(default_factory=dict)
    """Replies whose Slack call has not returned. Nothing else is posted until it has."""
    _beat_at: float = float("-inf")
    _quiet_until: Decimal = Decimal(0)
    """Slack rate-limited a reply: nothing is posted before this time."""
    _feedback: _Feedback | None = None
    """What to tell the clicker about the event being handled."""
    _notes_quiet_until: Decimal = Decimal(0)
    """A note failed: no note is sent before this time."""
    _take_down_quiet_until: Decimal = Decimal(0)
    """No buttons are taken down before this time."""

    def beat(self, *, now: bool = False) -> None:
        """Beat the instance lock's heartbeat, at most every HEARTBEAT_SECONDS unless `now`. Raises LockLost."""
        if now or time.monotonic() - self._beat_at >= HEARTBEAT_SECONDS:
            self.store.heartbeat(self.owner)
            self._beat_at = time.monotonic()

    def step(self) -> int:
        """Drain the queue and, once it is empty, reply to the posts that are due. Returns how much was done."""
        done, empty = self.drain()
        return done + self.reply_due() + self.take_down_buttons() if empty else done

    def drain(self) -> tuple[int, bool]:
        """Handle pending events: how many, and whether the queue is empty now."""
        done = 0
        for _ in range(MAX_DRAIN_BATCHES):
            self.beat()
            events = self.store.claim_events(self.owner, limit=DRAIN_BATCH)
            for event in events:
                self._feedback = None
                try:
                    status, outcome = self._handle_event(event.id, event.kind, event.body)
                except LockLost:
                    raise
                except Exception as error:
                    _log_failure("event", event.id, error)
                    status, outcome = EventStatus.FAILED, _code(error)
                self.store.finish_event(self.owner, event.id, status, outcome)
                if self._feedback is not None and not self.stopping():
                    self._tell(event.id, self._feedback)
            done += len(events)
            if len(events) < DRAIN_BATCH:
                return done, True
        return done, False

    def _handle_event(self, event_id: int, kind: str, body: str | None) -> tuple[EventStatus, str]:
        if body is None:
            return EventStatus.FAILED, "body_expired"
        try:
            payload = json.loads(body)
        except ValueError:
            return EventStatus.FAILED, "body_not_json"
        if kind == "interactive":
            return self._handle_click(payload)
        # Noted by message key: the message itself may arrive after its edit. Read leniently, from the raw
        # event: the ingest checks drop some edits (a deleted root becomes a tombstone). The post is read again
        # when it is due: its first dry run reads the edit, and an answered post is due for a revision.
        workspace = self.settings.slack.workspace_id
        found = revised_message(payload, self.settings.slack)
        if found is not None:
            self.store.note_revision(self.owner, PostKey(workspace, *found), event_id)
            return EventStatus.DONE, "revision_noted"
        decision = ingest_event(payload, self.settings.slack, _AlreadyStored())
        if isinstance(decision, Drop):
            return EventStatus.DROPPED, decision.reason.value
        if isinstance(decision, Edited | Deleted | Revised):
            return EventStatus.DROPPED, "not_a_revision"
        if not isinstance(decision, NewMessage):
            return EventStatus.DROPPED, "not_a_message"
        message = decision.message
        key = PostKey(workspace, message.channel, message.ts)
        # The history shows the message edited, and it is in a post already, from the event Slack sent when it
        # was made: that event has the text from before the edit, and this one is noted as its edit.
        if is_catch_up(decision.event_id) and message.edited_ts is not None and self.store.message_placed(key):
            self.store.note_revision(self.owner, key, event_id)
            return EventStatus.DONE, "revision_noted"
        try:
            self.store.place_message(
                self.owner,
                key,
                user_id=message.user,
                thread_ts=message.thread_ts,
                window_seconds=self.settings.slack.group_window_seconds,
                slack_event_id=event_id,
            )
        except ValueError:
            return EventStatus.FAILED, "bad_message"  # a timestamp not in Slack's form, or no user
        return EventStatus.DONE, ""

    def _handle_click(self, payload: object) -> tuple[EventStatus, str]:
        """Record the answer a click on a question's button gives, after the ingest checks again and the checks
        against the stored button: the same channel and message, a question still open and not expired, a post
        that has not moved on from the dry run that asked it, and a user who may answer it (design section 4,
        "Authorization"). Every refusal is finished as dropped, with its reason as the outcome. A click that
        passes the ingest checks tells the clicker alone what came of it."""
        click = ingest_action(payload, self.settings.slack)
        if isinstance(click, Drop):
            return EventStatus.DROPPED, click.reason.value
        if not isinstance(click, Click):
            return EventStatus.DROPPED, "not_a_click"
        if click.action_id != ANSWER_ACTION:
            return EventStatus.DROPPED, "action_not_handled_yet"
        button = self.store.button(click.value)
        status, outcome = self._answer(click, button)
        told = outcome
        if outcome == Answered.NOT_OPEN and button is not None:
            # The click is handled again after a crash that came after its answer was recorded: the same user,
            # and the same button. The same user's other button is a changed answer, and it was refused.
            answer = self.store.answer(button.item.id)
            replayed = answer is not None and (answer[0].get("button"), answer[1]) == (button.position, click.user)
            told = Answered.ANSWERED if replayed else outcome
        text = click_feedback(
            told,
            approver_only=button is not None and button.item.approver_only,
            approvers=bool(self.settings.slack.approvers),
            expiry_hours=self.settings.slack.plan_expiry_hours,
        )
        if text is not None:
            self._feedback = _Feedback(click.channel, click.user, click.thread_ts or click.message_ts, text)
        return status, outcome

    def _tell(self, event_id: int, feedback: _Feedback) -> None:
        """Send a note to the clicker. Best effort: the click is finished already. A note that fails is logged
        and not tried again, and no note is sent for NOTE_QUIET_SECONDS after it."""
        now = self._now()
        if now < self._notes_quiet_until:
            log.info("click feedback %s: not sent, a recent note failed", event_id)
            return
        self.beat()  # the note has its own deadline (run.NOTE_DEADLINE_SECONDS)
        try:
            self.slack.chat_postEphemeral(
                channel=feedback.channel, user=feedback.user, thread_ts=feedback.thread_ts, text=feedback.text
            )
        except Exception as error:
            _log_failure("click feedback", event_id, error)
            self._notes_quiet_until = now + NOTE_QUIET_SECONDS

    def take_down_buttons(self) -> int:
        """Take a question out of the reply it was asked in, once it is answered or a newer reply replaced it
        (design section 4, "Interactivity"): the reply is read back from Slack, and edited to end with a line that
        says what became of the question. Best effort, one reply per TAKE_DOWN_SPACING_SECONDS, fewest failed
        tries first; the clicks are checked against the store whatever Slack shows. Returns 1 when a reply was
        edited or given up on."""
        now = self._now()
        if self.stopping() or now < max(self._quiet_until, self._take_down_quiet_until):
            return 0
        listed = self.store.closed_with_buttons(limit=1)
        if not listed:
            return 0
        item, answered_by = listed[0]
        self.beat()
        self._take_down_quiet_until = now + TAKE_DOWN_SPACING_SECONDS
        try:
            removed = self._take_down(item, answered_by)
        except PosterBusy:
            return 0  # another call is still on its way: nothing was tried
        except Exception as error:
            _log_failure("buttons of question", item.id, error)
            tries = self.store.buttons_failed(self.owner, item.id)
            if tries < TAKE_DOWN_TRIES:
                wait = max(TAKE_DOWN_BACKOFF_SECONDS * 2 ** (tries - 1), _retry_after(error))
                self._take_down_quiet_until = now + wait
                return 0
            removed = False
        self.store.buttons_taken_down(self.owner, item.id, removed=removed)
        if not removed:
            log.warning("buttons of question %s: left in Slack", item.id)
        return 1

    def _take_down(self, item: ReviewItem, answered_by: str | None) -> bool:
        """Edit the reply; False when it is gone or carries no question to take out."""
        if item.message_ts is None:  # closed_with_buttons lists none
            return False
        found = self.slack.conversations_replies(
            channel=item.channel_id,
            ts=item.post.ts,
            oldest=item.message_ts,
            latest=item.message_ts,
            inclusive=True,
            limit=2,  # the thread's root always comes first
        )
        reply = next((m for m in found["messages"] if m.get("ts") == item.message_ts), None)
        if reply is None or not posted_by_app(reply, self.settings.slack.app_id):
            return False
        blocks = reply.get("blocks") or []
        closing = question_closed(answered_by if item.status is QuestionStatus.ANSWERED else None)
        kept = without_question(blocks)
        if kept is None:
            return _closed_already(blocks, closing)  # an edit whose call ran out of time went through
        self.slack.chat_update(
            channel=item.channel_id, ts=item.message_ts, text=str(reply.get("text") or ""), blocks=[*kept, closing]
        )
        return True

    def _answer(self, click: Click, button: Button | None) -> tuple[EventStatus, str]:
        if button is None:
            return EventStatus.DROPPED, "unknown_button"  # never stored, or the reply was posted again
        refused = self._refuse_answer(click, button)
        if refused:
            return EventStatus.DROPPED, refused
        answered = self.store.answer_question(self.owner, button, click.user)
        if answered is not Answered.ANSWERED:
            return EventStatus.DROPPED, answered.value
        log.info("post %s: question answered", button.item.post.ts)
        return EventStatus.DONE, answered.value

    def _refuse_answer(self, click: Click, button: Button) -> str:
        """Why the click cannot answer, or "" when it can."""
        item = button.item
        if (
            item.post.workspace_id != self.settings.slack.workspace_id
            or item.channel_id != click.channel
            or item.message_ts is None
            or item.message_ts != click.message_ts
        ):
            return "other_message"
        if item.status is QuestionStatus.STALE:
            return "stale"
        if item.status is QuestionStatus.ANSWERED:
            return "already_answered"
        # From the same time as the revisions of the post (`_reply`): its plan expires as one.
        if self._now() - seconds(item.expires_from or item.post.ts) > self.settings.slack.plan_expiry_hours * 3600:
            return "expired"
        if click.user in self.settings.slack.approvers:
            return ""
        if item.approver_only or item.poster_id is None or click.user != item.poster_id:
            return "not_allowed"
        return ""

    def reply_due(self) -> int:
        now = self._now()
        window = self.settings.slack.group_window_seconds
        done = 0
        for_the_model: list[WaitingPost] = []
        expiry = self.settings.slack.plan_expiry_hours * 3600
        for post in self.store.posts_waiting_for_dry_run(revisable_since=now - expiry):
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
            if post.revision == 1 and not post.has_key_message and seconds(post.key.ts) + ROOT_WAIT_SECONDS >= now:
                continue  # the thread root may still come (a revision was answered without it once already)
            failures = self._failures.get(post.key)
            if failures is not None and failures.next_try > now:
                continue
            waiting = self._llm_waiting.get(post.key)
            if waiting is not None and waiting.next_look > now:
                continue
            if self.stopping() or done >= MAX_POSTS_PER_STEP or now < self._quiet_until:
                break  # the rest wait for the next step, or until Slack takes replies again
            self.beat()
            if self._reply(post, now):
                done += 1
            else:
                for_the_model.append(post)  # costs nothing yet, and does not count: it cannot crowd a step out
        # One model call in a step, and last: a call is slow, so every reply that needs none has gone out, and
        # the queue is drained again (an edit may have come) before anything else is decided.
        chosen = self._whose_turn(for_the_model, now)
        if chosen is not None and not self.stopping():
            self.beat()
            self._reply(chosen, now, ask=True)
            done += 1
        return done

    def _whose_turn(self, posts: Sequence[WaitingPost], now: Decimal) -> WaitingPost | None:
        """The post the provider is asked about in this step, of those that wait for it.

        A post is only given up on after a call of its own, made on time even while the provider is otherwise
        left alone. Otherwise, once the backoff has passed, the post asked least recently: one post whose
        calls fail cannot take every call.
        """
        waiting = [(self._llm_waiting[post.key], post) for post in posts if post.key in self._llm_waiting]
        for state, post in waiting:
            if now >= state.first + RETRY_FOR_SECONDS:
                return post
        if not waiting or now < self._llm_quiet_until:
            return None
        return min(waiting, key=lambda found: found[0].asked_at)[1]  # the first of equals: the oldest post

    def _reply(self, post: WaitingPost, now: Decimal, *, ask: bool = False) -> bool:
        """Decide the post and post its reply. False: nothing was done, the post waits for a model call, which
        is only made with `ask`."""
        key = post.key
        # Before anything is read: what is read is the post as of this change, and one noted from here on makes
        # the next revision.
        changes = self.store.post_changes_through(key)
        age = now - seconds(post.newest_ts)
        # A revision's movement is as old as the post: a new reply does not make a week-old movement current.
        since = seconds(post.key.ts) if post.revision > 1 else seconds(post.newest_ts)
        read: tuple[Sequence[MessageInput], str, bool] | None = None
        if now - since > self.settings.slack.plan_expiry_hours * 3600:
            why = "too_old"  # for a revision, the reply above has expired: there is nothing to take back
        else:
            found = self._messages(key, changes)
            read, why = (None, found) if isinstance(found, str) else (found, "")
        if read is None:
            log.info("post %s: withheld (%s)", key.ts, why)
            # Unread, but decided on the messages the post holds now: one that joins later makes a revision.
            held = tuple(self.store.post_messages(key))
            self._conclude(
                post, now, DryRunOutcome.WITHHELD, None, {"withheld": why}, why=why, read=held, changes=changes
            )
            return True
        messages, poster, caught_up = read
        seen = tuple(message.ts for message in messages)
        try:
            extraction, unheard = self._extract(key, messages, now, ask)
        except LockLost:
            raise
        except Exception as error:
            self._broken(post, now, error, read=seen, changes=changes)
            return True
        if extraction is None:
            return False  # waits for the LLM provider, or for the queue to be drained after its call

        def record(views: Sequence[MovementView], late: bool) -> dict[str, Any]:
            return _record(
                extraction,
                views,
                late=late,
                poster=poster,
                messages=messages,
                max_quantity=self.settings.policy.max_quantity,
                llm_unavailable=unheard == _UNAVAILABLE,
                llm_no_reading=unheard == _NO_READING,
            )

        if extraction.declined or not extraction.movements:
            log.info("post %s: no reply (%s)", key.ts, "declined" if extraction.declined else "not a movement")
            # A model that did not answer, or gave no reading, says nothing about the thread: the notice says so.
            unread = "unread" if unheard else None
            self._conclude(
                post,
                now,
                DryRunOutcome.SILENT,
                extraction.extractor,
                record((), False),
                why=unread,
                read=seen,
                changes=changes,
            )
            return True
        catalog = self.catalog()
        self.beat()  # a catalog read can take a while
        late = caught_up or age > LATE_AFTER_SECONDS
        standing = self._standing(post)
        try:
            views = self._views(extraction, messages, poster, catalog, late)
            decided = record(views, late)
            parts = {part.pk: part.name for part in catalog.parts}
            locations = {location.pk: location.path for location in catalog.locations}
            body = dry_run_reply(views, parts, locations, revised=standing is not None)
        except Exception as error:
            self._broken(post, now, error, extraction.extractor, read=seen, changes=changes)
            return True
        # The one question the post needs goes in the reply, with buttons whose ids are stored with it. A
        # question that cannot be put is left out: the reply still says what needs a human.
        asked = None
        try:
            question = pick_question(views)
            if question is not None:
                buttons = choices(question)
                ids = [new_button_id() for _ in buttons]
                body = with_question(body, question_message(question, views, parts, locations, ids))
                asked = NewQuestion(
                    question_json(question),
                    question.approver_only,
                    [(button_id, choice_json(choice)) for button_id, choice in zip(ids, buttons, strict=True)],
                    poster=poster,
                    expires_from=post.key.ts if post.revision > 1 else post.newest_ts,  # as `since` above
                )
        except Exception as error:
            _log_failure("question", key.ts, error)
        if standing is not None and _same_reading(standing, decided):
            log.info("post %s: revision %d reads as the reply that stands", key.ts, post.revision)
            self._finish(key, DryRunOutcome.UNCHANGED, extraction.extractor, decided, post.revision, seen, changes)
            return True
        # Closed first: a sibling that arrives from here on starts its own post. A crash after posting and
        # before the outcome is recorded posts the reply again at restart; a duplicate beats a silent drop.
        self.store.close_siblings(self.owner, key)
        self._post(key, now, _Sent(extraction.extractor, decided, body, post.revision, seen, changes, question=asked))
        return True

    def _standing(self, post: WaitingPost) -> Mapping[str, Any] | None:
        """For a revision: what the reply that stands in the thread decided, or {} when its record expired.
        None: no reply stands (none was posted, or a notice took it back)."""
        if post.revision == 1:
            return None
        standing: Mapping[str, Any] | None = None
        for record in self.store.dry_run_history(post.key):
            if record.outcome is DryRunOutcome.REPLIED:
                standing = record.result or {}
            elif record.outcome is DryRunOutcome.RETRACTED:
                standing = None
        return standing

    def _conclude(
        self,
        post: WaitingPost,
        now: Decimal,
        outcome: DryRunOutcome,
        extractor: str | None,
        result: Mapping[str, Any],
        *,
        why: str | None = None,
        read: tuple[str, ...] | None = None,
        changes: int | None = None,
    ) -> None:
        """Finish a post that gets no dry-run reply. A revision whose post had a reply standing posts a notice
        that it no longer does, except when the post is too old: its reply has expired by then."""
        reason = why or ("silent" if outcome is DryRunOutcome.SILENT else "failed")
        if reason == "too_old" or self._standing(post) is None:
            self._finish(post.key, outcome, extractor, result, post.revision, read, changes)
            return
        log.info("post %s: revision %d takes the reply back (%s)", post.key.ts, post.revision, reason)
        decided = {**result, "retracted": reason, "instead_of": outcome.value}
        self._post(
            post.key,
            now,
            _Sent(extractor, decided, retraction_notice(reason), post.revision, read, changes, DryRunOutcome.RETRACTED),
        )

    def _extract(
        self, key: PostKey, messages: Sequence[MessageInput], now: Decimal, ask: bool
    ) -> tuple[Extraction | None, str]:
        """The extraction, and why the model was not heard ("" when it was, or was not needed). None: the post
        waits, for the provider or for the next step.

        Rules first. Only a post they decline is sent to the model, so text the rules can read never leaves
        the machine, and the model cannot change what the rules read. The model is only called with `ask`.
        """
        by_rule = extract_rules.extract(messages)
        if self.llm is None or not by_rule.declined:
            self._llm_waiting.pop(key, None)
            return by_rule, ""
        sent = tuple(messages)
        earlier = self._llm_read.get(key)
        if earlier is not None and earlier[0] == sent:
            return earlier[1], ""
        waiting = self._llm_waiting.setdefault(key, _Waiting(first=now))
        last_try = waiting.first + RETRY_FOR_SECONDS
        if not ask:
            waiting.next_look = min(max(self._llm_quiet_until, now), last_try)
            return None, ""
        waiting.asked_at = now
        self.beat(now=True)  # the call has the whole stale window (run.lock_stale_after)
        try:
            read = self.llm.extract(messages)
        except NoReading as error:
            # The provider is well: no backoff, and no second call for the same answer.
            self._llm_failed, self._llm_quiet_until = 0, Decimal(0)
            self._llm_waiting.pop(key, None)
            self.beat()
            log.warning("post %s: no reading from the model (%s)", key.ts, error)  # quotes nothing
            return by_rule, _NO_READING
        except ProviderError as error:
            waiting.count += 1
            self._llm_failed += 1
            # From the time the call ended: a call that takes a minute to fail has used its backoff up.
            ended = self._now()
            self._llm_quiet_until = ended + min(2 ** min(self._llm_failed, 16), MAX_BACKOFF_SECONDS)
            # The sentence quotes nothing (llm.extract.ProviderError).
            log.warning("post %s: no answer from the LLM provider (%s), attempt %d", key.ts, error, waiting.count)
            self.beat()
            if now >= last_try:
                self._llm_waiting.pop(key, None)
                log.warning("post %s: gave up on the LLM provider", key.ts)
                return by_rule, _UNAVAILABLE
            waiting.next_look = min(self._llm_quiet_until, last_try)
            return None, ""
        self._llm_failed, self._llm_quiet_until = 0, Decimal(0)
        self._llm_waiting.pop(key, None)
        self._llm_read[key] = (sent, read)
        self.beat()
        # The call took a while, and an edit of this post may have come in: the reply is posted from what is
        # kept, in the next step, after the queue has been drained.
        return None, ""

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
            self._post_failed(key, now, error, sent)
            return
        self._running.pop(key, None)
        self._failures.pop(key, None)
        self._llm_read.pop(key, None)
        self._llm_waiting.pop(key, None)
        reply_ts = response.get("ts")
        valid = isinstance(reply_ts, str) and _SLACK_TS.fullmatch(reply_ts)
        self.store.finish_dry_run(
            self.owner,
            key,
            sent.outcome,
            reply_ts if valid else None,
            extractor=sent.extractor,
            result=sent.decided,
            revision=sent.revision,
            read=sent.read,
            changes=sent.changes,
            question=sent.question,
        )
        if sent.outcome is DryRunOutcome.RETRACTED:
            log.info("post %s: revision %d notice posted", key.ts, sent.revision)
        elif sent.revision > 1:
            log.info("post %s: revision %d dry run posted", key.ts, sent.revision)
        else:
            log.info("post %s: dry run posted", key.ts)

    def _views(
        self,
        extraction: Extraction,
        messages: Sequence[MessageInput],
        poster: str,
        catalog: Catalog,
        late: bool,
    ) -> list[MovementView]:
        text = "\n".join(message.text for message in messages if not message.deleted)
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

    def _post_failed(self, key: PostKey, now: Decimal, error: Exception, sent: _Sent) -> None:
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
            self._finish(
                key, DryRunOutcome.GAVE_UP, sent.extractor, sent.decided, sent.revision, sent.read, sent.changes
            )

    def _broken(
        self,
        post: WaitingPost,
        now: Decimal,
        error: Exception,
        extractor: str | None = None,
        *,
        read: tuple[str, ...] | None = None,
        changes: int | None = None,
    ) -> None:
        """The pure pipeline raised on this post: it gets no reply, so it cannot stop every later post."""
        _log_failure("post", post.key.ts, error)
        self._conclude(post, now, DryRunOutcome.FAILED, extractor, {"error": _code(error)}, read=read, changes=changes)

    def _finish(
        self,
        key: PostKey,
        outcome: DryRunOutcome,
        extractor: str | None = None,
        result: Mapping[str, Any] | None = None,
        revision: int = 1,
        read: tuple[str, ...] | None = None,
        changes: int | None = None,
    ) -> None:
        self._llm_read.pop(key, None)
        self._llm_waiting.pop(key, None)
        self._failures.pop(key, None)
        self.store.close_siblings(self.owner, key)
        self.store.finish_dry_run(
            self.owner,
            key,
            outcome,
            extractor=extractor,
            result=result,
            revision=revision,
            read=read,
            changes=changes,
        )

    def _messages(self, key: PostKey, changes: int) -> tuple[Sequence[MessageInput], str, bool] | str:
        """The post's messages as the extractor takes them, each as it is after the noted changes up to
        `changes`, the author of the first one, and whether any of them was not delivered by Slack but found in
        the channel history (slack/catchup.py), or edited as the history showed. A deleted message is kept,
        marked deleted, with no text.

        A post is never read with a message left out or read from its original text. "unreadable" when a
        message cannot be read again (its body expired, it was grouped before the worker kept the link, or the
        ingest checks now drop it); "changed" when a change of it cannot be read, or every message was deleted.
        """
        noted = self.store.post_change_bodies(key, changes) if changes else {}
        messages: list[MessageInput] = []
        authors: list[str] = []
        caught_up = False
        for ts, body in self.store.post_bodies(key):
            decision = self._decision(body)
            if not isinstance(decision, NewMessage) or decision.message.ts != ts:
                return "unreadable"
            changed = _as_changed(decision, [self._decision(edit) for edit in noted.pop(ts, ())])
            if changed is None:
                return "changed"
            message, source = changed
            # Late too when the text read is an edit found in the history rather than delivered.
            caught_up = caught_up or is_catch_up(decision.event_id) or is_catch_up(source)
            authors.append(decision.message.user)
            messages.append(message)
        if not messages:
            return "unreadable"
        if noted or all(message.deleted for message in messages):
            return "changed"  # an edit of a thread root that is not in the post, or nothing left to read
        return messages, authors[0], caught_up

    def _decision(self, body: str | None) -> Decision | None:
        """A stored event, through the ingest checks with the current settings. None when it cannot be read."""
        try:
            payload = json.loads(body) if body else None
        except ValueError:
            return None
        return ingest_event(payload, self.settings.slack, _AlreadyStored()) if payload else None

    def _now(self) -> Decimal:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("the worker clock must return an aware datetime")
        return Decimal(str(now.timestamp()))


def _as_changed(stored: NewMessage, changes: Sequence[Decision | None]) -> tuple[MessageInput, str] | None:
    """A message as its noted changes leave it, and the id of the event its text comes from: deleted when any
    change deletes it, else as the edit with the newest edit timestamp shows it (Slack does not keep event
    order). None when a change cannot be read: it is not an edit or deletion of this message by its author,
    a stamp is not in Slack's form, or two edits with one timestamp disagree."""
    original = stored.message
    if changes and original.edited_ts is not None and not _SLACK_TS.fullmatch(original.edited_ts):
        return None
    newest, source, deleted = original, stored.event_id, False
    for change in changes:
        if isinstance(change, Deleted) and (change.channel, change.ts) == (original.channel, original.ts):
            deleted = True
            continue
        if not isinstance(change, Edited | NewMessage):
            return None  # Revised (a tombstone, an edit without a stamp or a user), a drop, or an expired body
        edit = change.message
        stamp = edit.edited_ts
        same = (edit.channel, edit.ts, edit.user) == (original.channel, original.ts, original.user)
        if not same or stamp is None or not _SLACK_TS.fullmatch(stamp):
            return None
        if newest.edited_ts is None or seconds(stamp) > seconds(newest.edited_ts):
            newest, source = edit, change.event_id
        elif seconds(stamp) == seconds(newest.edited_ts) and (edit.text, edit.file_ids) != (
            newest.text,
            newest.file_ids,
        ):
            return None
    if deleted:
        return MessageInput(original.ts, "", newest.edited_ts, deleted=True), source
    return MessageInput(newest.ts, newest.text, newest.edited_ts, newest.file_ids), source


def _closed_already(blocks: Sequence[Any], closing: Mapping[str, Any]) -> bool:
    last = blocks[-1] if blocks else None
    if not isinstance(last, Mapping) or last.get("type") != "context" or not last.get("elements"):
        return False
    first = last["elements"][0]
    return isinstance(first, Mapping) and first.get("text") == closing["elements"][0]["text"]


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


def _same_reading(standing: Mapping[str, Any], decided: Mapping[str, Any]) -> bool:
    """Whether a revision decided the same movements, matches and reasons as the reply that stands. Lateness is
    left out: it depends on when each was read, not on what the thread says."""

    def reading(result: Mapping[str, Any]) -> Any:
        movements = json.loads(json.dumps(result.get("movements")))
        for movement in movements or ():
            movement["reasons"] = [r for r in movement["reasons"] if r["code"] != ReasonCode.LATE_INGEST.value]
            movement["gate"] = [g for g in movement["gate"] if g[0] != ReasonCode.LATE_INGEST.value]
        return movements

    return bool(standing) and reading(standing) == reading(decided)


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
