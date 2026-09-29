# Slack platform research for the Slack -> InvenTree connector

Research date: 2026-09-28. Sources are official Slack docs (docs.slack.dev), the `slackapi` GitHub
repos, PyPI and npm. Anything not confirmed from a primary source is marked **unverified** and
repeated in the final section. All IDs and names below are synthetic (Acme Fiber, Sam's truck).

## Key takeaways for the connector

1. **Default to Socket Mode.** It needs only outbound WebSocket access, so it works behind NAT next
   to InvenTree with no public URL, TLS certificate or reverse proxy. Slack itself recommends HTTP
   for production and bars Socket Mode apps from the Marketplace, neither of which matters for a
   single-workspace self-hosted install.
2. **Supporting both transports is cheap with Bolt.** Listeners are transport-agnostic; only the
   entry point differs (Socket Mode handler vs. HTTP adapter). Ship two manifest variants.
3. **Dedup is mandatory in both modes.** Slack retries unacknowledged events (HTTP: up to 3 retries;
   Socket Mode: envelopes carry `retry_attempt`). Key on `event_id`, and make stock movements
   idempotent on `(channel, ts)`.
4. **Ack first, work later.** Everything has a 3-second deadline (events, interactions, `trigger_id`).
   Parsing, InvenTree calls and photo downloads must happen after the ack.
5. **Filter on `files[]` and `bot_id`, not only on `subtype`.** A photo with caption is a single
   message with `text` plus `files[]`; bot posts from modern apps carry `bot_id` rather than the
   `bot_message` subtype.
6. **Buttons are better than reactions for undo.** A button click gives an authenticated user ID,
   a `trigger_id`, a `response_url` and app-defined `value`; a reaction can be added by anyone who
   can see the message and gives no confirmation surface. Treat reactions as an optional shortcut.
7. **Authorize on the Slack user ID in the payload** (`user.id` in `block_actions`, `user` in
   reaction events) against a connector-side allowlist or `users.info` flags. Never trust display
   names.
8. **The 2025 `conversations.history`/`conversations.replies` rate limit cut does not apply** to an
   internal app a team creates in its own workspace from a manifest. It would apply if the project
   ever offered one shared, commercially distributed non-Marketplace app.
9. **Use Bolt for Python** if the connector is Python (InvenTree's ecosystem): v1.30.0, MIT,
   Python >= 3.7, `AsyncApp`, FastAPI/Starlette adapters, and a well-worn test pattern
   (`BoltRequest` + `SignatureVerifier.generate_signature` + mock Web API server).
10. **Least privilege:** seven bot scopes and four bot events cover the whole feature set (listed
    in section 10). Enforce a channel-ID allowlist in code as the first check in every listener.

---

## 1. Events API (HTTP) vs Socket Mode

### How each works

| | HTTP Events API | Socket Mode |
|---|---|---|
| Transport | Slack POSTs JSON to your public HTTPS Request URL | App opens an outbound WebSocket to Slack |
| Needs | Public HTTPS endpoint, valid TLS cert, signing secret | App-level token `xapp-...` with `connections:write` |
| Setup handshake | `url_verification` challenge | `apps.connections.open` returns a `wss://` URL, then a `hello` message |
| Ack | HTTP 2xx within 3 seconds | Send `{"envelope_id": "..."}` back on the socket |
| Interactivity | Separate Request URL, form-encoded `payload=` | Same socket, envelope `type: "interactive"` |
| Marketplace | Required | Not allowed |

Socket Mode details (from the Socket Mode guide): apps may hold **up to 10** concurrent WebSocket
connections and "each payload may be sent to any of the connections". The `hello` message includes
`approximate_connection_time`; Slack periodically refreshes connections and sends `disconnect`
messages with reasons `warning`, `refresh_requested` or `link_disabled`. Bolt and the SDKs
reconnect automatically.

Socket Mode envelope (synthetic):

```json
{ "envelope_id": "a1b2c3d4-0000-4000-8000-acme00000001", "type": "events_api",
  "accepts_response_payload": false, "retry_attempt": 0, "retry_reason": "",
  "payload": { "type": "event_callback", "event_id": "Ev0ACME0001", "event": { "type": "message" } } }
```

### Reliability and retries (HTTP)

- Respond with HTTP 2xx **within three seconds**.
- Failed deliveries are retried **3 times**: nearly immediately, after 1 minute, after 5 minutes.
- Retries carry `x-slack-retry-num` (1, 2, 3) and `x-slack-retry-reason`, one of `http_timeout`,
  `too_many_redirects`, `connection_failed`, `ssl_error`, `http_error`, `unknown_error`.
- Responding with header `x-slack-no-retry: 1` suppresses further retries for that event.
- If more than **95% of delivery attempts fail within 60 minutes**, Slack temporarily disables
  the app's event subscriptions.
- Delivery cap: **30,000 events per workspace per app per 60 minutes**; beyond that the app
  receives `app_rate_limited` events.
- **App down:** events during an outage are retried on the schedule above and then dropped. There
  is no replay API. Recovery means reading history with `conversations.history` after restart.

### Reliability and retries (Socket Mode)

- The guide says the app "still needs to acknowledge receiving each event so that Slack knows
  whether to retry".
- The Python SDK's `SocketModeRequest` exposes `retry_attempt` and `retry_reason`, and Bolt Python
  1.29.0 propagates them into `BoltRequest` headers, so retries are observable in both modes.
- **Unverified:** the exact Socket Mode retry schedule and ack deadline, and what happens to events
  when no connection is open. The official guide does not state them. Community issue reports
  describe redelivery after roughly 3 seconds with `retry_reason: "timeout"`. Assume the same
  at-least-once semantics as HTTP and assume events during downtime can be lost.

### Distribution

"Apps using Socket Mode are not currently allowed in the public Slack Marketplace", and "using HTTP
is a requirement" for Marketplace submission. For this project each team creates its own app in its
own workspace, so this restriction is irrelevant. Whether an unlisted, OAuth-distributed app may
use Socket Mode is **unverified** (the distribution guide does not address it).

### Recommendation

**Default: Socket Mode.** Slack's own comparison page says Socket Mode is for cases where "you work
behind a firewall and do not have the ability to expose HTTP endpoints", which is exactly the
target deployment. Slack does recommend HTTP for production on the grounds that long-lived
connections are stateful and subject to disconnects; mitigate with idempotent handlers, `event_id`
dedup and an optional catch-up scan on startup.

**One codebase, both modes:** yes. In Bolt the `App`/`AsyncApp` and all listeners are identical;
only startup differs (`AsyncSocketModeHandler(app, app_token).start_async()` vs. mounting
`AsyncSlackRequestHandler` in FastAPI/Starlette). Select with one config value.

Sources:
- https://docs.slack.dev/apis/events-api/
- https://docs.slack.dev/apis/events-api/using-socket-mode/
- https://docs.slack.dev/apis/events-api/comparing-http-socket-mode/
- https://docs.slack.dev/tools/python-slack-sdk/reference/socket_mode/request.html
- https://github.com/slackapi/bolt-python/releases (v1.29.0 notes)
- https://docs.slack.dev/distribution/

---

## 2. Request signature verification

### Algorithm

1. Read `X-Slack-Request-Timestamp`. Reject if it differs from local time by more than
   **5 minutes** (replay protection).
2. Take the **raw request body** bytes, before any JSON or form parsing.
3. Build the base string `v0:{timestamp}:{raw_body}`.
4. Compute HMAC-SHA256 of the base string with the **signing secret** as key; hex digest.
5. Prefix with `v0=` and compare to `X-Slack-Signature` using a constant-time comparison.

The Python SDK reference implementation is exactly this: `abs(now - int(timestamp)) > 60 * 5`
rejects, the signature is `f"v0={hmac.new(secret, f'v0:{timestamp}:{body}', sha256).hexdigest()}"`,
and the comparison uses `hmac.compare_digest`.

### Pitfalls

- **Raw body.** Frameworks that parse then re-serialize JSON change bytes and break the signature.
  Interactivity payloads are `application/x-www-form-urlencoded`, so the raw body is the encoded
  form string, not the decoded JSON.
- **Constant-time compare** (`hmac.compare_digest`, Node `crypto.timingSafeEqual`/`tsscmp`).
- **Clock skew** on the host causes false rejections; run NTP.
- **Empty or missing signing secret.** Bolt JS 4.7.3 started rejecting an empty `signingSecret` at
  initialization "to prevent HMAC forgery"; the connector should fail fast on the same condition.
- **`ssl_check` requests.** Both Bolt projects hardened handling in 2026 (Bolt JS 4.7.2 requires
  exact `ssl_check=1`; Bolt Python 1.29.0 verifies signatures on `ssl_check` requests). Stay on
  current Bolt versions.
- The old **verification token** in payloads (`token` field) is deprecated; do not rely on it.

### Bolt and Socket Mode

Bolt verifies signatures automatically in its `RequestVerification` middleware when constructed
with `signing_secret`, returning HTTP 401 `{"error": "invalid request"}` on failure.

**Socket Mode does not need signature verification.** There are no inbound HTTP requests; the
connection is authenticated by the app-level token. Bolt Python's middleware explicitly skips
verification when the request mode is `socket_mode`. The signing secret is therefore only required
in HTTP mode.

Sources:
- https://docs.slack.dev/authentication/verifying-requests-from-slack/
- https://github.com/slackapi/python-slack-sdk/blob/main/slack_sdk/signature/__init__.py
- https://github.com/slackapi/bolt-python/blob/main/slack_bolt/middleware/request_verification/request_verification.py
- https://github.com/slackapi/bolt-js/releases (v4.7.2, v4.7.3)

---

## 3. Message events

### Envelope and event shape

Subscribe to `message.channels` (public) and `message.groups` (private). A bot token can read
"any conversation the relevant bot is a member of", so the bot must be invited to the channel
(stated for `conversations.history`; assumed to hold for event delivery, **unverified**).

```json
{
  "type": "event_callback", "team_id": "T0ACME001", "api_app_id": "A0ACME001",
  "event_id": "Ev0ACME0001", "event_time": 1790000000,
  "event": {
    "type": "message", "channel": "C0ACME001", "channel_type": "channel",
    "user": "U0SAM0001", "text": "2 L7 Lite on my truck",
    "ts": "1790000000.000100", "event_ts": "1790000000.000100",
    "client_msg_id": "0f0e0d0c-0000-4000-8000-acme00000002"
  }
}
```

- **Message reference:** `(channel, ts)` identifies a message. A message is in a thread if it has
  `thread_ts`; if `thread_ts == ts` it is the parent, otherwise a reply. To reply in a thread,
  post with `thread_ts = event.thread_ts or event.ts`.
- **Dedup:** `event_id` is "globally unique across all workspaces" and is the right key for
  delivery dedup. `client_msg_id` appears in Slack's own examples on user-authored messages but has
  no documented semantics; treat it as a secondary hint only (**unverified** as a dedup guarantee).

### Subtypes

| Subtype | Recommended handling |
|---|---|
| (none) | Process: ordinary user message |
| `file_share` | Process: user message with `files[]` and optional `text` |
| `thread_broadcast` | Process as a thread reply (has `thread_ts`); avoid double handling |
| `bot_message` | Ignore |
| `message_changed` | `hidden: true`; new content is in `event.message`. Ignore, or re-evaluate only if no stock movement was committed for `message.ts` |
| `message_deleted` | `hidden: true`; `deleted_ts` names the message. Do not auto-undo stock; optionally flag |
| `channel_join`, `channel_leave`, `channel_topic`, `pinned_item`, others | Ignore |

Safest rule: allowlist `subtype in (None, "file_share", "thread_broadcast")` and drop the rest.

### Identifying bot posts

The `bot_message` subtype belongs to classic apps. For modern apps Slack says to detect bot
messages "by the presence of the `bot_id` and `bot_profile` fields in the event payload". Bolt's
self-event middleware drops events where `user == bot_user_id` or `bot_id == own bot_id` (it keeps
`member_joined_channel`/`member_left_channel`). The connector should additionally drop **every**
message with a `bot_id` so that other bots cannot trigger stock movements. `app_id` on message
events is widely observed but was not found in the primary reference (**unverified**).

### Photos

A photo with a caption arrives as **one** message event: `subtype: "file_share"`, caption in
`text`, photos in `files[]`.

```json
{
  "type": "message", "subtype": "file_share", "channel": "C0ACME001", "user": "U0SAM0001",
  "text": "2 L7 Lite on my truck", "ts": "1790000100.000200", "upload": true,
  "files": [{
    "id": "F0ACME0001", "name": "truck_shelf.jpg", "mimetype": "image/jpeg", "filetype": "jpg",
    "size": 1834211, "original_w": 4032, "original_h": 3024,
    "url_private": "https://files.slack.com/files-pri/T0ACME001-F0ACME0001/truck_shelf.jpg",
    "url_private_download": "https://files.slack.com/files-pri/T0ACME001-F0ACME0001/download/truck_shelf.jpg"
  }]
}
```

- **Multiple photos in one send** arrive as one message whose `files[]` has several entries
  (Slack: "a `files` array containing one or more leaner file objects").
- **Follow-up messages** (a photo sent after the text, or vice versa) are separate events with
  their own `ts`. They are linked only if the user replied in the thread (`thread_ts`). The
  connector needs its own correlation rule, for example same user and channel within N seconds,
  or explicitly ask in the thread.
- **Doc ambiguity:** the `file_share` reference page carries a "no longer served" banner but also
  says "In the Events API, subscriptions and deliveries will continue", and the 2018 changelog says
  it is only RTM that stopped. Treat presence of `files[]` as the signal and do not depend on the
  subtype string alone.

### Scopes

`channels:history` (for `message.channels`), `groups:history` (for `message.groups`), `files:read`
(download photos), `chat:write` (replies, receipts, updates), `reactions:read` (reaction events),
`reactions:write` (bot adds status reactions), `users:read` (`users.info` for display name and
admin flags).

Sources:
- https://docs.slack.dev/reference/events/message/
- https://docs.slack.dev/reference/events/message/file_share/
- https://docs.slack.dev/reference/events/message/bot_message/
- https://docs.slack.dev/reference/events/message/message_changed/
- https://docs.slack.dev/reference/events/message/message_deleted/
- https://docs.slack.dev/reference/events/message/thread_broadcast/
- https://docs.slack.dev/changelog/2018-05-file-threads-soon-tread/
- https://github.com/slackapi/bolt-python/blob/main/slack_bolt/middleware/ignoring_self_events/ignoring_self_events.py

---

## 4. Files and photos

### Downloading

`GET` the file's `url_private` (or `url_private_download`) with
`Authorization: Bearer xoxb-...`. The token needs `files:read`. Thumbnails (`thumb_360`,
`thumb_480`, up to `thumb_1024`) use the same authentication and are useful when a full-size
image is unnecessary.

Pitfalls:
- `url_private_download` is documented only for "editable-mode files", so fall back to
  `url_private`.
- A missing or wrong `Authorization` header yields an **HTML login page with a success status**,
  not an error. Validate the response `Content-Type` against the file's `mimetype` before use
  (behaviour reported in slackapi GitHub issues; not in the official reference).
- HTTP clients that drop the `Authorization` header on cross-host redirects can trigger the same
  symptom (**unverified**).
- In **Slack Connect** channels the file object may contain only
  `"file_access": "check_file_info"`; call `files.info` to get metadata.

### Metadata

`size` (bytes), `mimetype`, `filetype` (no 1-to-1 mapping with `mimetype`), `original_w`,
`original_h`, `mode` (`hosted`, `external`, `snippet`, `post`), `is_external`. Enforce a
connector-side maximum size and a mimetype allowlist (`image/jpeg`, `image/png`, `image/heic`)
before downloading.

### Privacy

- Files stay private to the workspace unless someone calls `files.sharedPublicURL`; the connector
  should never do that.
- Photos may contain faces, licence plates, customer premises and EXIF GPS data. Prefer processing
  in memory and storing only the Slack file ID and permalink; if photos are attached to InvenTree,
  make that an explicit opt-in setting.
- `files:read` grants access to files in every conversation the bot is in, so keep the bot in
  inventory channels only.

### API changes 2024-2026

`files.upload` is deprecated and "will stop functioning and be officially sunset on
November 12, 2025". Replacement is a three-step flow: `files.getUploadURLExternal`, POST the bytes
to the returned URL, then `files.completeUploadExternal`. This only matters if the connector
uploads files (needs `files:write`); downloading is unchanged. The SDK helpers
`files_upload_v2` / `uploadV2` wrap the new flow.

Sources:
- https://docs.slack.dev/reference/objects/file-object/
- https://docs.slack.dev/messaging/working-with-files/
- https://docs.slack.dev/reference/methods/files.upload/
- https://docs.slack.dev/reference/scopes/files.read/
- https://github.com/slackapi/bolt-js/issues/2585

---

## 5. Interactivity

### Posting a clarifying question in the thread

`chat.postMessage` with `channel`, `thread_ts` and `blocks`. Limits that matter:

| Field | Limit |
|---|---|
| Button `text` | 75 chars (may truncate near 30) |
| Button `action_id` | 255 chars |
| Button `value` | **2000 chars** |
| `block_id` | 255 chars; should be unique per message and changed on update |
| Elements per `actions` block | 25 |
| Message `text` | 4,000 recommended; truncated above 40,000 |

Keep `value` small: store a connector-side pending-movement ID (for example `mv_0001`) rather than
serialized state, and look it up server-side. Never trust quantities or part IDs round-tripped
through `value` without re-validation.

### `block_actions` payload (synthetic, trimmed)

```json
{
  "type": "block_actions",
  "api_app_id": "A0ACME001",
  "team": { "id": "T0ACME001", "domain": "acmefiber" },
  "user": { "id": "U0SAM0001", "username": "sam", "team_id": "T0ACME001" },
  "channel": { "id": "C0ACME001", "name": "inventory" },
  "container": { "type": "message", "message_ts": "1790000200.000300", "channel_id": "C0ACME001" },
  "message": { "ts": "1790000200.000300", "thread_ts": "1790000100.000200" },
  "trigger_id": "1790000300.0001.acme",
  "response_url": "https://hooks.slack.com/actions/T0ACME001/0000/synthetic",
  "actions": [{ "type": "button", "block_id": "confirm_mv_0001", "action_id": "movement_confirm",
                "value": "mv_0001", "action_ts": "1790000300.000400" }]
}
```

### Responding

- **Ack within 3 seconds** (HTTP 200, or envelope ack in Socket Mode). In Bolt, call `ack()` first.
- **`response_url`**: usable up to **5 times within 30 minutes**. Supports `replace_original`,
  `delete_original`, `response_type: "in_channel" | "ephemeral"` (default ephemeral) and
  `thread_ts`.
- **`chat.update`** (`channel`, `ts`): Tier 3, needs `chat:write`, works only on the bot's own
  messages and has no 30-minute limit, so prefer it for receipts that must change state later
  (for example "Undone by Sam"). Passing `text` without `blocks` removes the blocks, which is a
  handy way to remove buttons after a decision.
- **Ephemeral** (`chat.postEphemeral`): "delivery is not guaranteed", messages do not persist
  across sessions, cannot be updated via `chat.update`, and in threads are only shown "if there is
  already an active thread". Use only for low-stakes hints such as "only admins can approve".
- **Modals**: `views.open` needs a `trigger_id`, which "will expire 3 seconds after it's sent" and
  is single-use. `view_submission` must also be answered within 3 seconds.

### Delivery: HTTP vs Socket Mode

HTTP: POST to the Interactivity Request URL, form-encoded with a `payload` field containing JSON;
signature verification applies. Socket Mode: the same JSON arrives in an envelope with
`type: "interactive"`. Bolt normalizes both, so `@app.action("movement_confirm")` is identical.

### Who clicked

`payload.user.id` is the authenticated clicker, set by Slack. For authorization compare it with a
connector-configured approver list, or call `users.info` and read `is_admin`, `is_owner`,
`is_primary_owner`, `is_restricted`, `is_bot`, `deleted`. For audit record `user.id`, `team.id`,
`channel.id`, `container.message_ts`, `action_id`, `value`, `action_ts`. A workspace admin is not
necessarily an inventory approver, so a connector-side list is the better default.

Sources:
- https://docs.slack.dev/reference/interaction-payloads/block_actions-payload/
- https://docs.slack.dev/reference/block-kit/block-elements/button-element/
- https://docs.slack.dev/reference/block-kit/blocks/actions-block/
- https://docs.slack.dev/interactivity/handling-user-interaction/
- https://docs.slack.dev/reference/methods/chat.postMessage/
- https://docs.slack.dev/reference/methods/chat.update/
- https://docs.slack.dev/reference/methods/chat.postEphemeral/
- https://docs.slack.dev/reference/methods/views.open/
- https://docs.slack.dev/surfaces/modals/
- https://docs.slack.dev/reference/methods/users.info/

---

## 6. Reactions

```json
{ "type": "reaction_added", "user": "U0SAM0001", "reaction": "leftwards_arrow_with_hook",
  "item_user": "U0BOT0001", "event_ts": "1790000400.000500",
  "item": { "type": "message", "channel": "C0ACME001", "ts": "1790000200.000300" } }
```

`reaction_removed` has the same shape. Both require `reactions:read`. `user` is the reactor;
`item_user` is the author of the reacted item and is omitted for some non-user messages such as
incoming webhooks. `item.type` may be `message`, `file` or `file_comment`.

### Is reaction-as-undo reliable?

Partly. The event identifies the reactor and the exact message, which is enough to map to a
movement. Weaknesses:

| Concern | Reaction | Button |
|---|---|---|
| Who can trigger | Anyone who can see the message | Anyone who can see it, but the app can reject and explain |
| Accidental trigger | Easy (mis-tap, emoji picker) | Harder; can add a `confirm` dialog |
| Feedback to user | None unless the app posts | `response_url`, `trigger_id` for modal |
| Target | Any message; must check `item.ts` is one of the bot's receipts | Attached to the receipt by construction |
| Emoji aliases and skin tones | Must normalize names (**unverified** exact naming rules) | Not applicable |
| Undo of undo | `reaction_removed` semantics are ambiguous | Explicit second button |
| Payload | No free-form data | `value` up to 2000 chars |

Recommendation: button is the primary undo. If the reaction shortcut is offered, accept only one
configured emoji, only on receipt messages the connector posted (look up `item.channel` +
`item.ts`), apply the same authorization as the button, make undo idempotent, and ignore
`reaction_removed`.

Sources:
- https://docs.slack.dev/reference/events/reaction_added/
- https://docs.slack.dev/reference/events/reaction_removed/
- https://docs.slack.dev/reference/methods/reactions.add/

---

## 7. Bolt frameworks

| | Bolt for Python (`slack_bolt`) | Bolt for JavaScript (`@slack/bolt`) |
|---|---|---|
| Latest version | 1.30.0 (2026-07-15) | 5.1.0 (2026-09-02) |
| Runtime | Python >= 3.7; classifiers 3.7 to 3.14 | Node >= 20 (v5.0.0 dropped Node 18) |
| License | MIT | MIT |
| Core dependency | `slack_sdk >=3.38.0,<4` (latest 3.44.1) | `@slack/web-api ^8`, `@slack/socket-mode ^3`, `express ^5` |
| Async | `AsyncApp` (requires `aiohttp`) alongside sync `App` | Async-only (Promises) |
| HTTP adapters | Flask, Django, Starlette, FastAPI, aiohttp, Sanic and others | `HTTPReceiver`, `ExpressReceiver`, `AwsLambdaReceiver`, custom receivers |
| Socket Mode | `SocketModeHandler`, `AsyncSocketModeHandler`; backends: built-in, `websocket_client`, `aiohttp`, `websockets` | `socketMode: true` + `appToken`, or `SocketModeReceiver` |
| Token rotation | Built in since 1.7 | Built in since 3.5 |

Maintenance: both are maintained by Slack with steady releases. Bolt Python shipped 1.23 to 1.30
between 2025-03 and 2026-07; Bolt JS shipped 4.5 to 5.1 between 2025-10 and 2026-09. Recent work
in both concentrates on AI/agent features (`say_stream`, `set_status`) and security hardening,
which suggests rough feature parity for events, actions, views and Socket Mode. A formal parity
statement from Slack was not found (**unverified**).

Notable breaking change: Bolt JS 5.0.0 replaced axios with native fetch and removed the `agent`
and `clientTls` options. Bolt JS 5.1.0 added a `bodyLimit` (default 4 MB, HTTP 413).

### Testing with recorded fixtures (Bolt Python's own pattern)

Bolt Python's test suite (`tests/scenario_tests/`) is the de facto guide; no official testing
guide was found (**unverified** that none exists).

1. Build the app with a `WebClient` pointing at a local mock:
   `WebClient(token=..., base_url="http://localhost:8888")`, started by
   `setup_mock_web_api_server(self)` from `tests/mock_web_api_server`.
2. Load a recorded payload (JSON fixture), serialize it to the raw body. For interactivity the body
   is `payload=<urlencoded JSON>` with `content-type: application/x-www-form-urlencoded`.
3. Sign it: `SignatureVerifier(signing_secret).generate_signature(timestamp=ts, body=raw_body)`
   and set `x-slack-signature` and `x-slack-request-timestamp` headers (header values are lists).
4. `response = app.dispatch(BoltRequest(body=raw_body, headers=headers))`, then assert
   `response.status == 200`.
5. Assert outbound calls with `assert_received_request_count(self, "/chat.postMessage", 1)`;
   async variants exist.

Socket Mode fixtures can be tested the same way by constructing `BoltRequest(..., mode="socket_mode")`,
which skips signature verification. The mock server module lives in the repo's test tree and is
not published as a package, so the connector must vendor an equivalent or use a generic HTTP mock.
Sanitize recorded fixtures (tokens, `response_url`, real user and file IDs) before committing.

Sources:
- https://pypi.org/project/slack-bolt/ and https://pypi.org/pypi/slack-bolt/json
- https://pypi.org/project/slack-sdk/
- https://registry.npmjs.org/@slack/bolt/latest
- https://github.com/slackapi/bolt-python/releases
- https://github.com/slackapi/bolt-js/releases
- https://docs.slack.dev/tools/bolt-python/concepts/async/
- https://docs.slack.dev/tools/bolt-python/concepts/socket-mode/
- https://docs.slack.dev/tools/bolt-python/concepts/adapters/
- https://docs.slack.dev/tools/bolt-python/concepts/acknowledge/
- https://docs.slack.dev/tools/bolt-js/concepts/socket-mode/
- https://docs.slack.dev/tools/bolt-js/concepts/receiver/
- https://github.com/slackapi/bolt-python/tree/main/tests
- https://github.com/slackapi/bolt-python/blob/main/tests/scenario_tests/test_block_actions.py

---

## 8. App manifests

Manifests are YAML or JSON. They can declare display name and description
(`display_information.name` max 35 chars), bot user (`features.bot_user.display_name` max 80),
bot scopes (`oauth_config.scopes.bot`), event subscriptions
(`settings.event_subscriptions.bot_events` and optional `request_url`), interactivity
(`settings.interactivity.is_enabled`, optional `request_url`), `settings.socket_mode_enabled`,
`settings.token_rotation_enabled`, `settings.org_deploy_enabled` and
`settings.allowed_ip_address_ranges`.

**Not in the manifest:** tokens and secrets. The admin must still install the app to get the
`xoxb-` token, generate the `xapp-` token (Socket Mode) or copy the signing secret (HTTP), and
upload an icon.

### Admin flow (about two minutes)

1. Open https://api.slack.com/apps, choose Create New App, then "From a manifest".
2. Pick the workspace, paste the YAML, review the summary, Create.
3. Install to workspace and copy the Bot User OAuth Token.
4. Socket Mode: Basic Information, App-Level Tokens, generate a token with `connections:write`.
5. Invite the bot to the inventory channel (`/invite @Acme Inventory`) and copy the channel ID.

### Proposed Socket Mode manifest

```yaml
display_information:
  name: Acme Inventory
  description: Turns inventory messages into InvenTree stock movements
features:
  bot_user:
    display_name: Acme Inventory
    always_online: true
oauth_config:
  scopes:
    bot:
      - channels:history
      - groups:history
      - chat:write
      - files:read
      - reactions:read
      - reactions:write
      - users:read
settings:
  event_subscriptions:
    bot_events:
      - message.channels
      - message.groups
      - reaction_added
      - reaction_removed
  interactivity:
    is_enabled: true
  socket_mode_enabled: true
  org_deploy_enabled: false
  token_rotation_enabled: false
```

### One manifest or two?

**Two variants.** Slack's manifest validation states that event subscriptions require "either
Request URL or Socket Mode Enabled" and interactivity requires a Request URL or Socket Mode. The
HTTP variant sets `socket_mode_enabled: false` and adds
`settings.event_subscriptions.request_url` and `settings.interactivity.request_url` with the
deployer's hostname, and the Request URL must be verified, so the service has to be running
first. Scopes and events are identical in both. Whether a single manifest with Socket Mode enabled
and request URLs also present validates is **unverified**. Suggested files:
`manifest.socket-mode.yaml` (default) and `manifest.http.yaml` (template with a placeholder host).

Sources:
- https://docs.slack.dev/app-manifests/configuring-apps-with-app-manifests/
- https://docs.slack.dev/reference/app-manifest/

---

## 9. Rate limits and platform changes

### Web API tiers

| Method | Tier | Practical limit |
|---|---|---|
| `chat.postMessage` | Special | About 1 message per second per channel, plus a workspace-wide limit |
| `chat.update` | Tier 3 | 50+ per minute |
| `chat.postEphemeral` | Tier 4 | 100+ per minute |
| `conversations.history` | Tier 3 (internal and Marketplace apps) | 50+ per minute |
| `conversations.replies` | Tier 3 (internal and Marketplace apps) | 50+ per minute |
| `reactions.add` | Tier 3 | 50+ per minute |
| `users.info` | Tier 4 | 100+ per minute |
| `views.open` | Tier 4 | 100+ per minute |

Exceeding a limit returns HTTP 429 with a `Retry-After` header (seconds). The Slack SDKs ship
retry handlers; enable them. Cache `users.info` results.

### 2025 change to `conversations.history` and `conversations.replies`

Effective **2025-05-29**, for new apps and new installations "commercially distributed outside of
the Marketplace", both methods are limited to **1 request per minute** and `limit` is capped at
**15**. Slack's clarification of 2025-06-03: "Any internal customer-built apps will maintain their
existing rate limits".

**Impact on this project:** none in the intended model, where each team creates its own app from
the manifest in its own workspace. That is an internal customer-built app and keeps Tier 3.
Caveats:
- Slack does not give a formal definition of "internal customer-built" (**unverified** boundary).
- If the project ever offered a single shared app that many workspaces install through OAuth
  without Marketplace listing, the 1-per-minute limit would apply. Avoid that design.
- The connector is event-driven and should rarely need history calls; reserve them for catch-up
  after downtime and thread context.

The same announcement updated the API Terms of Service around how apps "store, use, and share data
accessed via Slack APIs". Anyone sending Slack message content or photos to a third-party model
should read the current terms; this document does not interpret them.

### Other changes relevant to a self-hosted custom app

- **Legacy custom bots** stopped functioning on 2025-03-31. New classic apps cannot be created.
- **Classic apps:** a 2026-11-16 end date was announced, then Slack stated the deprecation "has
  been paused"; classic apps keep working for now. Not relevant to a new granular-permission app.
- **Granular permissions** are the only model for new apps; the manifest above uses them.
- **`files.upload`** sunset 2025-11-12 (section 4).
- **RTM API** is unavailable to modern Slack apps; use Events API or Socket Mode.
- **2026-06-17:** system notifications move from Slackbot to a "Slack" system user (`USLACK`).
  Filter defensively on unexpected system user IDs.

Sources:
- https://docs.slack.dev/apis/web-api/rate-limits/
- https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps/
- https://docs.slack.dev/changelog/2025/06/03/rate-limits-clarity/
- https://docs.slack.dev/reference/methods/conversations.history/
- https://docs.slack.dev/reference/methods/conversations.replies/
- https://docs.slack.dev/changelog/2024-09-legacy-custom-bots-classic-apps-deprecation/
- https://docs.slack.dev/legacy/legacy-app-migration/migrating-classic-apps/
- https://docs.slack.dev/changelog/tags/breaking-change/

---

## 10. Token handling and security

| Secret | Prefix | Used for | Needed in |
|---|---|---|---|
| Bot token | `xoxb-` | All Web API calls and file downloads | Both modes |
| App-level token | `xapp-` | `apps.connections.open` (scope `connections:write`) | Socket Mode only |
| Signing secret | none | Verifying inbound HTTP requests | HTTP mode only |

- Load from environment variables or a secrets file outside the repo; keep `.env` in
  `.gitignore`; never log tokens. Revoke with `auth.revoke` if leaked.
- **Token rotation:** optional. Access tokens then expire every 12 hours and are refreshed with
  single-use `xoxe-` refresh tokens through the OAuth flow. It "may not be turned off once it's
  turned on" and does not apply to app-level tokens. For a single-workspace Socket Mode install
  with a static bot token it adds an OAuth dependency for little gain; recommend leaving
  `token_rotation_enabled: false` and documenting manual rotation (reinstall to reissue).
- **IP allowlisting:** Slack can restrict Web API token use to listed IP ranges
  (`settings.allowed_ip_address_ranges`). Useful for teams with a static egress IP.
- **Standard bot tokens and expiry:** the tokens page does not state that non-rotating bot tokens
  expire; long-lived behaviour is the common understanding (**unverified** from that page).

### Proposed minimal scope list (bot)

| Scope | Why |
|---|---|
| `channels:history` | Receive `message.channels` in public channels the bot is in |
| `groups:history` | Same for private channels; drop if only public channels are used |
| `chat:write` | Thread replies, receipts, `chat.update`, ephemeral messages |
| `files:read` | Download photos |
| `reactions:read` | `reaction_added` / `reaction_removed`; drop if undo is button-only |
| `reactions:write` | Bot adds status reactions; optional |
| `users:read` | `users.info` for names and role flags; optional if using a static ID allowlist |

Deliberately excluded: `chat:write.public`, `im:history`, `mpim:history`, `channels:read`,
`users:read.email`, `files:write`, `commands`, and all user scopes.

### Proposed event subscription list (bot events)

`message.channels`, `message.groups`, `reaction_added`, `reaction_removed`.

### Channel allowlisting

Slack has no per-channel scope for bots; a bot receives messages from every channel it is a member
of. Therefore:
1. Configure allowed channels by **ID** (`C0ACME001`), not by name; names can change.
2. Make the allowlist check the first step of every listener (messages, actions, reactions) and
   drop silently, with a debug log line, anything else. An empty allowlist should mean "act
   nowhere".
3. Also verify `team_id` and `api_app_id` against configuration.
4. The act of inviting the bot to a channel is the second, Slack-side control; document that the
   bot should be invited only to inventory channels.

Sources:
- https://docs.slack.dev/authentication/tokens/
- https://docs.slack.dev/authentication/using-token-rotation/
- https://docs.slack.dev/authentication/best-practices-for-security/
- https://docs.slack.dev/reference/app-manifest/

---

## 11. Dev and test workspaces

### Slack Developer Program sandboxes

- Join at https://api.slack.com/developer-program; membership is free.
- Provisioning a sandbox requires being "on a paid plan already" or providing "a valid payment
  method", used "for identity verification purposes only; you will not be charged". Members of an
  Enterprise org may need Org Admin approval.
- Sandboxes are **Enterprise org** environments.
- Limits: up to 2 active sandboxes; up to 10 provisioned per 30 days; active for 6 months by
  default and extendable; max 8 users and 2 guests per sandbox; after expiry data is kept for a
  3-month grace period and then deleted; Slack Connect only between sandboxes.

### Plain free workspace

Anyone can create a free Slack workspace and install a custom app from the manifest with no
payment method. Constraints on the free plan: at most **10** third-party or custom app
installations and **90 days** of visible message history. For this connector that is sufficient,
and it matches what many small teams actually run. A sandbox is mainly useful for testing
Enterprise-org behaviour.

Recommendation: contributors use a personal free workspace; maintainers keep one sandbox for
Enterprise checks. CI should use recorded fixtures (section 7) rather than a live workspace.

Sources:
- https://docs.slack.dev/tools/developer-sandboxes/
- https://slack.com/help/articles/27204752526611-Feature-limitations-on-the-free-version-of-Slack
- https://slack.com/help/articles/115002422943-Usage-limits-for-free-workspaces

---

## Open questions / unverified

1. **Socket Mode retry schedule, ack deadline and offline behaviour.** Official docs confirm only
   that acks determine retries and that envelopes carry `retry_attempt`/`retry_reason`. Timing and
   whether events are queued while no connection is open are not documented. Needs an empirical
   test: stop the connector, post messages, restart, observe.
2. **Socket Mode with unlisted OAuth distribution.** Docs only exclude the Marketplace.
3. **`client_msg_id` semantics.** Present in Slack's examples but undocumented; do not rely on it.
4. **`app_id` on message events.** Commonly observed, not found in the primary reference. `bot_id`
   and `bot_profile` are documented.
5. **`file_share` reference page wording** is self-contradictory ("no longer served" vs. Events API
   deliveries continue). Verify with a live capture, including whether multi-photo sends from the
   mobile clients always produce a single message.
6. **`url_private` failure mode** (HTML login page with success status) comes from GitHub issues,
   not official docs. Redirect and header-stripping behaviour is unverified.
7. **Manifest containing both `socket_mode_enabled: true` and request URLs.** Validation behaviour
   not confirmed; plan for two manifest files.
8. **Definition of "internal customer-built app"** for the 2025 rate limits is not formally given.
   The project's per-team manifest install fits the plain reading.
9. **Bolt feature parity** between Python and JavaScript is inferred from release notes.
10. **No official Bolt testing guide** was found; the fixture pattern is taken from Bolt Python's
    own test suite, whose mock server is not a published package.
11. **Emoji name normalization** in reaction events (aliases, skin-tone suffixes) was not checked.
12. **Classic apps end date** is in flux (announced 2026-11-16, then paused). Irrelevant to new apps.
13. **`private_metadata` and `callback_id` length limits** for modals were not confirmed.
14. **Free plan limits** come from Slack help centre search results; the article pages were not
    fetched directly.
15. **Bot channel membership as a precondition for message event delivery** is documented for
    `conversations.history` access, not explicitly for Events API delivery.
