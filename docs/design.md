# Design

Status: proposal, 2026-09-28. Sections marked **(decision pending)** wait for
an answer from Dan. Everything else is the working design.

Background research, with sources, is in [`research/`](research/):
[InvenTree](research/inventree.md), [Slack](research/slack.md),
[prior art and stack](research/prior-art-and-stack.md).

## 1. Goal

Turn informal Slack posts about stock ("2 L7 Lite on my truck") into correct,
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

Every stage is a function from typed input to typed output. Only three
stages touch the outside world: Ingest and Review talk to Slack, Resolve
reads InvenTree, and Apply writes to InvenTree. Everything between is pure
and is tested without network access.

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

A pure core (stages 2 to 7 and the gate) with adapters for Slack, InvenTree
and storage. Shipped as one service.

- Good: the core is tested without network access. Another chat platform, a
  CLI or a small plugin can reuse it later. This matches the shape of the
  pipeline.
- Bad: abstraction can grow beyond what one service needs. The answer is to
  keep it to one package, with adapter interfaces only where a second
  implementation exists or is planned.

### Recommendation **(decision pending)**

Option C, deployed as in option A: one Python package, one container, a pure
core and three adapters. A thin InvenTree plugin can come after v1.0 if
users ask for settings inside the InvenTree UI.

### Language **(decision pending)**

| | Python | TypeScript |
| --- | --- | --- |
| Slack SDK | Bolt for Python, maintained, async | Bolt for JS, maintained |
| InvenTree fit | Same language as InvenTree and its client | None |
| Contributors | InvenTree users already run Python | Extra runtime for most users |
| Fuzzy matching, data tools | Strong | Adequate |
| Typing | Type hints with a strict checker | Native |

Recommendation: Python. The users and likely contributors come from the
InvenTree community, and Bolt for Python covers everything the connector
needs.

## 4. Slack side

- **Transport.** Socket Mode is the default: an outbound WebSocket, no
  public URL and no TLS setup, which suits a small team behind NAT. HTTP
  with the Events API is supported for teams that prefer it. The listeners
  are the same, and only the entry point differs. The repository ships one
  app manifest per transport.
- **Signature verification** applies to HTTP. It is always on and has no
  switch. Tests cover a valid signature, a wrong secret, a changed body, a
  stale timestamp and missing headers.
- **Acknowledge first.** Slack expects an answer within 3 seconds. The
  listener stores the raw event, acknowledges, and hands the work to a
  queue.
- **Deduplication.** Slack retries deliveries. Events are deduplicated on
  `event_id`, and movements on the post key (section 7).
- **Filtering.** The first check in every listener is the channel
  allowlist. Bot posts are detected by `bot_id` as well as by subtype.
  Edits and deletions never create or change an applied movement. They
  create a review item if the post already has one.
- **Grouping.** A photo with a caption arrives as one message. Sibling
  messages from the same user within a short window, and replies in the
  thread, join the same post. Grouping closes when the dry-run reply is
  posted. Later messages in the thread are treated as answers or
  corrections.
- **Interactivity.** Questions and receipts use Block Kit in the thread.
  Button values carry only an opaque id that points to a row in the
  connector's database, never trusted data.
- **Authorization.** The Slack user id in the payload is checked against
  the configured approvers. Display names are never trusted.
- **Undo** is a button on the receipt. A reaction is not enough, because
  anyone who can see the message can add one.
- **Scopes** are the minimum: read messages in the channels the app is
  invited to, read files, write messages. The exact list is in the Slack
  research notes and will be in the manifest.

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
  step lists the stock items of the matched part in the source location and
  picks them by a documented rule: oldest first, and a single item that
  covers the quantity is preferred over a split. Any choice the rule cannot
  make is a review reason.
- **Splits and merges.** A partial transfer splits the stock item. The
  source keeps its id with a lower quantity, and a new stock item appears
  at the destination. Repeated small transfers would litter a vehicle with
  many stock items, so transfers ask the server to merge into matching
  stock where the API version supports it. Verification and undo work on
  quantities per part and location, never on stock item ids alone.
- **Depletion.** Removing the last unit can delete the stock item together
  with its history, depending on a server setting. Verification treats a
  missing source item as correct only when the plan expected it to reach
  zero.
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
  of roles that allows stock adjustments. Tokens expire (one year by
  default). The connector reports the expiry at startup and warns before
  it is reached.
- **Rate limits.** The server throttles API calls by default in recent
  versions. The client honors 429 responses with backoff. A write is never
  retried automatically (section 7).

## 6. Safety model

### The write gate

A write happens only if all three are true. Each one is evaluated
independently, and each one alone can block.

1. **Switch.** `writes.enabled` is true. The default is false. Turning it
   on is an explicit configuration step, and the connector logs it at
   startup.
2. **Policy.** The policy allows this movement. The default policy
   **(decision pending)** is review-first: a human approves every movement
   with a button.
3. **Validation.** The plan has no open review reason, every part and
   location is matched to exactly one record, and every stock item is
   resolved.

Confidence is not an input. The gate function receives the plan, the policy
and the switch, and the plan type has no confidence field. A model score
can add a review reason. It can never remove one. A property test feeds
the gate arbitrary confidence values through every available path and
checks that the decision does not change.

### Dry-run

Dry-run builds the exact request and shows it, and stops there. It shares
all code with apply up to the HTTP call. The InvenTree client has two
classes: a read-only client, and a writing client that can only be
constructed by the gate. With writes off, the writing client does not exist
in the process, and a test asserts that.

### Untrusted input

Message text and photos are data. They are passed to the model in a
delimited field, and the model's output is parsed against a strict schema.
The model cannot call tools, choose parts, or reach InvenTree. Whatever it
returns, the worst case is a wrong candidate, which then faces validation,
matching against the real catalog, and the gate. The corpus has
prompt-injection cases.

### Privacy

- Raw message text is stored in the audit database and nowhere else. Logs
  carry ids, not text.
- Photos are fetched when needed and are not stored. The audit trail keeps
  the file id and a hash.
- Tokens and secrets are redacted in logs, errors and stored API calls, by
  type (a secret value type that prints as `***`) and by pattern as a
  second layer.
- Text sent to an LLM provider leaves the machine. The docs say so plainly,
  and a local model and the rule-based extractor are supported.

## 7. Idempotent apply, verification and undo

This is the highest-risk part of the system.

### Post key

`post_key = (workspace id, channel id, ts of the first message)`. One post
key has at most one applied movement set. The database enforces it with a
unique constraint, not with application logic alone.

### State machine

```text
planned ──▶ approved ──▶ applying ──▶ applied ──▶ verified ──▶ undoing ──▶ undone
                            │            │                        │
                            ▼            ▼                        ▼
                         failed     verify_failed            undo_failed
```

Transitions are compare-and-set updates in a transaction. Two workers that
race for the same post cannot both move it to `applying`.

### Apply protocol

1. **Intent.** In one transaction, move the movement to `applying` and
   store the exact request body, plus a snapshot of the quantities of every
   stock item involved.
2. **Marker.** The request's notes field carries a marker that names the
   Slack post and a short unique key. InvenTree stores the notes on the
   stock history entries it writes, so the marker is visible to operators
   and can be searched by the connector. The notes field is short, so the
   marker is compact and the connector checks the length before sending.
3. **Precondition.** Read the source stock items again. If a quantity
   changed since the plan was shown, or a source holds less than the plan
   takes from it, stop and return to review.
4. **Write.** One API request per movement. InvenTree runs each stock
   adjustment request in one database transaction, so all lines of a
   movement succeed or fail together. A write request is sent once. A
   timeout or a connection error is an unknown outcome, and it goes to
   recovery, not to a retry.
5. **Read back.** Read the stock items and the stock history. Check that
   the quantity of each part fell at the source and rose at the
   destination by the planned amounts, and that history entries carry the
   marker.
6. **Record.** Move to `verified` and post the receipt.

Between steps 3 and 4 another user can still change the stock. The
connector cannot lock InvenTree, so the window cannot be closed, only
detected: the read-back in step 5 compares against the snapshot from step
1, and any difference beyond the planned change moves the movement to
`verify_failed` and tells the operator.

### Crash recovery

A crash or a timeout between steps 4 and 6 leaves a movement in
`applying`. At startup, and after any unknown outcome, the connector
searches the stock history for the marker.

- The marker is there: the write happened. Continue with read-back.
- The marker is not there, and the quantities equal the snapshot: the
  write did not happen. The movement returns to `approved` and an operator
  is told. It is never retried without a human.
- Anything else: the state is unknown. This includes the case where a
  depleted stock item was deleted together with its history. The movement
  moves to `verify_failed`, further writes for the post are blocked, and
  the operator gets the snapshot and the current quantities side by side.

### Undo

Undo builds the reverse of the recorded movement from the audit trail, not
from the message. It goes through the same protocol, with its own marker
and read-back. Undo is refused with an explanation if the stock has moved
on since, for example when part of it was already transferred elsewhere.
Undo is itself idempotent: a second click does nothing.

| Movement | Undo |
| --- | --- |
| Transfer | Transfer the same quantities back to the source location |
| Remove | Add the quantity back. If the stock item was deleted on depletion, this needs a new stock item and goes to review |
| Add | Remove the same quantity |
| Count | Count back to the recorded earlier value, only if the current value is still the one the connector set |

Undo restores quantities per part and location. It does not promise to
restore stock item ids, because splits and merges change them.

## 8. Data model and audit trail

The store is SQLite in a single file, which suits a single-node deployment
and needs no extra service. Access goes through a small repository layer,
so another database can be added if a user needs one.

| Table | Content |
| --- | --- |
| `slack_events` | Raw events, unique on `event_id` |
| `posts` | Grouped messages, unique on post key |
| `extractions` | Extractor name and version, prompt version, raw output |
| `candidates` | Candidate movements and line items |
| `matches` | Match results with candidates and the rule that matched |
| `review_items` | Reasons, the question asked, the answer, who answered |
| `movements` | The state machine of section 7, unique on post key |
| `api_calls` | Every InvenTree request and response, redacted |
| `aliases` | Alias, target, who confirmed it, from which review item |
| `audit_log` | Append-only log of every state change |

`audit_log` rows are never updated or deleted by the application. Database
triggers reject both. Retention of raw text is configurable, and expiry
replaces the text while keeping the row.

An operator can answer three questions from the trail: what did the
connector see, why did it decide what it decided, and what exactly did it
send to InvenTree.

## 9. Extraction

- **Interface.** `extract(post) -> list[candidate]`, with two
  implementations: rule-based and LLM.
- **Rule-based extractor.** Needs no network and no key. It handles the
  common patterns (quantity, item, preposition, location), spelled-out
  numbers and units. It is the fallback, the test oracle for the simple
  cases, and the default in the quickstart.
- **LLM extractor.** A provider interface with a strict JSON schema for
  the output. Planned providers: Anthropic, any OpenAI-compatible endpoint
  (which covers local servers), and none. The provider is optional at
  install time.
- **Disagreement.** When both extractors run and disagree on a quantity, a
  movement type or a location, that is a review reason.
- **Versioning.** Prompt and schema are versioned, and the version is
  stored with every extraction.

## 10. Matching

Deterministic, in this order. The first rule that produces a result wins.

1. Exact match on IPN or name.
2. Normalized match: case, punctuation, plurals, possessives, spacing.
3. Confirmed alias.
4. Token and fuzzy candidates, with a score threshold and a cap on the
   list length.

One result from rules 1 to 3 is `matched`. Any result from rule 4 is a
candidate list for a human, even when the list has one entry and the score
is high, unless the entry is the only part that contains all the tokens.
The exact rule will be tuned against the corpus and documented with its
scores. "My truck" resolves through the mapping from Slack user to vehicle.

## 11. Testing strategy

| Layer | How | Where |
| --- | --- | --- |
| Pure core | Unit and property tests | Every PR |
| Safety invariants | Named tests, one per invariant, listed in the docs | Every PR |
| Slack | Recorded event fixtures, signature tests, no network | Every PR |
| Extraction | Corpus with recorded model outputs | Every PR |
| Extraction, live | Corpus against a real provider | When a key is set |
| InvenTree | Real server in Docker, pinned versions, seeded from `eval/catalog/` | When relevant paths change |
| Idempotency | Parallel and restart tests against the real server | When relevant paths change |
| Docs | Quickstart followed from scratch | Every milestone |

CI stays lean: integration jobs run only when their paths change, and
dependencies are cached.

## 12. Configuration

One file plus environment variables for secrets. Settings are validated at
startup, and the connector refuses to start on an unknown key. The startup
log prints the effective settings with secrets redacted, and states in one
line whether writes are on.

## 13. Proposed v1.0 scope **(decision pending)**

In:

- One Slack workspace and one InvenTree instance
- Socket Mode and HTTP
- Text posts, and photo posts through their caption
- Transfer, remove, add and count
- Review and clarification in the thread
- Apply with read-back, receipts and undo
- Alias learning from confirmed corrections
- Rule-based extractor, and LLM extractor with pluggable providers
- Docker image built locally, and docker compose file

Out:

- Reading quantities, serials or items from photo pixels (experimental
  after v1.0)
- A web dashboard
- Purchase orders, build orders and sales orders
- Multiple workspaces or instances in one deployment
- Other chat platforms
- A hosted service

## 14. Open questions

Tracked as issues labeled `needs-dan`. See the decision log in
[`decisions/`](decisions/).
