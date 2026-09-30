"""The Slack listener: check, store, then acknowledge (design section 4, "Acknowledge first", "Durable queue").

Bolt for Python receives payloads over Socket Mode or HTTP. Over HTTP, Bolt checks the request signature before
anything here runs; `build_app` turns that check on for HTTP and has no switch for it, and the settings refuse
HTTP mode without a signing secret. The listener runs the pure checks of `ingest`, stores an accepted payload
in the durable queue, and only then returns, so Bolt acknowledges only what is stored. If storing fails, the
handler raises, Bolt answers with an error, and Slack delivers the event again.

A dropped payload is acknowledged and not stored, so messages from channels outside the allowlist never reach
the database. Only the drop reason is logged, never message text. Processing happens later, from the queue.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from slack_bolt import Ack, App, BoltRequest, BoltResponse
from slack_bolt.authorization import AuthorizeResult
from slack_sdk import WebClient

from stockthread.config import SlackSettings
from stockthread.slack.ingest import Click, Decision, Drop, DropReason, ingest_action, ingest_event
from stockthread.store import EventKind, Store

log = logging.getLogger(__name__)

_ACTION_TS = re.compile(r"\d{1,10}\.\d{1,6}")


@dataclass(frozen=True)
class BotIdentity:
    """Who the connector is in Slack, from `auth.test` at startup."""

    team_id: str
    bot_id: str
    bot_user_id: str


class IdentityError(Exception):
    """The bot token does not belong to the configured workspace."""


class _AuthTest(Protocol):
    def auth_test(self) -> Any: ...


def identify(client: _AuthTest, settings: SlackSettings) -> BotIdentity:
    """Call `auth.test` once at startup: the token works, and it belongs to the configured workspace."""
    result = client.auth_test()
    team_id, bot_id, user_id = result.get("team_id"), result.get("bot_id"), result.get("user_id")
    if not (isinstance(team_id, str) and isinstance(bot_id, str) and isinstance(user_id, str)):
        raise IdentityError("auth.test did not name a bot: use a bot token (xoxb-)")
    if team_id != settings.workspace_id:
        raise IdentityError("the bot token belongs to another workspace than slack.workspace_id")
    return BotIdentity(team_id, bot_id, user_id)


class ThreadStores:
    """One store connection per thread. Bolt runs handlers on worker threads, and a SQLite connection stays in
    the thread that opened it. WAL mode lets the connections of one process write in turn."""

    def __init__(self, path: str | Path) -> None:
        self._path = path
        self._local = threading.local()

    def __call__(self) -> Store:
        store: Store | None = getattr(self._local, "store", None)
        if store is None:
            store = Store.open(self._path)
            self._local.store = store
        return store


class Enqueue:
    """`ingest.SeenEvents` backed by the durable queue: an accepted event is stored under its event id."""

    def __init__(self, store: Store, body: str) -> None:
        self._store = store
        self._body = body

    def first_time(self, event_id: str) -> bool:
        return self._store.enqueue_event(event_id, "event", self._body)


class Listener:
    """What the Bolt handlers call. Each method returns the ingest decision, for logs and tests."""

    def __init__(self, settings: SlackSettings, stores: Callable[[], Store]) -> None:
        self._settings = settings
        self._stores = stores

    def on_event(self, body: Mapping[str, Any]) -> Decision:
        decision = ingest_event(body, self._settings, Enqueue(self._stores(), dump(body)))
        _log(decision, "event")
        return decision

    def on_action(self, body: Mapping[str, Any]) -> Decision:
        decision = ingest_action(body, self._settings)
        if isinstance(decision, Click):
            key = _click_key(decision, body)
            if key is None:
                decision = Drop(DropReason.MALFORMED)
            elif not self._stores().enqueue_event(key, "interactive", dump(body)):
                decision = Drop(DropReason.DUPLICATE_EVENT)
        _log(decision, "interactive")
        return decision


def build_app(
    settings: SlackSettings,
    identity: BotIdentity,
    listener: Listener,
    *,
    bot_token: str,
    signing_secret: str | None,
    client: WebClient | None = None,
) -> App:
    """The Bolt app for either transport. Over HTTP, the signature check is always on."""
    http = settings.transport == "http"
    if http and not signing_secret:
        raise ValueError("HTTP mode needs a signing secret")

    def authorize(**_: Any) -> AuthorizeResult:
        return AuthorizeResult(
            enterprise_id=None,
            team_id=identity.team_id,
            bot_token=bot_token,
            bot_id=identity.bot_id,
            bot_user_id=identity.bot_user_id,
        )

    app = App(
        client=client or WebClient(),  # `authorize` gives each request the bot token
        authorize=authorize,
        signing_secret=signing_secret if http else None,
        request_verification_enabled=http,
        process_before_response=True,  # acknowledge only after the handler has stored the payload
        token_verification_enabled=False,  # `identify` has called auth.test already
        logger=log,
    )

    @app.middleware
    def _only_own_transport(req: BoltRequest, resp: BoltResponse, next: Callable[[], BoltResponse]) -> BoltResponse:
        # Bolt checks signatures on every request not marked as Socket Mode. An app built for Socket Mode has
        # no signing secret, so it must never answer a request that came any other way.
        if (req.mode == "socket_mode") != (not http):
            return BoltResponse(status=401, body="")
        return next()

    @app.event("message")
    def _message(body: dict[str, Any]) -> None:
        listener.on_event(body)

    @app.event(re.compile(".*"))
    def _other_event() -> None:
        # Acknowledged and not stored: an unhandled event would get a 404, and Slack would retry it.
        _log(Drop(DropReason.NOT_A_MESSAGE), "event")

    @app.action(re.compile(".*"))
    def _click(ack: Ack, body: dict[str, Any]) -> None:
        listener.on_action(body)
        ack()

    return app


def _click_key(click: Click, body: Mapping[str, Any]) -> str | None:
    """A queue key for a click. Slack gives clicks no event id, but each click has its own `action_ts`."""
    actions = body.get("actions")
    action_ts = actions[0].get("action_ts") if isinstance(actions, list) and isinstance(actions[0], Mapping) else None
    if not isinstance(action_ts, str) or not _ACTION_TS.fullmatch(action_ts):
        return None
    return f"click:{click.channel}:{click.message_ts}:{click.user}:{action_ts}"


def dump(body: Mapping[str, Any]) -> str:
    return json.dumps(body, separators=(",", ":"), sort_keys=True)


def _log(decision: Decision, kind: EventKind) -> None:
    if isinstance(decision, Drop):
        log.info("slack %s dropped: %s", kind, decision.reason.value)
    else:
        log.debug("slack %s accepted: %s", kind, type(decision).__name__)
