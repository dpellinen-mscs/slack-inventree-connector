# Design

Status: working design, 2026-09-29. Shape, stack and default policy are
decided ([0003](decisions/0003-standalone-python-service.md),
[0004](decisions/0004-default-write-policy.md)). Sections marked
**(decision pending)** still wait for an answer from Dan.

InvenTree and Slack behavior described here comes from reading
documentation and source code. It becomes a fact for this project only when
a test proves it (section 11).

Background research, with sources, is in [`research/`](research/):
[InvenTree](research/inventree.md), [Slack](research/slack.md),
[prior art and stack](research/prior-art-and-stack.md).

## 1. Goal

Turn informal Slack posts about stock ("2 Disc 40 on my truck") into correct,
auditable InvenTree stock movements, with a human in the loop exactly where a
human is needed.

The ranking of outcomes drives every tradeoff below:

1. A correct movement, applied once.
2. No movement, and a clear question in the thread.
3. No movement, silently. This is drift, the problem we set out to fix.
4. A wrong movement. **This is worse than all of the above.**

## 2. Pipeline

```text
Slack event
   │
   ▼
┌─────────┐   ┌─────────┐   ┌──────────┐   ┌───────┐   ┌─────────┐
│ Ingest  │──▶│ Extract │──▶│ Validate │──▶│ Match │──▶│ Resolve │
└─────────┘   └─────────┘   └──────────┘   └───────┘   └─────────┘
 verify        LLM or        contradiction   parts and    concrete
 allowlist     rules,        checks, text    locations,   stock items
 dedup         strict        review          no model     and serials
 group         schema        reasons
                                                              │
   ┌──────────────────────────────────────────────────────────┘
   ▼
┌────────┐   ┌───────┐   ┌─────────┐   ┌────────┐   ┌───────┐
│ Review │──▶│ Plan  │──▶│  Gate   │──▶│ Apply  │──▶│ Learn │
└────────┘   └───────┘   └─────────┘   └────────┘   └───────┘
 question     exact       switch AND    idempotent   confirmed
 in thread    API         policy AND    write,       corrections
              request,    validation    read-back,   become
              dry-run                   receipt,     aliases
                                        undo
```

Every stage is a function from typed input to typed output. Four stages
touch the outside world: Ingest and Review talk to Slack, Resolve reads
InvenTree, and Apply writes to InvenTree. Extract, Validate, Match, Plan
and Gate are the pure core and are tested without network access. Match
works on a catalog snapshot that Resolve refreshes. Every check that needs
live stock data belongs to Resolve.

| Stage | Input | Output | Can fail to |
| --- | --- | --- | --- |
| Ingest | Slack event | Post (grouped messages) | Ignored, with a reason |
| Extract | Post | Candidate movements | `unclear` candidate |
| Validate | Candidates | Candidates with review reasons | Review |
| Match | Item and location text | matched, ambiguous or unmatched | Review |
| Resolve | Matched candidate | Concrete stock items | Review |
| Review | Review reasons | One question in the thread | Waiting |
| Plan | Resolved candidate | Exact API request | |
| Gate | Plan, policy, switch | Allowed or a dry-run | Dry-run |
| Apply | Allowed plan | Verified movement | Failed, reported |
| Learn | Confirmed correction | Alias | |

## 3. Architecture options

### Option A: standalone service

A separate process next to InvenTree. It talks to Slack and to the InvenTree
REST API, and keeps its own small database.

- Good: independent of the InvenTree server process and version. A crash or
  a slow LLM call cannot hurt the inventory server. Socket Mode gives a
  deployment with no public endpoint. It uses the same permission checks and
  stock history as any other API user.
- Bad: one more container to run and configure. It is not visible in the
  InvenTree plugin list.

### Option B: InvenTree plugin

A plugin that runs inside the InvenTree server.

- Good: one install, settings in the InvenTree UI, direct access to models.
- Bad: tied to server internals, which change between releases. A long-lived
  Slack connection and slow LLM calls do not fit a web worker. Receiving
  Slack events over HTTP would mean exposing InvenTree to the internet.
  Plugin code runs with full database access, so a bug has a larger blast
  radius, and direct model access can bypass the API's checks. Integration
  tests are harder to write.

### Option C: core library with thin adapters

A pure core (Extract, Validate, Match, Plan and Gate) with adapters for
Slack, InvenTree and storage. Shipped as one service.

- Good: the core is tested without network access. Another chat platform, a
  CLI or a small plugin can reuse it later. This matches the shape of the
  pipeline.
- Bad: abstraction can grow beyond what one service needs. The answer is to
  keep it to one package, with adapter interfaces only where a second
  implementation exists or is planned.

### Decision

Option C, deployed as in option A: one Python package, one container, a pure
core and three adapters ([0003](decisions/0003-standalone-python-service.md)). A thin InvenTree plugin can come after v1.0 if
users ask for settings inside the InvenTree UI.

### Language

| | Python | TypeScript |
| --- | --- | --- |
| Slack SDK | Bolt for Python, maintained, async | Bolt for JS, maintained |
| InvenTree fit | Same language as InvenTree and its client | None |
| Contributors | InvenTree users already run Python | Extra runtime for most users |
| Fuzzy matching, data tools | Strong | Adequate |
| Typing | Type hints with a strict checker | Native |

Decision: Python. The users and likely contributors come from the
InvenTree community, and Bolt for Python covers everything the connector
needs.

## 4. Slack side

- **Transport.** Socket Mode is the default: an outbound WebSocket, no
  public URL and no TLS setup, which suits a small team behind NAT. HTTP
  with the Events API is supported for teams that prefer it. The listeners
  are the same, and only the entry point differs. The repository ships one
  app manifest per transport.
- **Signature verification** applies to HTTP, for events and for
  interactivity payloads. It is always on and has no switch. In HTTP mode
  the connector refuses to start without a signing secret. Tests cover a
  valid signature, a wrong secret, a changed body, a stale timestamp and
  missing headers.
- **Identity checks.** Every payload must carry the configured workspace id
  and app id. Anything else is dropped.
- **Acknowledge first.** Slack expects an answer within 3 seconds. The
  listener stores the raw event, acknowledges, and processes it later.
  `slack/listener.py` runs the ingest checks inside the Bolt handler, which
  runs before the acknowledgement (`process_before_response`). An accepted
  payload is stored first; if storing fails, the handler raises and Slack
  delivers the event again. A dropped payload is acknowledged and never
  stored. A click has no event id, so its queue key is built from the
  channel, the message, the user and the click's own `action_ts`.
- **Durable queue.** The queue is the `slack_events` table with a
  processing status. Rows that were not finished are picked up again at
  startup, so a restart loses nothing that was acknowledged. A row moves
  `pending` -> `processing` -> `done`, `dropped` or `failed`, and back to
  `pending` only at startup. An event claimed 3 times without finishing is
  `failed`, so one bad payload cannot crash the connector in a loop. Only
  the holder of the instance lock claims and finishes events, and startup
  requeues only events an earlier instance left behind. Delivery is **at
  least once**: after a lock takeover, an event the old instance was still
  processing runs again, so processing checks the lock before any write,
  and everything downstream is idempotent (post keys, request keys,
  markers). Triggers keep the key and the raw body fixed (retention may
  expire the body only of a finished event), keep attempts from going down,
  move the claim only with the status, freeze finished rows, and refuse
  deletes and illegal status changes. An outcome is a short code, never text.
- **Deduplication.** Slack retries deliveries. Events are deduplicated on
  `event_id`, messages on `(workspace, channel, ts)`, and writes on the
  request key (section 7).
- **Filtering.** The first check in every listener is the channel
  allowlist. An empty allowlist means the connector acts nowhere. Message
  subtypes are handled by allowlist: plain messages, file shares and thread
  broadcasts, plus edits and deletions, which become their own values and
  never write by themselves. Any message with a `bot_id`, or an edit of
  one, is dropped, which also stops the connector from reacting to its own
  posts. `slack/ingest.py` implements these checks as a pure function that
  gives every drop a reason. The deduplication there is in memory; the
  durable queue takes it over.
- **Shared channels.** A message or click from a user of another
  organization (Slack Connect) is dropped as `wrong_workspace`. Enterprise
  Grid org installs are not supported yet: `workspace_id` must be a `T` id.
- **Clicks at ingest.** Only the connector's own buttons on a message are
  accepted. Only an approver may click Apply, and ingest drops any other
  user's Apply click. Other buttons (answers, corrections, Undo)
  may come from the poster, so ingest passes them on with the user id, and
  review checks the user against the stored button row.
- **Catch-up after downtime.** Slack does not replay events that were
  missed while the connector was down. At startup and after a reconnect,
  the connector reads the history of each allowlisted channel from the last
  message it stored, up to a configured age (default 72 hours). Posts found
  this way carry the review reason `late_ingest` and are never applied
  automatically. Without this step, downtime would cause silent drift.
- **Grouping.** A photo with a caption arrives as one message. Sibling
  messages from the same user within a short window (default 5 seconds),
  and replies in the thread, join the same post. A message belongs to
  exactly one post. The post key is fixed when the post is created and is
  never recomputed, because Slack does not guarantee the order of events.
  Sibling grouping closes when the dry-run reply is posted. A thread
  reply after that creates a new plan revision. The rule is
  `core/grouping.py`; `Store.place_message` runs it inside one transaction
  under the instance lock. A reply that arrives before its thread root
  creates the post keyed on the root's timestamp, and the root joins it
  when it arrives. The window is compared exactly, to the microsecond.
  The window is measured from the nearest placed message, so siblings
  chain (0 s, 4 s, 8 s is one post). Which messages end up together can
  therefore depend on arrival order (0, 8, 4 gives two posts; a sibling
  whose reply arrived before it keys its own post), but a message never
  changes post once placed, and a reply always joins its root. Timestamps
  must be in Slack's exact form, because keys compare as strings.
- **Plan revisions.** Every plan has a revision number and a hash of the
  messages it was built from. An edit, a deletion or a new message in the
  thread makes the revision stale: its buttons are removed and a new plan
  is built. An approval names one revision and succeeds only if that
  revision is still current. Plans expire (default 24 hours).
- **After apply,** an edit, a deletion or a correction never writes by
  itself. It creates a review item that offers an undo. A revised plan can
  be approved only after the undo has been verified.
- **Interactivity.** Questions and receipts use Block Kit in the thread.
  A button carries only a random id of at least 128 bits that points to a
  row in the connector's database. The handler checks that the channel and
  the message in the payload are the ones stored with that id. Buttons are
  removed once a decision is made.
- **Authorization.** The Slack user id in the payload is checked against
  the configured approvers. An empty approver list means nobody can
  approve. Display names are never trusted. Only the poster or an approver
  can answer a question or correct a post, by button or by thread reply.
- **Undo** is a button on the receipt. A reaction is not enough, because
  anyone who can see the message can add one.
- **Scopes** are the minimum for v1.0: read messages in the channels the
  app is invited to, and write messages. File access is added only when
  photo extraction is built. The exact list will be in the manifest.

## 5. InvenTree side

- **REST only.** The connector calls the public REST API with a token. It
  does not touch the database. It uses its own small async client for the
  handful of endpoints it needs. The official Python client is synchronous,
  has no retries, and discards the response of stock adjustments, which
  the audit trail needs.
- **Version check.** At startup the connector reads `apiVersion` from the
  API root and refuses to enable writes on a version outside the tested
  range. Dry-run still works, with a warning. The test matrix pins the
  current stable release and the previous minor, and also runs the floating
  `stable` tag so that a new release is noticed early.
- **The server is permissive, so the connector is strict.** According to
  the research, InvenTree does not reject a request for more than a stock
  item holds: a transfer moves the whole item, and a remove stops at zero.
  Both return success. The connector therefore checks quantities before
  the write and verifies them after it. It never relies on the server to
  refuse a bad request. Integration tests pin this behavior for every
  supported version.
- **Stock items, not parts.** A transfer moves stock items. The resolve
  step lists the stock items of the matched part in the source location
  itself, without sub-locations. It considers only items that are in
  stock, have status OK and are not allocated to an order. It picks by a
  documented rule: oldest first, and a single item that covers the quantity
  is preferred over a split. If the candidates differ in batch, status or
  supplier part, the choice is a review reason.
- **Movement type to API operation.** Every dry-run shows the operation.

  | Movement type | Operation |
  | --- | --- |
  | `take_to_vehicle`, `take_to_site`, `return`, `transfer` | Transfer |
  | `consume` | Remove by default. Policy can make it a transfer to a site location |
  | `receive` | Add to an existing stock item |
  | `count` | Count, only when the part has exactly one stock item in the location |

  v1.0 never creates stock items or locations. When an add or a count has
  no single target stock item, that is a review reason, and a human creates
  the item in InvenTree.
- **Splits.** A partial transfer splits the stock item. The source keeps
  its id with a lower quantity, and a new stock item appears at the
  destination. For stock that is not serialized, verification and undo
  work on quantities per part and location. Serialized stock is verified
  and undone by stock item and serial number.
- **No merge in v1.0.** The server can merge transferred stock into
  matching stock at the destination. A merge deletes the moving stock item,
  writes the transfer's history entry on the destination item, and removes
  batch identity. That would weaken verification and undo, so v1.0 never
  merges. It sends `merge: false` on every transfer line. Measured
  (`test_char_transfer_merge_setting_on`): on 1.5.6 only an explicit
  `merge: true` merges; the server-wide setting "merge stock on transfer"
  (off by default) does not change a line that omits `merge` or sends
  `false`. 1.4.3 has neither the setting nor the field, and never merges.
  The cost is more small stock items on a vehicle. Merge is an option to
  consider after v1.0.
- **Depletion.** Removing the last unit can delete the stock item together
  with its history, depending on a server setting. Verification treats a
  missing source item as correct only when the plan expected it to reach
  zero. Measured on 1.4.3 and 1.5.6 (`test_char_deplete_deletes_item_keeps_history`):
  the item answers 404, but its history entries survive, so the research
  claim that history is deleted with the item was wrong. The marker of the
  removal that deleted the item is found by search on 1.5.6 and not on
  1.4.3. A history query filtered by the deleted item answers 400, not an
  empty list.
  The authority for "the item is gone" is `GET /api/stock/<pk>/`
  answering 404. The 400 only corroborates it, because a malformed id
  gets a 400 too. Markers are looked up by search, never through the
  item filter alone.
- **Serialized stock** always has quantity 1 per stock item, so "2 units"
  means two stock items. The research found that add and remove on a
  serialized item do nothing and still return success. The connector never
  sends a quantity change for a serialized item. It moves whole items,
  chosen by serial number. A serial read from a photo is unconfirmed until
  a human or a barcode confirms it.
- **Locations.** Vehicles and job sites must exist as stock locations
  before stock can move there. The connector never creates locations by
  itself. The docs guide the admin through modeling them, and a setup check
  lists the Slack users that have no vehicle mapped. Structural locations
  cannot hold stock and are never a match.
- **Least privilege.** InvenTree tokens carry the full permissions of
  their user, so the docs describe a dedicated user with the smallest set
  of roles that allows stock adjustments: view on Part and Stock Location,
  and view and add on Stock Item (`test_char_restricted_user_can_adjust`).
  InvenTree stores "change" together with "add" on Stock Item, so that
  user can also edit stock items; it cannot delete them. The setup check
  reads the roles of the token's user and reports what is missing or
  excessive. According
  to the research, tokens expire after one year by default. Where the API
  exposes the expiry, the connector reports it at startup.
- **Pack units.** A container unit or a stated pack size is always a
  review reason. v1.0 never converts packs. A human gives the quantity in
  the unit the part is stocked in.
- **Characterization tests.** Every server behavior this document relies
  on has a named test that runs against each pinned InvenTree version, on
  PostgreSQL as well as SQLite. If one fails on a version, the connector
  refuses writes on that version. Section 11 lists the tests.
- **Rate limits.** According to the research, the server throttles API
  calls by default in recent versions. The client honors 429 responses
  with backoff for reads. A write is never retried automatically
  (section 7).

## 6. Safety model

The threats these controls answer, and the status of each control, are in
[`threat-model.md`](threat-model.md).

### The write gate

A write happens only if all three are true. Each one is evaluated
independently, and each one alone can block.

1. **Switch.** `writes.enabled` is true. The default is false. Turning it
   on is an explicit configuration step, and the connector logs it at
   startup.
2. **Policy.** The policy allows this movement
   ([0004](decisions/0004-default-write-policy.md)). By default a
   `take_to_vehicle` or `take_to_site` movement with no review reason
   applies automatically, with a receipt and an Undo button. Every other
   movement type waits for a human to click "Apply". `policy.auto_apply`
   lists the movement types that may apply automatically. An empty list
   makes every movement review-first.
3. **Validation.** The plan has no open review reason, every part and
   location is matched to exactly one record, and every stock item is
   resolved.

The gate is evaluated twice: when a plan is approved, and again
immediately before the write. The switch, the approver list and the server
version can change in between.

### The model cannot authorize a write

Leaving a confidence score out of the gate is not enough, because a model
can also steer the gate by what it omits. These rules close that path:

- **Review reasons are computed by rule.** The validator derives every
  text-level reason from the raw text and the structured candidate: implied
  source, missing or vague quantity, container units, hedges, negations,
  questions, instructions aimed at the connector. Flags from the model can
  add a reason. They can never remove one, and the pipeline never depends
  on the model to raise one.
- **Grounding.** Each item text must appear in the message, and each
  quantity must correspond to a number or number word in the message.
  Anything else is a review reason.
- **Limits.** A quantity above `policy.max_quantity` is a review reason.
  So is a quantity above the stock on hand, which the resolve step checks.
- **Matching is deterministic** and takes only the item text, the location
  text and the poster as input (section 10). The extraction prompt never
  contains catalog or alias data, so the model cannot learn part names to
  aim at.
- **Auto-apply is stricter than approval.** A movement applies without a
  click only when every match comes from an exact, normalized or alias
  rule, the vehicle resolves through a mapped Slack user or an exact
  location name, and the model raised no flag. If the rule-based extractor
  produced a candidate that disagrees with the LLM extractor, that is a
  review reason.
- **Implied source.** `policy.default_source` can name a default source
  location per movement type. It is unset by default, and an implied
  source is then a review reason.

A pipeline-level test fixes a post and a catalog, varies every score the
model returns, strips every flag the model returns, and checks that the
gate decision never moves from "blocked" to "allowed".

### Dry-run

Dry-run builds the exact request and shows it, and stops there. It shares
all code with apply up to the HTTP call. The InvenTree client has two
classes: a read-only client, and a writing client that can only be
constructed by the gate. The read-only client rejects every method except
GET at the transport level. With writes off, the writing client does not
exist in the process, and the full test suite with writes off records zero
requests other than GET.

### Untrusted input

Message text and photos are data. They are passed to the model in a
delimited field, and the model's output is parsed against a strict schema.
The model cannot call tools, choose parts, or reach InvenTree. Whatever it
returns, the worst case is a wrong candidate, which then faces validation,
matching against the real catalog, and the gate. The corpus has
prompt-injection cases, and any deviation on them is a hard failure.

### Privacy

- Raw message text is stored in the audit database and nowhere else. Logs
  and the append-only audit log carry ids, not text.
- Message text never goes into InvenTree. The notes of a stock adjustment
  hold the marker, a link to the Slack message and the Slack user id.
- Retention covers every table that repeats message text: raw events,
  extractions, candidates, review answers and stored API calls.
- Photos are not downloaded in v1.0.
- Tokens and secrets are redacted in logs, errors and stored API calls, by
  type (a secret value type that prints as `***`) and by pattern as a
  second layer.
- Text sent to an LLM provider leaves the machine. The docs say so plainly.
  A rule-based filter masks strings that look like secrets before text is
  sent to a remote provider. A local model and the rule-based extractor are
  supported.

## 7. Idempotent apply, verification and undo

This is the highest-risk part of the system. Nothing in this section is
built before the characterization tests of section 11 pass.

### Units of work

| Unit | Key | Rule |
| --- | --- | --- |
| Post | `(workspace id, channel id, ts of the first message)` | Has at most one applied plan revision at a time |
| Plan revision | `(post key, revision)` | Holds 1 to n requests. A new revision can be applied only after the previous one is verified as undone |
| Request | `(post key, revision, sequence number)` | Applied at most once. Has its own marker and its own state |

One Slack post can describe several movements ("2 ONTs to my truck, and
returned 1 switch to receiving"), and movements with different
destinations need separate API requests. InvenTree cannot run them as one
transaction. The requests of a plan therefore run in order. At the first
failure or unknown outcome, the remaining requests are not sent, the plan
is marked `partially_applied`, and the operator is offered an undo of the
requests that completed. Nothing is rolled back automatically.

The database enforces the keys with unique constraints, not with
application logic alone. The local database is not the only guard: the
marker check below also protects against a lost or restored database.

One Slack app has one connector instance. A startup lock with a heartbeat
refuses a second instance on the same database. Two instances on different
databases cannot be prevented by the connector. The read-back detects
their writes after the fact, and the docs warn against that setup.

A plan has its own state: `current`, `stale`, `expired`, `applied`,
`partially_applied`, `undone` or `accepted`.

### States of a request

| State | Meaning | Next |
| --- | --- | --- |
| `planned` | Part of a current plan | `approved`, `rejected`, `cancelled` (plan became stale), `expired` |
| `approved` | A human or the policy approved it | `applying`, `planned` (precondition failed, back to review), `verify_failed` (marker already present), `cancelled` (plan became stale) |
| `applying` | Intent stored, write about to be sent or sent | `applied`, `failed`, `unknown` |
| `failed` | InvenTree refused the request. Nothing was written | `approved`, by a human, as a new attempt |
| `unknown` | The outcome is not known | `applied`, `failed` (no marker and nothing changed) or `verify_failed`, by recovery |
| `applied` | InvenTree accepted the request | `verified`, `verify_failed` |
| `verified` | Read-back agrees with the plan | `undoing` |
| `verify_failed` | Read-back or recovery found something unexpected | `accepted` or `undoing`, by the operator |
| `accepted` | The operator accepted the observed state. Final | |
| `undoing` | Undo intent stored | `undone`, `undo_failed`, `undo_unknown` |
| `undo_failed` | InvenTree refused the undo. Nothing was written | `undoing`, by a human, as a new attempt |
| `undo_unknown` | The outcome of the undo is not known | `undone`, `verified` or `verify_failed`, by recovery |
| `undone` | The undo is verified. Final | |
| `rejected`, `cancelled`, `expired` | Never applied. Final | |

A new plan revision for the post can be approved only when every request
of the earlier revision is in a final state that left no stock change
behind: `undone`, `rejected`, `cancelled`, `expired`, or `failed` with no
later attempt. After `accepted`, the post is closed for further plans.

Transitions are compare-and-set updates in a transaction. Two workers that
race for the same request cannot both move it to `applying`, and a second
click on a button changes nothing.

### The marker

`[sic:<22 random base32 characters>:<a or u><two-digit attempt number>]`
(Crockford's alphabet in lower case: digits and letters without i, l, o, u),
for example `[sic:k7m2q9x4t1b8w3n6p5r0zd:a01]`. `a` marks an apply and `u`
an undo. The random id belongs to one request, and every attempt of that
request shares it.

- The marker has a fixed length and comes first in the notes. The notes
  hold nothing else except a link to the Slack message and the Slack user
  id, and they stay under 200 characters. The server stores notes in a
  `varchar(512)`, which PostgreSQL enforces (`test_char_notes_length`).
- The search for a marker reads every page of results and compares the
  whole token, because the server search matches substrings.
- **A marker that is found proves the write happened. A marker that is not
  found proves nothing.** On 1.4.3 the marker of a removal that deleted
  its stock item cannot be found (section 5, Depletion), an admin can
  delete history. History was readable as soon as the server answered
  in the measured setup (one server process with SQLite,
  `test_char_marker_visible_after_response`), but that is not assumed
  for every deployment.

### Apply protocol

All steps from 2 to 6 run under one writer lock in the process, so the
connector never has two InvenTree writes in flight and cannot race itself.

1. **Gate.** Evaluate the write gate again.
2. **Marker check.** Search the stock history for the marker id of this
   request, from any attempt, apply or undo. A hit that the recorded state
   does not explain stops the apply and moves the request to
   `verify_failed` for the operator. Markers of the other requests of the
   post are compared with their recorded states in the same way: a
   verified request must have its marker, and a request that never ran
   must not.
3. **Snapshot and precondition.** Read every stock item of each part in
   the plan, at the source and at the destination location, without
   sub-locations. Compare with what the plan was built on. If a source
   holds less than the plan takes from it, or anything relevant changed,
   stop: the request returns to `planned` and the post to review.
   Also record the ids of the history entries that already exist on each
   source stock item the request takes from. Otherwise store the snapshot,
   the exact request body and the move to `applying` in one transaction.
4. **Write.** Send the request once. A request that InvenTree refuses
   with a 4xx changes nothing: all lines succeed or fail together
   (`test_char_multi_item_request_is_atomic`). A 5xx is not that safe. On
   1.5.6 with PostgreSQL, a request whose history entry fails to save
   answers 500 after the stock change is committed, and no entry carries
   the marker (`test_char_overlong_notes_write_without_history`). That is
   why a 5xx is an unknown outcome and recovery compares quantities.
5. **Read back.** Three checks, and any failure means `verify_failed`:
   - the history entries that carry the marker have the deltas of the
     plan;
   - the quantities per part and location match the snapshot plus the
     plan;
   - the source stock items and the items the request created have no
     history entry that is missing from the snapshot and lacks this
     request's marker. Such an entry means another writer touched the
     same stock during the apply.
6. **Record.** Move to `verified` and post the receipt.

Another InvenTree user can still change the stock between steps 3 and 4.
The connector cannot lock InvenTree, so that window cannot be closed, only
detected by step 5.
A concurrent write can also make the connector's own request move less
than it asked for (a transfer of more than an item holds moves the whole
item), and on 1.4.3 with PostgreSQL two concurrent splits of one item can
even create stock (`test_char_concurrent_transfers_same_item`). The first
two checks of step 5 catch the short move (a delta of 1 where the plan says
4), but not the 1.4.3 lost update: there the connector's own deltas and the
source and destination quantities all match the plan, and the extra stock
appears at a third location outside the snapshot. The third check catches
it, because the other writer's split leaves an entry without the marker on
the source item. The price is that any concurrent write to the same stock
items ends in `verify_failed` and goes to the operator, even a harmless
one. A concurrent writer that touches none of these items cannot be
detected, and it also cannot corrupt the connector's movement.

### Unknown outcomes and recovery

An outcome is **known** only when InvenTree itself answered: a 2xx status,
or a 4xx status with a JSON body in InvenTree's error format. A known 4xx
moves the request to `failed`. Everything else is **unknown**: timeouts,
connection errors, every 5xx, the statuses 408, 429 and 499, and any answer
whose body does not come from InvenTree, such as a proxy error page. A
crash while a request is in `applying` is unknown as well.

A request that timed out on the client may still be running on the
server, so recovery waits before it concludes anything:

1. The request is quarantined for `apply.settle_seconds` (default 120,
   and it must exceed the request timeout of the server).
2. After that, recovery searches for the marker and reads the quantities.

| Marker | Quantities compared with the snapshot | Result |
| --- | --- | --- |
| Found | Match the plan | Continue with read-back, then `verified` |
| Found | Anything else | `verify_failed` |
| Not found | Unchanged | `failed`: the operator may approve a new attempt |
| Not found | Changed | `verify_failed` |

A new attempt, after a failure or after recovery, gets a new attempt
number in its marker. It runs the whole protocol again, and step 2
searches for the markers of **all** earlier attempts, so a late write from
an earlier attempt blocks it. A retry never happens without a human.

`verify_failed` blocks further writes for the post. The operator sees the
snapshot, the plan and the current state side by side, and chooses: accept
the state as it is, or reverse the change that was observed.

### Undo

Undo reverses **what was observed** in the stock history at apply time, not
what the message or the plan said. It goes through the same protocol, with
its own marker, read-back and recovery.

Undo is allowed only if the stock the apply touched has no later history
entries except the connector's own, and the condition in the table holds.
Otherwise undo is refused with an explanation and becomes a review item.
Undo expires after `undo.max_age` (default 7 days).

| Operation | Undo | Condition |
| --- | --- | --- |
| Transfer | Transfer the same stock back to the source location | The destination stock items exist and hold at least the moved quantity |
| Remove | Add the quantity back | The stock item still exists. If it was deleted on depletion, a human recreates it |
| Add | Remove the same quantity | The stock item holds at least the added quantity |
| Count | Count back to the recorded earlier value | The current value is still the one the connector set |

For stock that is not serialized, undo restores quantities per part and
location. It does not promise to restore stock item ids, because a split
changes them.

## 8. Data model and audit trail

The store is SQLite in a single file, which suits a single-node deployment
and needs no extra service.

| Table | Content |
| --- | --- |
| `slack_events` | Raw events with a processing status, unique on `event_id` |
| `posts` | One row per post, unique on post key |
| `post_messages` | Which post each message belongs to, unique on `(workspace, channel, ts)`: ids, author and thread, never text (migration 0003) |
| `plans` | Plan revisions with the hash of their input messages |
| `extractions` | Extractor name and version, prompt version, raw output |
| `candidates` | Candidate movements and line items |
| `matches` | Match results with candidates and the rule that matched |
| `review_items` | Reasons, the question asked, the answer, who answered |
| `requests` | The state machine of section 7, unique on the request key, with the marker id |
| `request_attempts` | One row per apply or undo attempt: marker, snapshot, request body, outcome |
| `interactions` | Button ids with the message they belong to, the plan revision and the decision |
| `instance_lock` | The single-instance lock with its heartbeat |
| `api_calls` | Every InvenTree request and response, redacted |
| `aliases` | Alias, target, who confirmed it, from which review item |
| `audit_log` | Append-only log of every state change, ids only |

`audit_log` rows are never updated or deleted by the application. Database
triggers reject both. Triggers also keep every key column fixed, keep
the recorded intent of an attempt (marker, snapshot, request body) fixed,
and keep rows that carry a key from being deleted. The first migration
holds the tables of section 7; the others arrive with their features
([ADR 0009](decisions/0009-audit-store.md)). The log holds ids and never message text, so it can
stay complete when retention removes text elsewhere. Retention of text is
configurable, and expiry replaces the text while keeping the row.

An operator can answer three questions from the trail: what did the
connector see, why did it decide what it decided, and what exactly did it
send to InvenTree.

## 9. Extraction

- **Interface.** `extract(post) -> list[candidate]`, with two
  implementations: rule-based and LLM.
- **Rule-based extractor** (`core/extract_rules.py`). Needs no network
  and no key. It handles the common patterns (quantity, item, preposition,
  location), spelled-out numbers and units. It is the fallback, the test
  oracle for the simple cases, and the default in the quickstart.
  - It is precision-first. It reads one message of a known shape and
    **declines** the rest: photos, threads, sibling messages, lists,
    several movements, a third person as the actor, or a post without a
    verb that does not open with a number. `Extraction.declined` means "could
    not read", which is not the same as "not a movement". Questions,
    requests, plans, conditions and negations are "not a movement".
  - Post flags are raised on every post, even a declined one. A sentence
    that holds instructions to the connector is dropped before parsing.
  - Flags that follow from the fields are computed by rule, never guessed.
  - Corpus 0.2.0 (`tests/core/test_extract_rules_corpus.py`, matching not
    scored): 0 hard failures; movement detection precision 100%, recall
    63.4%; flag precision 100%, recall 53.1%; case exact match 70.3%. The
    30 missed movements are all declined posts: photos, threads and
    sibling messages (11), lists, headings and multi-movement posts (7),
    and wording it does not know (12: "gave", "swapped", a third person,
    no quantity, Slack chatter around the movement, Spanish, text after a
    comma that ends the location).
  - Nothing is left unread: text after a comma that ends a location, or a
    number word after a quantity ("two dozen", "twenty five", "a
    hundred"), declines the post. "1,500" is one quantity.
- **LLM extractor.** A provider interface with a strict JSON schema for
  the output. v1.0 ships one adapter for OpenAI-compatible endpoints,
  which covers hosted providers and local servers. More adapters can
  follow. The provider is optional at install time.
- **Disagreement.** When both extractors run and disagree on a quantity, a
  movement type or a location, that is a review reason.
- **Versioning.** Prompt and schema are versioned, and the version is
  stored with every extraction.

## 10. Matching

Deterministic. The rules run in this order:

1. **exact**: the text equals an IPN or a part name, or a location path
   or name.
2. **normalized**: the same after folding case, punctuation, plurals,
   possessives and spacing.
3. **alias**: a confirmed alias, or "my truck" for the poster's vehicle.
4. **words**: every word of the text is one of the record's words (name,
   keywords and aliases; path and description for locations). "Disc 40"
   finds "Norvane Disc 40 Access Point". A word such as "3ft" or "van3"
   may be split into letters and digits.
5. **fuzzy**: as rule 4, but a word of 4 letters or more may be misspelled
   (`difflib` similarity 0.8, so "ondrl" finds "Ondrel"). Words with digits
   never get this allowance, so "truck 7" never becomes "Truck 1".

- The first rule with any hit decides. One hit is `matched`, more than one
  is `ambiguous` with every hit as a candidate. Matching does not fall
  through to a later rule to find a single winner. The candidate list is
  capped where it is shown, not in the result.
- A single hit at rule 4 or 5 is `matched`, because the corpus clerk would
  pick that one record. It still cannot authorize a write: it raises the
  `fuzzy_match` review reason. Only rules 1 to 3 authorize, and the code
  enforces that with an allowlist (`Match.can_authorize_a_write`).
- Corpus 0.2.0, with the expected text as input (`tests/core/test_matching_corpus.py`):
  part match 100% (109 results) and location match 100% (92 results).
  Of the 99 single part matches, 81 came from rule 4 or 5 ("Disc 40",
  "ONTs"), and so did 20 of the 88 single location matches. Under the
  current policy all of those need a confirmation.
- Plural endings are folded only on words without digits at rules 2 and
  3 ("40S" may be another model), and "-es" only after a hissing sound
  ("boxes", "switches", but "tapes" is never "tap"). Rules 4 and 5 fold
  both kinds ("d40s", "tapes"), and those rules never authorize. Where
  folding still makes two records look the same ("ups": "UP" or "UPS"),
  the result is ambiguous.
- A trailing "+" is part of the word at rules 1 to 3 ("SFP+" and "SFP",
  "PoE+" and "PoE" are different products). Rules 4 and 5 ignore it, so
  "poe injector" still finds "PoE+ Injector", for confirmation.
- Template, inactive and virtual parts are never a match. Structural
  locations are never a match.
- "My truck" resolves through the mapping from Slack user to vehicle,
  only when the word ("truck", "van") names that vehicle in its path,
  description or aliases. "My trailer" from a user with a truck goes
  through the other rules instead. Aliases that contain such words are stored per user.
- An alias is checked against the catalog snapshot when it is used. An
  alias that points to an inactive part or a deleted location is ignored
  and reported.

## 11. Testing strategy

| Layer | How | Where |
| --- | --- | --- |
| Pure core | Unit and property tests | Every PR |
| Safety invariants | Named tests, one per invariant, listed in the docs | Every PR |
| Slack | Recorded event fixtures, signature tests, no network | Every PR |
| Extraction | Corpus with recorded model outputs | Every PR |
| Extraction, live | Corpus against a real provider | When a key is set |
| InvenTree behavior | Characterization tests, real server in Docker, pinned versions | When relevant paths change |
| Apply and undo | Parallel, restart and fault tests against the real server | When relevant paths change |
| Docs | Quickstart followed from scratch | Every milestone |

CI stays lean: integration jobs run only when their paths change, and
dependencies are cached.

### Characterization tests

The research read InvenTree's behavior from documentation and source code.
It did not run it. These tests turn each claim the design depends on into
a fact, per pinned version. They use a raw HTTP helper that exists only in
the test suite.

| Test | Claim |
| --- | --- |
| `test_char_over_quantity_transfer` | A transfer of more than an item holds is not rejected |
| `test_char_over_quantity_remove` | A remove of more than an item holds stops at zero |
| `test_char_serialized_quantity_noop` | Add and remove on a serialized item change nothing, return success, and whether they still write a history entry |
| `test_char_deplete_deletes_item_keeps_history` | Depletion can delete the stock item. The earlier history survives. The removal's marker is found by search on 1.5.6 but not on 1.4.3 |
| `test_char_partial_transfer_split` | A partial transfer keeps the source id and creates a new item |
| `test_char_marker_on_every_entry_type` | The notes reach every history entry a request writes |
| `test_char_marker_visible_after_response` | Whether history is readable as soon as the server has answered |
| `test_char_marker_search_is_substring` | The history search matches substrings, and `limit` with `offset` pages through it without overlap |
| `test_char_notes_length` | Notes up to 512 characters are stored whole. Longer ones: SQLite stores them, PostgreSQL answers 500 |
| `test_char_overlong_notes_write_without_history` | PostgreSQL: on 1.5.6 that 500 still commits the stock change, with no history entry. On 1.4.3 it changes nothing |
| `test_char_multi_item_request_is_atomic` | One bad line fails the whole request |
| `test_char_restricted_user_can_adjust` | View on Part and Stock Location plus view and add on Stock Item allow transfer, add, remove and count. The server adds "change" to "add"; delete stays refused |
| `test_char_user_without_stock_add_cannot_adjust` | Without "add" on Stock Item every adjustment is a 403 that changes nothing, even with "change" |
| `test_char_concurrent_transfers_same_item` | Two simultaneous transfers of 4 from an item of 5. SQLite: colliding requests give one 201 and one 5xx that wrote nothing; requests handled one after the other behave like PostgreSQL 1.5.6. PostgreSQL 1.5.6: both 201, the second moves only the 1 left, so a 201 can move less than asked. PostgreSQL 1.4.3: both 201 and stock grows to 9 (a lost update) |
| `test_char_transfer_does_not_merge` | A transfer to a location that holds the same part leaves separate items (server-wide merge setting off) |
| `test_char_transfer_merge_setting_on` | The server-wide merge setting does not merge a line that omits `merge` or sends `false`; only `merge: true` merges (1.5.6) |
| `test_char_stock_list_cascade_false` | A stock list with `cascade=false` leaves out sub-locations |
| `test_char_error_body_format` | The shape of a 4xx answer from InvenTree, which recovery uses to tell it from a proxy |

Two Slack claims need a capture from a real workspace before the fixtures
are frozen: a photo with a caption arrives as one message, and the app
receives events only from channels it is a member of.

A fault proxy between the connector and InvenTree delays, drops and
corrupts responses (`tests/support/faultproxy.py`). Each fault happens
either before the request reaches the server or after the server answered.
`test_char_failure_seen_by_client_hides_committed_transfer` shows that a
client timeout, a lost connection, a proxy error page and a cut body can
each hide a transfer that committed. The proxy drives the M3 tests for
unknown outcomes, including
`test_late_commit_after_client_timeout_never_double_applies`.

## 12. Configuration

One TOML file plus environment variables for secrets. Settings are
validated at startup, and the connector refuses to start on an unknown key,
a value of the wrong type ("yes" is not a boolean) or a secret in the file.
Errors name the setting and never repeat its value.
[`stockthread.example.toml`](../stockthread.example.toml) lists every
setting with its default, and `stockthread check-config` validates a file. The startup
log prints the effective settings with secrets redacted, and states in one
line whether writes are on.

## 13. Proposed v1.0 scope **(decision pending)**

In:

- One Slack workspace and one InvenTree instance
- Socket Mode and HTTP
- Text posts, and photo posts through their caption
- Transfer, remove, add, and count for the single stock item case
- Review and clarification in the thread
- Apply with read-back, receipts and undo
- Alias learning from confirmed corrections
- Rule-based extractor, and LLM extractor with one provider adapter
- Catch-up after downtime
- Docker image built locally, and docker compose file

Out:

- Reading quantities, serials or items from photo pixels (experimental
  after v1.0)
- Merge on transfer, pack conversion, creating stock items or locations
- Reminders for unanswered questions (plans expire instead)
- A web dashboard
- Purchase orders, build orders and sales orders
- Multiple workspaces or instances in one deployment
- Other chat platforms
- A hosted service

## 14. Open questions

Tracked as issues labeled `needs-dan`. See the decision log in
[`decisions/`](decisions/).
