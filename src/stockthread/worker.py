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

3. `work_due` runs the step of the apply protocol each request is due for, through `apply`, and `post_receipts`
   posts the receipt each plan is due in its thread, with an Undo button where an undo may be asked for.
Nothing here writes to InvenTree but `apply` and an Undo click on a receipt, through `undo` (both None while writes
are off). Message
text is read from the stored raw events and never logged. Clicks are M2 (review). Every edit and deletion is noted
by message key, in whatever order Slack delivers it, and a post is read as its messages are now: each one as its
newest edit shows it (by the edit's own timestamp, not by arrival), and a deleted one left out. A post with a change
that cannot be read (a tombstone, an edit without a stamp, a body that expired) is never read from its original
text: it is withheld as `changed`.

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

import hashlib
import json
import logging
import re
import sqlite3
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from stockthread.applier import plan_views, undo_refusal
from stockthread.apply import Applied
from stockthread.config import Settings
from stockthread.core import extract_rules
from stockthread.core.answers import Effects, Given, Typed, replay
from stockthread.core.grouping import seconds
from stockthread.core.marker import is_marker_id
from stockthread.core.matching import Catalog, match_location, match_part
from stockthread.core.model import (
    CandidateMovement,
    Extraction,
    Flag,
    LineItem,
    Match,
    MatchStatus,
    MessageInput,
    MovementType,
    Operation,
    PostFlag,
    PostKey,
    ReasonCode,
    ReasonSource,
    RequestKey,
    ReviewReason,
    operation_for,
)
from stockthread.core.questions import (
    Question,
    choice_json,
    choices,
    pick_question,
    question_from_json,
    question_json,
    reason_line,
)
from stockthread.core.requests import UNDOABLE, RequestState
from stockthread.core.resolve import LineStock, SourcePick, StockItem, resolve_lines
from stockthread.core.typed_answers import read_typed_answer, replay_typed, takes_typed_answer
from stockthread.core.validate import photo_not_read, validate
from stockthread.inventree import InvenTreeError
from stockthread.llm.extract import NoReading, ProviderError
from stockthread.plan_record import NotRebuilt, plan_of
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
    CLICK_FEEDBACK,
    UNDO_ACTION,
    MovementView,
    click_feedback,
    dry_run_reply,
    gate_reasons,
    question_closed,
    question_message,
    receipt,
    receipt_replaced,
    rejection_notice,
    retraction_notice,
    with_question,
    without_question,
)
from stockthread.store import (
    MAX_EVENT_ATTEMPTS,
    Answered,
    Button,
    DryRunOutcome,
    EventStatus,
    LockLost,
    NewPlan,
    NewQuestion,
    QuestionStatus,
    ReviewItem,
    StateConflict,
    Store,
    WaitingPost,
    new_button_id,
)
from stockthread.store.db import DryRunRecord

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
MAX_NOTED = 1000
"""Thread replies remembered as told (`Worker._note_not_allowed`); the memory starts over beyond this."""
HEARTBEAT_SECONDS = 10.0
"""The worker beats the instance lock at most this often, before each batch and each post, so one long step
(Slack or InvenTree slow to answer) never lets the lock look stale."""
MAX_POSTS_PER_STEP = 20
DRAIN_BATCH = 50
EVENT_RETRY_SECONDS = (5, 30, 120)
"""The wait before each retry of an event that failed transiently, by the attempt that failed. Within
MAX_EVENT_ATTEMPTS and MAX_EVENT_RETRY_DELAY."""
MAX_DRAIN_BATCHES = 20
"""A step drains at most this many batches. While events are still pending no post is due, so a backlog
never splits siblings."""

WORK_RETRY_SECONDS = 30
"""After a step of the apply protocol failed and left its request as it was (an outage), no step is tried before
this time."""
STUCK_RETRY_SECONDS = 60 * 60
"""A request or receipt the connector cannot act on (its record cannot be rebuilt, or is not what it should be) is
logged as an error, and tried again only after this long."""
RECEIPT_TRIES = 5
"""Failed tries in a row of one receipt after which each further failure is logged as an error for an operator."""
RECEIPT_CHECK_SECONDS = 5
"""A receipt whose Slack call has not returned is looked at again after this long."""

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
    answers: int | None = None
    """The newest answered question the decision knew of (None: the newest when it is recorded)."""
    outcome: DryRunOutcome = DryRunOutcome.REPLIED
    """REPLIED, or RETRACTED for a notice that the reply above no longer stands."""
    checks: int = 0
    next_check: Decimal = Decimal(0)
    question: NewQuestion | None = None
    """The question the reply asks, stored with its buttons once the reply is posted."""
    plan: NewPlan | None = None
    """The plan the reply makes when it could apply in full, stored with it (ADR 0016)."""


Undoer = Callable[[RequestKey, MovementView, str], Applied]
"""(request, the movement its request was built from, the Slack user who asked) -> where `apply.undo_apply` left the
request, run to the end (run.py gives it the InvenTree client, the writer lock and the instance lock's owner)."""

Stepper = Callable[[RequestKey], Applied]
"""request -> where `applier.work_request` left it (run.py gives it the InvenTree client, the writer lock, the
instance lock's owner, what the gate reads now and the settle time)."""

UNDO_FEEDBACK = {
    RequestState.UNDONE: "Undone: the stock is back as it was before this movement, and was read back to check it.",
    RequestState.UNDO_FAILED: "Not undone: InvenTree refused the undo, or the connector would not send it. Look at the "
    "movement in InvenTree.",
    RequestState.UNDO_UNKNOWN: "The undo was sent, but whether it went through is not known yet. Please do not undo it "
    "by hand until it has been checked.",
    RequestState.VERIFY_FAILED: "The undo was sent, but its check found a problem. Look at the movement in InvenTree "
    "before relying on it.",
}
"""What the clicker alone is told about each state an undo that started ends in."""
UNDO_WRITES_OFF = "Not undone: writes are turned off in the connector's settings. Undo it in InvenTree instead."
UNDO_ALREADY = "This movement was undone already."
UNDO_ERROR = "Not undone: something went wrong before anything was changed. Please try again in a few minutes."
UNDO_NOT_REBUILT = "Not undone: the connector cannot rebuild this movement from its records. Undo it in InvenTree."

StockReader = Callable[[Sequence[tuple[int, int]]], Mapping[tuple[int, int], Sequence[StockItem]]]
"""(part, location) pairs -> the stock items of that part in that location itself, in stock (`stock_items`)."""

_QUANTITY_UNSETTLED = frozenset(
    {
        ReasonCode.QUANTITY_MISSING,
        ReasonCode.QUANTITY_VAGUE,
        ReasonCode.PACK_UNIT_AMBIGUOUS,
        ReasonCode.QUANTITY_FROM_PHOTO,
    }
)
"""A line with one of these has no quantity to pick stock for yet: its question comes first (design section 4)."""


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
    stock: StockReader | None = None
    """Reads the stock items of (part, location) pairs, for source selection. None: no line's stock is read. A
    failure is an outage, as for the catalog: it propagates, and the post waits."""
    stopping: Callable[[], bool] = lambda: False
    """Checked between posts, so a stop request does not wait for a whole backlog."""
    llm: PostExtractor | None = None
    """Reads the posts the rule-based extractor declines. None: those posts get no reply."""
    undo: Undoer | None = None
    """Runs the undo an Undo click asks for, once every check passed. None while writes are off: no click writes, and
    no receipt offers an Undo button."""
    apply: Stepper | None = None
    """Runs the step of the apply protocol a request is due for (`work_due`). None while writes are off: no request
    is written, read back or recovered."""
    _work_quiet_until: Decimal = Decimal(0)
    _held: dict[RequestKey | tuple[PostKey, int], tuple[int, Decimal]] = field(default_factory=dict)
    """Requests, and plans (post, revision) due a receipt, whose last try failed: the failures in a row, and the time
    before which they are not tried again. One that keeps failing never holds the others back."""
    _receipts_quiet_until: Decimal = Decimal(0)
    _receipts_running: dict[tuple[PostKey, int], tuple[tuple[RequestState, ...], Mapping[str, Any]]] = field(
        default_factory=dict
    )
    """Receipts whose Slack call has not returned, with the states they report and the message: the next try sends
    the same message, so it takes that call's result (run.DeadlinePoster) instead of posting again."""
    _llm_read: dict[PostKey, dict[tuple[MessageInput, ...], Extraction]] = field(default_factory=dict)
    """What the model read, and the messages as it read them, kept until the post is finished: a reply that is
    tried again is not read again. A post that has gained, lost or changed a message since is. A post is read with
    and without its answer messages (answers by thread reply), so each set read is kept."""
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
    _noted: set[tuple[str, str]] = field(default_factory=set)
    """(channel, ts): thread replies whose author was told they may not answer."""
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
        if not empty:
            return done
        return done + self.reply_due() + self.work_due() + self.post_receipts() + self.take_down_buttons()

    def drain(self) -> tuple[int, bool]:
        """Handle pending events: how many, and whether the queue is empty now. An event put off after a transient
        failure keeps it from being empty until it is finished, so no post is due without it."""
        done = 0
        self.store.requeue_unfinished(self.owner, own=True)  # what an error cut a drain off from, if anything
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
                    if _transient(error) and event.attempts < MAX_EVENT_ATTEMPTS:
                        after = timedelta(seconds=EVENT_RETRY_SECONDS[event.attempts - 1])
                        self.store.retry_event_later(self.owner, event.id, after)
                        done -= 1
                        continue
                    status, outcome = EventStatus.FAILED, _code(error)
                self.store.finish_event(self.owner, event.id, status, outcome)
                if self._feedback is not None and not self.stopping():
                    self._tell(event.id, self._feedback)
            done += len(events)
            if len(events) < DRAIN_BATCH:
                return done, not self.store.events_pending()
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
        if click.action_id == UNDO_ACTION:
            return self._handle_undo(click)
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

    def _handle_undo(self, click: Click) -> tuple[EventStatus, str]:
        """Run the undo an Undo click on a receipt asks for (design section 7, "Undo"), after the checks against the
        stored request: the button's value is a request's marker id, the click came from the newest receipt of that
        request's plan (`PlanRow.receipt_ts`) in its channel, the clicker is the poster or an approver, and the store
        allows an undo now (`applier.undo_refusal`). `apply.undo_apply` checks the stock again under the writer lock.
        A click handled again (Slack's retry, or a crash after the undo started) is refused by the store: an earlier
        undo that was not refused blocks every later one, so two clicks move stock once. The clicker alone is told
        what came of it."""
        key = self.store.request_with_marker(click.value) if is_marker_id(click.value) else None
        if key is None:
            return self._undo_told(click, EventStatus.DROPPED, "unknown_button", CLICK_FEEDBACK["unknown_button"])
        plan = self.store.plan(key.post, key.revision)
        if (
            key.post.workspace_id != self.settings.slack.workspace_id
            or key.post.channel_id != click.channel
            or plan.receipt_ts is None
            or plan.receipt_ts != click.message_ts
        ):
            return self._undo_told(click, EventStatus.DROPPED, "other_message", CLICK_FEEDBACK["other_message"])
        try:
            rebuilt = plan_views(self.store, plan)
        except NotRebuilt as error:
            log.warning("undo of request %s: %s", key, error)
            return self._undo_told(click, EventStatus.DROPPED, "not_rebuilt", UNDO_NOT_REBUILT)
        refused = undo_refusal(
            self.store,
            key,
            user=click.user,
            poster=rebuilt.poster,
            approvers=self.settings.slack.approvers,
            max_age=timedelta(hours=self.settings.undo.max_age_hours),
        )
        if refused is not None:
            # A second click, or this click handled again after its undo (a crash before the event was finished).
            text = UNDO_ALREADY if self.store.request(key).state is RequestState.UNDONE else f"Not undone: {refused}."
            return self._undo_told(click, EventStatus.DROPPED, "undo_refused", text)
        if self.undo is None:
            return self._undo_told(click, EventStatus.DROPPED, "writes_off", UNDO_WRITES_OFF)
        before = self.store.request(key).state
        self.beat(now=True)  # the undo reads and writes InvenTree under its own deadlines
        try:
            applied = self.undo(key, rebuilt.views[key.sequence - 1], click.user)
        except LockLost:
            raise
        except Exception as error:
            if self.store.request(key).state is before:
                # Nothing was stored: the click may be handled again. Told only if it is not (`drain`).
                self._undo_told(click, EventStatus.FAILED, "", UNDO_ERROR)
                raise
            _log_failure("undo of request", key, error)
            applied = Applied(self.store.request(key).state, f"failed: {_code(error)}")
        log.info("undo of request %s: %s (%s)", key, applied.state, applied.why)
        if applied.state is before and applied.why.startswith("no undo: "):  # refused before its intent was stored
            why = applied.why.removeprefix("no undo: ")
            return self._undo_told(click, EventStatus.DROPPED, "undo_refused", f"Not undone: {why}.")
        text = UNDO_FEEDBACK.get(applied.state, f"The undo ended as {applied.state}. Look at it in InvenTree.")
        return self._undo_told(click, EventStatus.DONE, applied.state.value, text)

    def work_due(self) -> int:
        """Run the step of the apply protocol each request is due for (`Store.requests_to_work`; design section 7)
        through `apply`: the write of an `approved` request, the read-back of an `applied` one, the recovery of an
        `unknown` apply or undo once settled. Returns how many requests changed state. No step starts while an
        event waits in the queue (one put off after a failure too): an edit or deletion that came while the worker was
        busy may take an approval back, so `drain` handles it first.

        A step that raises and leaves its request as it was changed nothing, so it is safe to try again: an unknown
        write is only ever recovered, never sent again (ADR 0015). That request is held back, for WORK_RETRY_SECONDS
        doubled with each failure in a row up to STUCK_RETRY_SECONDS, so it never holds the others back. An outage
        (InvenTree, the network, the disk) also pauses every step for WORK_RETRY_SECONDS. Any other error is one an
        operator must see, and is logged as one. A step that raises after its request moved on is not held back: the
        next step goes on from the new state."""
        now = self._now()
        if self.apply is None or self.stopping() or now < self._work_quiet_until:
            return 0
        done = 0
        for key in self.store.requests_to_work():
            if self.stopping():
                break
            if now < self._held.get(key, (0, now))[1]:
                continue
            if self.store.events_pending():
                break  # an edit or a deletion that came meanwhile is handled first: it may take the approval back
            before = self.store.request(key).state
            self.beat(now=True)  # the step reads and writes InvenTree under its own deadlines
            try:
                applied = self.apply(key)
            except LockLost:
                raise
            except Exception as error:
                if self.store.request(key).state is not before:
                    _log_failure("request", key, error)
                    self._held.pop(key, None)
                    done += 1
                    continue
                failed_at = self._now()  # a step can take long: its backoff counts from when it failed
                failures = self._hold(key, failed_at, WORK_RETRY_SECONDS)
                if _outage(error):
                    log.warning("request %s: the step failed (%s), tried again later", key, type(error).__name__)
                    self._work_quiet_until = failed_at + WORK_RETRY_SECONDS
                    break
                log.error(
                    "request %s: the step failed %d times in a row (%s), an operator must look at it",
                    key,
                    failures,
                    _described(error),
                )
                continue
            self._held.pop(key, None)
            if applied.state is not before:
                done += 1
                log.info("request %s: %s (%s)", key, applied.state, applied.why)
        return done

    def post_receipts(self) -> int:
        """Post the receipt each plan is due (`Store.receipts_due`; design section 7, step 6) in its post's thread, and
        record it with the states it reports (`Store.receipt_posted`): from then on only an Undo click on it counts.
        A movement gets an Undo button when `undo` is set and an undo of it may be asked for now (`Store.undo_refusal`;
        whether the clicker may is checked at the click). Returns how many were posted.

        A receipt that fails is held back, with a backoff that grows with its failures in a row, and is never given
        up on: after RECEIPT_TRIES each failure is logged as an error. One that keeps failing never holds the others
        back. A rate limit pauses every post for as long as Slack asks, and an outage (the network, InvenTree's
        catalog) every receipt. A call that has not returned is tried again with the same message, which takes its
        result. A receipt whose movements changed while it was on its way is not recorded but edited to say a newer
        one follows; the plan is then due again. A receipt Slack gave no ts for is not posted again by this process.
        """
        now = self._now()
        if self.stopping() or self._running or now < max(self._quiet_until, self._receipts_quiet_until):
            return 0  # a dry-run reply on its way holds receipts back, as a receipt holds replies (`reply_due`)
        due = [*self._receipts_running]
        due += [plan for plan in self.store.receipts_due() if plan not in self._receipts_running]
        done = 0
        for plan in due:
            if self.stopping():
                break
            if now < self._held.get(plan, (0, now))[1]:
                continue
            post, revision = plan
            try:
                made = self._receipts_running.get(plan) or self._receipt(post, revision)
            except Exception as error:
                if _outage(error):
                    log.warning("post %s: no receipt now (%s), tried again later", post.ts, type(error).__name__)
                    self._receipts_quiet_until = self._now() + WORK_RETRY_SECONDS
                    return done
                failures = self._hold(plan, self._now(), WORK_RETRY_SECONDS)
                log.error(
                    "post %s: no receipt for plan revision %d (%d times in a row: %s), an operator must look at it",
                    post.ts,
                    revision,
                    failures,
                    _described(error),
                )
                continue
            states, body = made
            self.beat(now=True)
            try:
                response = self.slack.chat_postMessage(
                    channel=post.channel_id,
                    thread_ts=post.ts,
                    text=body["text"],
                    blocks=body["blocks"],
                    unfurl_links=False,
                    unfurl_media=False,
                )
            except PosterBusy:
                return done  # another call is still on its way: nothing was tried
            except PostStillRunning:
                self._receipts_running[plan] = made
                self._receipts_quiet_until = self._now() + RECEIPT_CHECK_SECONDS
                log.warning("post %s: receipt still on its way to Slack", post.ts)
                return done
            except Exception as error:
                self._receipts_running.pop(plan, None)
                failed_at = self._now()  # Retry-After counts from Slack's answer, not from when the batch began
                failures = self._hold(plan, failed_at, 2)
                asked = _retry_after(error)
                level = logging.ERROR if failures > RECEIPT_TRIES else logging.WARNING
                log.log(level, "post %s: receipt failed (%s), %d in a row", post.ts, _slack_code(error), failures)
                if asked:
                    self._quiet_until = failed_at + asked  # for every post, not only this one
                    return done
                if _outage(error):
                    self._receipts_quiet_until = self._held[plan][1]
                    return done
                continue  # Slack refused this one (an archived channel, say): the others go on
            self._receipts_running.pop(plan, None)
            self._held.pop(plan, None)
            ts = response.get("ts")
            if not (isinstance(ts, str) and _SLACK_TS.fullmatch(ts)):
                # Posted, but nothing to check a click against, and posting again could repeat it: an operator looks.
                self._held[plan] = (0, Decimal("Infinity"))
                log.error(
                    "post %s: Slack returned no message ts for the receipt of plan revision %d", post.ts, revision
                )
                continue
            try:
                self.store.receipt_posted(self.owner, post, revision, ts, states)
            except StateConflict:
                log.info("post %s: receipt of plan revision %d is out of date: replaced", post.ts, revision)
                self._replace_receipt(post, ts)
                continue
            done += 1
            log.info("post %s: receipt of plan revision %d posted", post.ts, revision)
        return done

    def _hold(self, which: RequestKey | tuple[PostKey, int], now: Decimal, first: int) -> int:
        """Hold `which` back after a failure: `first` seconds, doubled with each failure in a row, at most
        STUCK_RETRY_SECONDS. Returns its failures in a row."""
        failures = self._held.get(which, (0, now))[0] + 1
        self._held[which] = (failures, now + min(first * 2 ** min(failures - 1, 16), STUCK_RETRY_SECONDS))
        return failures

    def _receipt(self, post: PostKey, revision: int) -> tuple[tuple[RequestState, ...], Mapping[str, Any]]:
        """The states the plan's receipt reports, and the receipt. Raises NotRebuilt when its record cannot be
        rebuilt, and what reading the catalog raises."""
        plan = self.store.plan(post, revision)
        rebuilt = plan_views(self.store, plan)
        catalog = self.catalog()
        max_age = timedelta(hours=self.settings.undo.max_age_hours)
        undo = [
            row.marker_id
            if self.undo is not None
            and row.state in UNDOABLE
            and self.store.undo_refusal(row.key, max_age=max_age) is None
            else None
            for row in plan.requests
        ]
        states = tuple(row.state for row in plan.requests)
        body = receipt(
            rebuilt.views,
            states,
            [row.marker_id for row in plan.requests],
            {part.pk: part.name for part in catalog.parts},
            {location.pk: location.path for location in catalog.locations},
            inventree_url=self.settings.inventree.url,
            approved_by=plan.approved_by,
            undo=undo,
        )
        return states, body

    def _replace_receipt(self, post: PostKey, ts: str) -> None:
        """Best effort: a click on the old receipt is refused whatever it shows (`_handle_undo`)."""
        body = receipt_replaced()
        try:
            self.slack.chat_update(channel=post.channel_id, ts=ts, text=body["text"], blocks=body["blocks"])
        except Exception as error:
            _log_failure("replaced receipt of post", post.ts, error)

    def _undo_told(self, click: Click, status: EventStatus, outcome: str, text: str) -> tuple[EventStatus, str]:
        self._feedback = _Feedback(click.channel, click.user, click.thread_ts or click.message_ts, text)
        return status, outcome

    def _tell(self, event_id: int | str, feedback: _Feedback, what: str = "click feedback") -> None:
        """Send a note to the clicker (or to the author of a thread reply). Best effort: the click is finished
        already. A note that fails is logged and not tried again, and no note is sent for NOTE_QUIET_SECONDS after
        it."""
        now = self._now()
        if now < self._notes_quiet_until:
            log.info("%s %s: not sent, a recent note failed", what, event_id)
            return
        self.beat()  # the note has its own deadline (run.NOTE_DEADLINE_SECONDS)
        try:
            self.slack.chat_postEphemeral(
                channel=feedback.channel, user=feedback.user, thread_ts=feedback.thread_ts, text=feedback.text
            )
        except Exception as error:
            _log_failure(what, event_id, error)
            self._notes_quiet_until = now + NOTE_QUIET_SECONDS

    def take_down_buttons(self) -> int:
        """Take a question out of the reply it was asked in, once it is answered, a newer reply replaced it, or its
        plan expired (design section 4, "Interactivity"): the reply is read back from Slack, and edited to end with a
        line that says what became of the question. Best effort, one reply per TAKE_DOWN_SPACING_SECONDS, fewest
        failed tries first; the clicks are checked against the store whatever Slack shows. Returns 1 when a reply
        was edited or given up on."""
        now = self._now()
        if self.stopping() or now < max(self._quiet_until, self._take_down_quiet_until):
            return 0
        # From the same time as a click's expiry (`_refuse_answer`).
        self.store.expire_questions(self.owner, now - self.settings.slack.plan_expiry_hours * 3600)
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
            # Every failure pauses all take-downs, the last try of this question too: the next question waits
            # out the same outage or rate limit instead of spending its tries in it.
            wait = max(TAKE_DOWN_BACKOFF_SECONDS * 2 ** (min(tries, TAKE_DOWN_TRIES) - 1), _retry_after(error))
            self._take_down_quiet_until = now + wait
            if tries < TAKE_DOWN_TRIES:
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
        # Listed while open only once expired (closed_with_buttons).
        expired = item.status is QuestionStatus.OPEN
        closing = question_closed(answered_by if item.status is QuestionStatus.ANSWERED else None, expired=expired)
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
        # An answer taken as its plan expired is told it came too late, for one more plan window (issue #75).
        for post in self.store.posts_waiting_for_dry_run(
            revisable_since=now - expiry, late_answers_since=now - 2 * expiry
        ):
            sent = self._running.get(post.key)
            if sent is not None:
                if sent.next_check <= now and not self.stopping():
                    self.beat()
                    self._post(post.key, now, sent)
                    done += 1
                continue
            if self._running or self._receipts_running:
                continue  # one call at a time: a reply or a receipt is still on its way
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
        answers = self.store.post_answers_through(key)
        age = now - seconds(post.newest_ts)
        # The first dry run fixes when the plan expires (stored as the post's `plan_from`): a revision's movement is
        # as old as the post as first read, and a new reply does not make a week-old movement current.
        plan_from = post.newest_ts if post.revision == 1 else post.plan_from or post.key.ts
        since = seconds(plan_from)
        read: tuple[Sequence[MessageInput], str, bool, Mapping[str, str]] | None = None
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
                post,
                now,
                DryRunOutcome.WITHHELD,
                None,
                {"withheld": why},
                why=why,
                read=held,
                changes=changes,
                answers=answers,
            )
            return True
        all_messages, poster, caught_up, authors = read
        everything = tuple(message.ts for message in all_messages)
        history = self.store.dry_run_history(key) if post.revision > 1 else []
        # Answers by thread reply: a message counted as the answer to a question of the reading that stands is kept
        # out of what the extractor reads. A new message may be one (`candidate`); it is judged on that reading.
        # Then the post is read with it, then with every answer message: the reading differs, answers start over.
        try:
            typed = self._typed(key, history, all_messages, answers)
            candidate = self._candidate(key, history, all_messages, authors, typed, changes)
        except LockLost:
            raise
        except Exception as error:  # a stored record or question that cannot be read: this post only
            self._broken(post, now, error, read=everything, changes=changes, answers=answers)
            return True
        tries: list[dict[int, MessageInput]] = [{**typed, candidate[0].id: candidate[1]}] if candidate else []
        tries += [typed, {}] if typed else [{}]
        catalog: Catalog | None = None
        late = caught_up or age > LATE_AFTER_SECONDS
        standing = self._standing(post)
        for answering in tries:
            kept_out = {message.ts for message in answering.values()}
            messages = [message for message in all_messages if message.ts not in kept_out]
            seen = tuple(message.ts for message in messages)
            try:
                # Only answers are new: the post reads as it did, so the reading that stands is kept rather than
                # asked for again (a model may read the same words otherwise, and the answers would no longer fit).
                kept = _kept_reading(history, seen, changes)
                extraction, unheard = (kept, "") if kept is not None else self._extract(key, messages, now, ask)
            except LockLost:
                raise
            except Exception as error:
                self._broken(post, now, error, read=everything, changes=changes, answers=answers)
                return True
            if extraction is None:
                return False  # waits for the LLM provider, or for the queue to be drained after its call

            def record(
                views: Sequence[MovementView],
                late: bool,
                extraction: Extraction = extraction,
                messages: Sequence[MessageInput] = messages,
                unheard: str = unheard,
            ) -> dict[str, Any]:
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

            unread = extraction.declined or not extraction.movements
            if answering and unread:
                continue  # the post reads otherwise without its answer messages: answers start over
            if unread:
                log.info("post %s: no reply (%s)", key.ts, "declined" if extraction.declined else "not a movement")
                # A model that did not answer, or gave no reading, says nothing about the thread: the notice says so.
                self._conclude(
                    post,
                    now,
                    DryRunOutcome.SILENT,
                    extraction.extractor,
                    record((), False),
                    why="unread" if unheard else None,
                    read=everything,
                    changes=changes,
                    answers=answers,
                )
                return True
            if catalog is None:
                catalog = self.catalog()
                self.beat()  # a catalog read can take a while
            try:
                views = self._views(extraction, messages, poster, catalog, late)
                reading = _digest(record(views, late))
                if answering and reading != _row(history)[0]:
                    continue  # the post reads otherwise: answers start over
                effects = self._replay(key, history, reading, answers, views, answering, catalog, poster)
                if candidate and candidate[0].id in answering:
                    author = authors[candidate[1].ts]
                    taken = self._take_answer(
                        candidate, views, effects, extraction, all_messages, late, catalog, poster, author
                    )
                    if not taken:
                        continue  # not an answer, or not from someone who may answer: read with the post
                    answers = self.store.post_answers_through(key)
                    effects = self._replay(key, history, reading, answers, views, answering, catalog, poster)
                views = self._answered(views, effects, extraction, all_messages, late)
            except LockLost:
                raise
            except Exception as error:
                self._broken(post, now, error, extraction.extractor, read=everything, changes=changes, answers=answers)
                return True
            break
        if extraction is None or catalog is None:  # the last try reads every message and always ends the loop
            raise RuntimeError("no reading after every try")
        # Stock is read last, for the parts and places as answered, and is not part of the reading: answers stay
        # with a post whose stock changed. Outside the `try`: a failed read is an outage, and the post waits.
        stock = self._read_stock(views) if effects.rejected is None else {}
        try:
            views = _with_stock(views, stock, effects.stock)
            decided = {
                **record(views, late),
                "reading": reading,  # before the answers: what a later reading is compared with
                "answers": effects.applied,
                "ignored": effects.ignored,
                "rejected": effects.rejected,
                "extracted": [movement.movement_type.value for movement in extraction.movements],  # before answers
            }
            parts = {part.pk: part.name for part in catalog.parts}
            locations = {location.pk: location.path for location in catalog.locations}
            if effects.rejected is not None:
                body = rejection_notice()
            else:
                body = dry_run_reply(views, parts, locations, revised=standing is not None, answered=kept is not None)
        except Exception as error:
            self._broken(post, now, error, extraction.extractor, read=everything, changes=changes, answers=answers)
            return True
        # The one question the post needs goes in the reply, with buttons whose ids are stored with it. A
        # question that cannot be put is left out: the reply still says what needs a human. A rejected reading
        # asks nothing: a corrected post or thread reply is read afresh.
        asked = None
        try:
            question = pick_question(views) if effects.rejected is None else None
            if question is not None:
                buttons = choices(question)
                ids = [new_button_id() for _ in buttons]
                body = with_question(body, question_message(question, views, parts, locations, ids))
                asked = NewQuestion(
                    question_json(question),
                    question.approver_only,
                    [(button_id, choice_json(choice)) for button_id, choice in zip(ids, buttons, strict=True)],
                    poster=poster,
                    expires_from=plan_from,  # the plan's one expiry, as `since` above
                )
        except Exception as error:
            _log_failure("question", key.ts, error)
        if standing is not None and _same_reading(standing, decided):
            log.info("post %s: revision %d reads as the reply that stands", key.ts, post.revision)
            self._finish(
                key,
                DryRunOutcome.UNCHANGED,
                extraction.extractor,
                decided,
                post.revision,
                everything,
                changes,
                answers,
                plan=_plan(key, standing),  # the reply that stands is the one a plan acts on
            )
            return True
        # Closed first: a sibling that arrives from here on starts its own post. A crash after posting and
        # before the outcome is recorded posts the reply again at restart; a duplicate beats a silent drop.
        self.store.close_siblings(self.owner, key)
        self._post(
            key,
            now,
            _Sent(
                extraction.extractor,
                decided,
                body,
                post.revision,
                everything,
                changes,
                answers,
                question=asked,
                plan=_plan(key, decided),
            ),
        )
        return True

    def _replay(
        self,
        key: PostKey,
        history: Sequence[DryRunRecord],
        reading: str,
        through: int,
        views: Sequence[MovementView],
        typed: Mapping[int, MessageInput],
        catalog: Catalog,
        poster: str,
    ) -> Effects:
        """The effects of the answers given about this reading: those to questions asked by the newest dry runs
        in a row that read the post the same way, up to the newest answer known when the post was read. A dry run
        that read it otherwise (or did not read it) ends the row: answers before it are kept, and not applied.
        An answer by thread reply counts only while its message is kept out of the reading (`typed`, by question)
        and is not deleted; it is read as the message is now."""
        same: set[int] = set()
        for found in reversed(history):
            if found.outcome not in (DryRunOutcome.REPLIED, DryRunOutcome.UNCHANGED) or not found.result:
                break
            if _digest(found.result) != reading:
                break
            same.add(found.revision)
        shape = [len(view.movement.line_items) for view in views]
        stored: list[Given | tuple[int, Question, MessageInput, str, str | None]] = []
        unreadable: list[int] = []
        for item, answer, by in self.store.answers(key, through) if same else ():
            if item.revision not in same:
                continue
            try:
                if "message" not in answer:
                    stored.append(Given.stored(item.id, item.question, answer, by))
                    continue
                question = question_from_json(item.question)
                message = typed.get(item.id)
                if message is None or message.deleted or question.movement >= len(views):
                    unreadable.append(item.id)  # deleted, edited since, or read with the post: asked again
                else:
                    stored.append((item.id, question, message, by, item.poster_id))
            except ValueError:
                unreadable.append(item.id)
        # A typed answer is read on the movement as the buttons left it, as it was judged: a "0" typed for a count
        # is a quantity only once the type picked is a count.
        types = replay([entry for entry in stored if isinstance(entry, Given)], shape, self.settings.slack.approvers)
        given: list[Given | Typed] = []
        for entry in stored:
            if isinstance(entry, Given):
                given.append(entry)
                continue
            item_id, question, message, by, asked_poster = entry
            movement = views[question.movement].movement
            if question.movement in types.types:
                movement = replace(movement, movement_type=types.types[question.movement])
            says = replay_typed(message.text, question, movement, catalog, poster)
            if says is None:
                unreadable.append(item_id)
            else:
                given.append(Typed(item_id, question, says, message.text, by, asked_poster))
        effects = replay(given, shape, self.settings.slack.approvers)
        effects.ignored = sorted([*effects.ignored, *unreadable])
        return effects

    def _typed(
        self, key: PostKey, history: Sequence[DryRunRecord], messages: Sequence[MessageInput], through: int
    ) -> dict[int, MessageInput]:
        """The messages counted as answers to questions of the reading that stands (the newest dry runs in a row
        that read the post the same way), by question. A message edited since it was judged is not one: it is a
        thread message from then on, read with the post. A deleted one stays out of the reading, and its question
        is asked again."""
        row = _row(history)[1]
        if not row:
            return {}
        by_ts = {message.ts: message for message in messages}
        found: dict[int, MessageInput] = {}
        for item, answer, _ in self.store.answers(key, through):
            ts = answer.get("message")
            message = by_ts.get(ts) if isinstance(ts, str) and item.revision in row else None
            if message is not None and message.edited_ts == answer.get("edited"):
                found[item.id] = message
        return found

    def _candidate(
        self,
        key: PostKey,
        history: Sequence[DryRunRecord],
        messages: Sequence[MessageInput],
        authors: Mapping[str, str],
        typed: Mapping[int, MessageInput],
        changes: int,
    ) -> tuple[ReviewItem, MessageInput, Question, bool] | None:
        """The open question and the message that may answer it by thread reply, and whether its author may answer
        it: the question takes one, and the post is as its newest dry run read it except for this one message,
        posted after the reply that asked. Two new messages, or an edit, and the thread is read with the post as
        before. A message whose author may not answer is a thread message; it is judged only to tell its author."""
        item = self.store.open_question(key)
        last = history[-1] if history else None
        if item is None or item.message_ts is None or last is None or not last.result:
            return None
        if last.outcome not in (DryRunOutcome.REPLIED, DryRunOutcome.UNCHANGED) or last.changes_through != changes:
            return None
        try:
            question = question_from_json(item.question)
        except ValueError:
            return None
        if not takes_typed_answer(question):
            return None
        kept_out = {message.ts for message in typed.values()}
        read = last.result.get("messages") or []
        new = [message for message in messages if message.ts not in kept_out and message.ts not in read]
        if len(new) != 1 or [m.ts for m in messages if m.ts not in kept_out and m is not new[0]] != read:
            return None
        message = new[0]
        if message.deleted or not message.text.strip() or seconds(message.ts) <= seconds(item.message_ts):
            return None
        author = authors.get(message.ts)
        if author is None:
            return None
        allowed = author in self.settings.slack.approvers or (
            not item.approver_only and item.poster_id is not None and author == item.poster_id
        )
        return item, message, question, allowed

    def _take_answer(
        self,
        candidate: tuple[ReviewItem, MessageInput, Question, bool],
        views: Sequence[MovementView],
        effects: Effects,
        extraction: Extraction,
        messages: Sequence[MessageInput],
        late: bool,
        catalog: Catalog,
        poster: str,
        author: str,
    ) -> bool:
        """Whether the candidate message reads cleanly as the answer to its question, judged on the reading that
        stands with the answers given so far, and was recorded as that answer."""
        item, message, question, allowed = candidate
        answered = self._answered(views, effects, extraction, messages, late)
        if not 0 <= question.movement < len(answered):
            return False
        movement = answered[question.movement].movement
        if read_typed_answer(message.text, question, movement, catalog, poster) is None:
            return False
        if not allowed:
            self._note_not_allowed(item, message, author)
            return False
        result = self.store.answer_by_message(self.owner, item, message.ts, message.edited_ts, author)
        if result is not Answered.ANSWERED:
            log.info("post %s: thread reply not taken as an answer (%s)", item.post.ts, result.value)
            return False
        log.info("post %s: question answered by thread reply", item.post.ts)
        return True

    def _note_not_allowed(self, item: ReviewItem, message: MessageInput, author: str) -> None:
        """Tell the author of a message that reads cleanly as the answer, but who may not answer, why it was not
        taken: the note a click would get. Once per message while the worker runs; a restart may tell them again."""
        noted = (item.channel_id, message.ts)
        if noted in self._noted or self.stopping():
            return
        if len(self._noted) >= MAX_NOTED:
            self._noted.clear()
        self._noted.add(noted)
        log.info("post %s: thread reply from someone who may not answer", item.post.ts)
        text = click_feedback(
            "not_allowed",
            approver_only=item.approver_only,
            approvers=bool(self.settings.slack.approvers),
            expiry_hours=self.settings.slack.plan_expiry_hours,
        )
        if text is not None:
            self._tell(item.post.ts, _Feedback(item.channel_id, author, item.post.ts, text), "reply feedback")

    def _answered(
        self,
        views: Sequence[MovementView],
        effects: Effects,
        extraction: Extraction,
        messages: Sequence[MessageInput],
        late: bool,
    ) -> list[MovementView]:
        """The views with the answers' effects: a picked type is validated again, a picked or confirmed record
        replaces its match, and a cleared reason is left out. A quantity or serials typed in the thread replace the
        line's own; validation then runs again on that movement, with the answer messages' text in the post."""
        found = []
        for n, view in enumerate(views):
            movement, reasons = view.movement, view.reasons
            if n in effects.types:
                movement = replace(movement, movement_type=effects.types[n])
            if n in effects.retyped:
                movement = replace(
                    movement,
                    line_items=tuple(
                        _typed_line(effects, n, line, item) for line, item in enumerate(movement.line_items)
                    ),
                )
            if n in effects.types or n in effects.retyped:
                reasons = self._reasons(movement, extraction, messages, late)
            found.append(
                MovementView(
                    movement=movement,
                    items=[effects.parts.get((n, line), match) for line, match in enumerate(view.items)],
                    source=effects.places.get((n, "source"), view.source),
                    destination=effects.places.get((n, "destination"), view.destination),
                    reasons=tuple(
                        reason
                        for reason in reasons
                        if (n, reason_line(reason, movement), reason.code, reason.detail) not in effects.cleared
                        and (n, reason_line(reason, movement), reason.code) not in effects.settled
                    ),
                    named={key[1]: text for key, text in effects.named.items() if key[0] == n},
                )
            )
        return found

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
        answers: int | None = None,
    ) -> None:
        """Finish a post that gets no dry-run reply. A revision whose post had a reply standing posts a notice
        that it no longer does, except when the post is too old: its reply has expired by then. A post too old
        with an answer no dry run read (taken just before the plan expired) says once that it came too late, so
        the thread never shows an answer that nothing came of."""
        reason = why or ("silent" if outcome is DryRunOutcome.SILENT else "failed")
        if reason == "too_old" and answers is not None and answers > self.store.post_answers_read(post.key):
            reason = "too_late"
        # No reply standing with a late answer: a newer record took the reply back, and closed the question then.
        if reason == "too_old" or self._standing(post) is None:
            self._finish(post.key, outcome, extractor, result, post.revision, read, changes, answers)
            return
        log.info("post %s: revision %d takes the reply back (%s)", post.key.ts, post.revision, reason)
        decided = {**result, "retracted": reason, "instead_of": outcome.value}
        self._post(
            post.key,
            now,
            _Sent(
                extractor,
                decided,
                retraction_notice(reason),
                post.revision,
                read,
                changes,
                answers,
                DryRunOutcome.RETRACTED,
            ),
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
        earlier = self._llm_read.get(key, {}).get(sent)
        if earlier is not None:
            return earlier, ""
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
        self._llm_read.setdefault(key, {})[sent] = read
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
        # From the moment the thread may show this reply, the plan of the reply above it no longer stands: none is
        # current until the outcome is recorded, even while the call is still running or if recording it fails.
        self.store.stale_plan(self.owner, key)
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
            answers=sent.answers,
            question=sent.question,
            plan=sent.plan,
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
        return [
            MovementView(
                movement=movement,
                items=[_part(item.item_text, catalog, poster) for item in movement.line_items],
                source=_location(movement.source_text, catalog, poster),
                destination=_location(movement.destination_text, catalog, poster),
                reasons=self._reasons(movement, extraction, messages, late),
            )
            for movement in extraction.movements
        ]

    def _read_stock(self, views: Sequence[MovementView]) -> Mapping[tuple[int, int], Sequence[StockItem]]:
        """The stock items of every (part, location) pair a settled line acts on (`_stock_lines`)."""
        pairs = sorted({(part, place) for view in views for _, _, part, place, _ in _stock_lines(view)})
        if self.stock is None or not pairs:
            return {}
        found = self.stock(pairs)
        self.beat()  # a stock read can take a while
        return found

    def _reasons(
        self, movement: CandidateMovement, extraction: Extraction, messages: Sequence[MessageInput], late: bool
    ) -> tuple[ReviewReason, ...]:
        text = "\n".join(message.text for message in messages if not message.deleted)
        lateness = (ReviewReason(ReasonCode.LATE_INGEST, ReasonSource.RULE),) if late else ()
        # No extractor sees a photo's pixels (design section 13): the photo may show other items, another
        # quantity or serials, so a person checks the reading against it.
        unread = (ReviewReason(ReasonCode.PHOTO_NOT_READ, ReasonSource.RULE),) if photo_not_read(messages) else ()
        return (
            lateness
            + unread
            + validate(movement, text, max_quantity=self.settings.policy.max_quantity, post_flags=extraction.post_flags)
        )

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
                key,
                DryRunOutcome.GAVE_UP,
                sent.extractor,
                sent.decided,
                sent.revision,
                sent.read,
                sent.changes,
                sent.answers,
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
        answers: int | None = None,
    ) -> None:
        """The pure pipeline raised on this post: it gets no reply, so it cannot stop every later post."""
        _log_failure("post", post.key.ts, error)
        result = {"error": _code(error)}
        self._conclude(post, now, DryRunOutcome.FAILED, extractor, result, read=read, changes=changes, answers=answers)

    def _finish(
        self,
        key: PostKey,
        outcome: DryRunOutcome,
        extractor: str | None = None,
        result: Mapping[str, Any] | None = None,
        revision: int = 1,
        read: tuple[str, ...] | None = None,
        changes: int | None = None,
        answers: int | None = None,
        *,
        plan: NewPlan | None = None,
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
            answers=answers,
            plan=plan,
        )

    def _messages(
        self, key: PostKey, changes: int
    ) -> tuple[Sequence[MessageInput], str, bool, Mapping[str, str]] | str:
        """The post's messages as the extractor takes them, each as it is after the noted changes up to
        `changes`, the author of the first one, whether any of them was not delivered by Slack but found in
        the channel history (slack/catchup.py), or edited as the history showed, and the author of each, by
        timestamp. A deleted message is kept, marked deleted, with no text.

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
        return messages, authors[0], caught_up, dict(zip((m.ts for m in messages), authors, strict=True))

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


def _row(history: Sequence[DryRunRecord]) -> tuple[str | None, set[int]]:
    """The reading that stands and the revisions of the newest dry runs in a row that read the post that way; None
    and no revisions when the newest record kept no reading (a notice, a silent or failed one, an expired record)."""
    reading: str | None = None
    row: set[int] = set()
    for found in reversed(history):
        if found.outcome not in (DryRunOutcome.REPLIED, DryRunOutcome.UNCHANGED) or not found.result:
            break
        digest = _digest(found.result)
        if reading is not None and digest != reading:
            break
        reading = digest
        row.add(found.revision)
    return reading, row


def _typed_line(effects: Effects, movement: int, line: int, item: LineItem) -> LineItem:
    """A line item with the quantity and serials typed for it in the thread. A quantity typed for a pack-unit
    question counts pieces: the line's unit is the piece and its pack size is dropped. A line the post named no item
    for takes the typed name as its item text, so "items missing" moves on to the next line without one."""
    key = (movement, line)
    if key in effects.named and not (item.item_text or "").strip():
        item = replace(item, item_text=effects.named[key])
    if key in effects.quantities:
        item = replace(item, quantity=effects.quantities[key])
    if key in effects.pieces:
        item = replace(item, unit=None, pack_size=None)
    if key in effects.serials:
        item = replace(item, serials=effects.serials[key])
    return item


def _part(text: str | None, catalog: Catalog, poster: str) -> Match:
    return match_part(text, catalog, poster) if text else Match(MatchStatus.UNMATCHED)


def _location(text: str | None, catalog: Catalog, poster: str) -> Match | None:
    return match_location(text, catalog, poster) if text else None


def _stock_lines(view: MovementView) -> list[tuple[int, Operation, int, int, Decimal]]:
    """The lines whose stock can be read: (line, operation, part, location, quantity). A line qualifies once its
    part, the place its operation acts at (the source for a transfer or a remove, the destination for an add or a
    count) and its quantity are settled; anything less would pick stock for a guess."""
    movement = view.movement
    operation = operation_for(movement.movement_type)
    if operation is None:
        return []
    place = view.source if operation in (Operation.TRANSFER, Operation.REMOVE) else view.destination
    if place is None or not place.can_authorize_a_write or place.record is None:
        return []
    unsettled = {reason_line(reason, movement) for reason in view.reasons if reason.code in _QUANTITY_UNSETTLED}
    if None in unsettled:
        return []  # a quantity doubt that names no line: none is settled
    found = []
    for line, (item, part) in enumerate(zip(movement.line_items, view.items, strict=True)):
        if line in unsettled or item.quantity is None or not part.can_authorize_a_write or part.record is None:
            continue
        if item.quantity < 0 or (item.quantity == 0 and operation is not Operation.COUNT):
            continue  # the validator raises its own reason
        found.append((line, operation, part.record, place.record, item.quantity))
    return found


def _with_stock(
    views: Sequence[MovementView],
    found: Mapping[tuple[int, int], Sequence[StockItem]],
    chosen: Mapping[tuple[int, int], tuple[int, ...]] | None = None,
) -> list[MovementView]:
    """The views with the stock items each settled line acts on, and the reasons the picks raise, each naming its
    line (`core/resolve.py`). The lines of every movement of the post share the stock, in the post's order. A line
    a person named stock items for (`chosen`, by movement and line) acts on those only. Lines whose stock was not
    read are left as they are."""
    chosen = chosen or {}
    lines = [
        (index, entry)
        for index, view in enumerate(views)
        for entry in _stock_lines(view)
        if (entry[2], entry[3]) in found
    ]
    resolved = resolve_lines(
        [
            LineStock(
                operation,
                quantity,
                found[(part, place)],
                views[index].movement.line_items[line].serials,
                chosen.get((index, line)),
            )
            for index, (line, operation, part, place, quantity) in lines
        ]
    )
    picks: dict[int, list[SourcePick | None]] = {}
    added: dict[int, list[ReviewReason]] = {}
    for (index, (line, *_)), pick in zip(lines, resolved, strict=True):
        picks.setdefault(index, [None] * len(views[index].items))[line] = pick
        added.setdefault(index, []).extend(
            replace(reason, detail=f"line {line + 1}: {reason.detail}") for reason in pick.reasons
        )
    return [
        replace(view, reasons=(*view.reasons, *added[index]), stock=tuple(picks[index])) if index in picks else view
        for index, view in enumerate(views)
    ]


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
                    }
                    for line, (item, part, pick) in enumerate(
                        zip(view.movement.line_items, view.items, view.stock or [None] * len(view.items), strict=True)
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


def _plan(key: PostKey, result: Mapping[str, Any]) -> NewPlan | None:
    """The plan a reply showing the record `result` makes (ADR 0016), or None. Worked out before anything is posted:
    whatever goes wrong here makes no plan, and never keeps a reply that was sent from being recorded."""
    try:
        made = plan_of(json.loads(json.dumps(result)))  # as the store gives the record back
    except Exception as error:
        _log_failure("plan", key.ts, error)
        return None
    return None if made is None else NewPlan(*made)


def _reading(result: Mapping[str, Any]) -> Any:
    """The movements, matches and reasons a record decided. Lateness is left out: it depends on when the post was
    read, not on what the thread says."""
    movements = json.loads(json.dumps(result.get("movements")))
    for movement in movements or ():
        movement["reasons"] = [r for r in movement["reasons"] if r["code"] != ReasonCode.LATE_INGEST.value]
        movement["gate"] = [g for g in movement["gate"] if g[0] != ReasonCode.LATE_INGEST.value]
    return movements


def _digest(result: Mapping[str, Any]) -> str:
    """What a record read, before any answer: its `reading`, or for a record without one (none applied an answer
    then) the digest of its movements."""
    stored = result.get("reading")
    if isinstance(stored, str):
        return stored
    canonical = json.dumps(_reading(result), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _same_reading(standing: Mapping[str, Any], decided: Mapping[str, Any]) -> bool:
    """Whether a revision decided the same movements, matches and reasons as the reply that stands, with the same
    answers. A new answer always posts a reply, even one that changes nothing: it asks the next question."""

    def answered(result: Mapping[str, Any]) -> tuple[Any, ...]:
        return (result.get("answers") or [], result.get("ignored") or [], result.get("rejected"))

    return bool(standing) and _reading(standing) == _reading(decided) and answered(standing) == answered(decided)


def _kept_reading(history: Sequence[DryRunRecord], read: Sequence[str], changes: int) -> Extraction | None:
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


def _retry_after(error: Exception) -> int:
    """The seconds a rate-limited Slack call asks to wait (its Retry-After header), or 0. The worker waits
    between steps and never inside a call, so the instance lock keeps its heartbeat."""
    headers: Any = getattr(getattr(error, "response", None), "headers", None) or {}
    try:
        stated = next(value for name, value in dict(headers).items() if str(name).lower() == "retry-after")
        return min(max(int(stated), 0), RETRY_FOR_SECONDS)
    except (StopIteration, TypeError, ValueError):
        return 0


def _outage(error: Exception) -> bool:
    """A failure of what the worker reads or writes through, not of the request or receipt itself: InvenTree, the
    network, the disk. It is likely to fail every other one too, so they all wait."""
    return isinstance(error, InvenTreeError | TimeoutError) or _transient(error)


def _described(error: Exception) -> str:
    """The class, and for the connector's own errors their message (keys, states and fixed text only)."""
    if isinstance(error, NotRebuilt | ValueError | AssertionError | StateConflict):
        return f"{type(error).__name__}: {error}"
    return type(error).__name__


def _transient(error: Exception) -> bool:
    """A failure that may pass by itself: the database busy or locked, a full disk, an I/O or network error. Any
    other error is a bug or a payload that will fail the same way again."""
    return isinstance(error, sqlite3.OperationalError | OSError)


def _code(error: Exception) -> str:
    """A short outcome code for an error: its class name only, never its message."""
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(error).__name__).lower()
    return "error_" + re.sub(r"[^a-z0-9_]", "", name)[:58]


def _slack_code(error: Exception) -> str:
    """Slack's error code when Slack answered with one (`is_archived`, `channel_not_found`), else `_code`."""
    response: Any = getattr(error, "response", None)
    try:
        code = response.get("error") if response is not None else None
    except (AttributeError, TypeError):
        code = None
    return code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", code) else _code(error)


def _log_failure(what: str, which: object, error: Exception) -> None:
    """The error class and where it was raised. No message and no traceback: either could quote the post."""
    frames = traceback.extract_tb(error.__traceback__)
    where = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else "?"
    log.error("%s %s: failed (%s at %s)", what, which, type(error).__name__, where)
