# Roadmap

The path to v1.0. Each milestone has a definition of done (DoD). A milestone
is finished only when every DoD line is true, the docs match the behavior,
and the README quickstart has been followed from scratch in a temp directory.

The design is in [`docs/design.md`](docs/design.md). Decisions are in
[`docs/decisions/`](docs/decisions/).

Safety invariants apply from the first line of code, and each one has tests:

1. Stock writes are off by default.
2. Dry-run is always available and never writes.
3. Model confidence never authorizes a write.
4. Each planned request is applied at most once, across retries, restarts
   and unknown outcomes. A post has at most one applied plan at a time.
5. Every write is read back and verified.
6. Every applied movement has an undo that restores the quantities exactly,
   or refuses with an explanation.

## Phase 0: research and design

- [x] Research notes: InvenTree, Slack, prior art and stack (`docs/research/`)
- [x] Design document (`docs/design.md`)
- [x] Roadmap (this file)
- [x] Eval corpus v0.1 with format, schema and validator (`eval/`)
- [x] Reference scorer for the corpus (`eval/tools/score.py`)
- [x] Lean CI for the corpus
- [x] Shape, stack and default policy decided ([0003](docs/decisions/0003-standalone-python-service.md), [0004](docs/decisions/0004-default-write-policy.md))
- [x] Name decided: stockthread ([0005](docs/decisions/0005-project-name.md))
- [x] License decided: MIT ([0008](docs/decisions/0008-mit-license.md))
- [x] Attribution decided: none ([0010](docs/decisions/0010-no-attribution.md))
- [x] Docker works on the build machine (#2)

## M0: foundations

- [x] Project scaffold: package layout, formatter, linter, type checker, test runner ([0006](docs/decisions/0006-python-tooling.md))
- [x] CI: format, lint, types and unit tests on every PR, with dependency caching
- [x] Configuration: typed settings from environment and file, validated at startup, secrets redacted in every log and error
- [x] Domain model: post, plan revision with input hash, request, line item, match result, review reason, decision
- [x] Audit store: schema, migrations, append-only log, request keys, marker and snapshot columns ([0009](docs/decisions/0009-audit-store.md))
- [x] InvenTree test harness: pinned versions in Docker, seeded from `eval/catalog/`, torn down after each run, on PostgreSQL as well as SQLite for every pinned version
- [ ] Characterization tests for every InvenTree behavior the design relies on (design section 11; its table lists 36 tests, kept equal to the suite by `tests/test_docs_characterization.py`)
- [x] Characterization of a transfer with the server-wide "merge stock on transfer" setting on
- [x] Fault proxy for delayed, dropped and failed responses (`tests/support/faultproxy.py`)
- [x] InvenTree client: read-only, rejects every method except GET, with API version check at startup ([0007](docs/decisions/0007-inventree-read-client.md))
- [x] Threat model, first version ([`docs/threat-model.md`](docs/threat-model.md))
- [x] `SECURITY.md` (GitHub private vulnerability reporting; turn it on when the repository goes public), `CONTRIBUTING.md`, `CHANGELOG.md`

**DoD:** a contributor can clone, install and run the full gate with one
documented command. Integration tests read the seeded catalog from a real
InvenTree in CI. The characterization tests pass on every pinned version,
and each failure is reflected in the design. No code path in the package
can write stock yet: only the test suite has a raw HTTP helper.

## M1: read-only extraction and matching, dry-run in Slack

- [x] Slack ingest over Socket Mode and HTTP, with signature verification on HTTP (listener `slack/listener.py` checks, stores, then acknowledges; the worker is `worker.py`; `stockthread run` serves Socket Mode, or HTTP through waitress (`slack/http_server.py`, ADR 0013), tested over a real socket with signed fixtures; HTTP not yet tried against Slack itself, which needs a public https URL)
- [x] Workspace and app id checks, channel allowlist, subtype allowlist, bot filtering, deduplication (`slack/ingest.py`; in-memory dedup until the durable queue)
- [x] Durable event queue (`slack_events`, migration 0002), and catch-up from channel history after downtime (`slack/catchup.py`, migration 0006; a deletion made during downtime is not seen)
- [x] Grouping: photo with caption, sibling messages and threads become one post (`core/grouping.py`, migration 0003; the worker places each message)
- [x] Rule-based extractor that needs no LLM (`core/extract_rules.py`; one message per post for now, declines the rest)
- [x] LLM extractor behind a provider interface, with a strict output schema (`llm/extract.py`: prompt `llm-0.2`, strict parser, secret masking; `llm/openai_compatible.py`; `[llm]` settings; the worker asks it for the posts the rules decline, ADR 0011; scored on `anthropic/claude-haiku-4.5`, scores in `docs/design.md`)
- [x] Eval runner: scores either extractor against the corpus, using recorded fixtures in CI (rules: `tests/core/test_extract_rules_corpus.py`; model: `python -m stockthread.llm.record` records, `tests/llm/test_recorded_corpus.py` scores; one model recorded in `eval/recorded/`)
- [x] Validator: deterministic contradiction checks and text-level review reasons (`core/validate.py`)
- [x] Matcher: exact, normalized, alias, then fuzzy candidates, for parts and locations (`core/matching.py`; the catalog snapshot is read from InvenTree by `catalog.py`; "my truck" resolves through the `[vehicles]` settings)
- [x] Dry-run reply in the Slack thread: what would change, and every review reason (renderer `slack/replies.py`; `worker.py` posts it once the sibling window has passed; `stockthread run` serves it; tried in a dev workspace on 2026-09-30 for a post found by catch-up: one late dry run in the thread, no reply to chatter; a post delivered over Socket Mode while the connector runs is not yet tried there)
- [ ] Slack app manifests and the quickstart (Socket Mode manifest `manifests/slack-socket.json` and the README quickstart done; install and `check-config` followed from scratch; the Socket Mode manifest, tokens and channel invite worked in a dev workspace on 2026-09-30, set up by the owner rather than by a newcomer following the quickstart; HTTP manifest `manifests/slack-http.json` and its README section done; on 2026-10-25 steps 1 and 4-6 followed as written from a fresh clone in a temp dir against InvenTree 1.5.6: the step 4 read-only token passed `check-setup`, and `run` connected over Socket Mode to the dev workspace; step 7, a live post while it runs, needs a person: #182)

**DoD:** a post in an allowlisted channel gets a dry-run reply in its thread
within 15 seconds. A post that is not a movement gets no reply. A post made
while the connector was down is found at restart and flagged as late. On
the corpus, both extractors have zero hard failures, the LLM extractor on
recorded outputs. Match accuracy and extraction scores are published in the
docs. Slack fixtures and signature tests pass in CI, for events and for
interactivity payloads. Two fixtures come from a real workspace capture.
The quickstart reaches the first dry-run in about 15 minutes.

## M2: review and clarification

- [x] Review items with explicit reasons, stored in the audit trail (every reason in each dry run's record, the question with its reason code; `stockthread trail` prints them for one post)
- [x] One focused question per post in the thread, with buttons for short candidate lists
  - [x] Which question: `core/questions.py` picks one per post by tier (design section 4, "Questions")
  - [x] The question in the thread, with buttons and a stored button row (in the dry-run reply; migration 0010)
  - [x] Stale buttons removed from Slack when a revision replaces the reply (and once a question is answered; migration 0012)
- [x] Answers by button or by thread reply update the candidate and re-run validation
  - [x] A click on a question's button is checked against its stored row and recorded as the answer
  - [x] The clicker is told why a click did nothing (stale, expired, not allowed): an ephemeral note in the thread
  - [x] The answer updates the candidate, re-runs validation, and asks the next question (by button)
  - [x] Which part, which place, how many and which serials are answered by a thread reply that reads cleanly (#85)
- [x] Authorization: who may answer and who may approve, by button and by thread reply
  - [x] Poster or approver, approvers only for approval questions, by button and by thread reply (#85)
  - [x] The "poster or approver only" note for a clean thread reply from someone else (#86)
- [x] Plan revisions: an edit, a deletion or a new thread message makes a plan stale
  - [x] A new message after the dry run gets a revised dry run, or a notice that the reply no longer stands
  - [x] An edit or a deletion after the dry run starts a revision by itself
  - [x] A revision reads an edited message's new text (and so does the first dry run; a deleted message drops out)
- [x] Source selection: choose concrete stock items for each line, serials included
  - [x] The pick rule: OK and unallocated items only, one covering item before a split, oldest first, serials, differing batch or supplier part asks (`core/resolve.py`)
  - [x] In the dry run: read each line's stock once its part, place and quantity are settled, show the pick, and raise its reasons
  - [x] Ask the stock question as a choice of stock items, yes or no to a split, or read the stock again (by button)
- [x] Plan expiry
  - [x] A question whose plan expired is taken out of its reply, says so, and takes no answer (migration 0014)
  - [x] One expiry time per post for its questions and its revisions (migration 0015, #74)
  - [x] An approved plan is never written past its expiry; one whose first write began is finished (`applier.work_request`, `applier.plan_expired`)
- [x] Setup check: users without a vehicle, token roles, locations that are missing (`stockthread check-setup`; users are those who have posted, from the local database, so no new Slack scope)
- [x] Admin guidance: how to model vehicles and sites as stock locations (`docs/locations.md`)

**DoD:** every corpus case with a review reason produces the expected
question. An answered question leads to a fully resolved dry-run plan with
concrete stock items. A button of a stale or expired plan does nothing and
says why. Button payloads are covered by recorded fixtures, including
clicks and thread answers by users who are not allowed to answer, and
payloads whose channel or message does not fit the stored id.

## M3: apply, verify, receipt and undo

- [x] Request builder for transfer, remove, add and count
  - [x] The request of one movement: path and body for each operation, checked strictly (`core/write_request.py`; accepted by both pinned versions in `tests/integration/test_built_requests.py`)
  - [x] A decided plan becomes its requests, one per movement, in the post's order (`plan.py`)
- [x] Write gate: switch, policy and deterministic validation must all pass, at approval and again before the write
  - [x] The judgement: blocked, waits for Apply, or allowed with its requests (`gate.py`)
  - [x] Judged at approval and again before the write, on the rebuilt views with the settings, vehicles, answers and plan state read then (`applier.approve_by_policy`, `applier.work_request`)
- [x] Apply protocol: writer lock, marker check, snapshot, single write, read-back
  - [x] Marker search: every attempt of one request, whole markers only (`InvenTreeReader.marker_entries`)
  - [x] Marker check: the markers found agree with the recorded states of every request of the post (`core/marker_check.py`, `Store.recorded_requests`)
  - [x] Marker check against InvenTree: search, and a missing marker excused only by every item's 404 (`apply.check_post_markers`)
  - [x] Precondition: the plan still holds against the stock read now, and the snapshot stored with the attempt (`precondition.check`)
  - [x] Intent: the stored body's notes lead with the attempt's marker (`Store.next_marker`, `Store.begin_attempt`)
  - [x] Single write: one request sent once, its outcome accepted, refused or unknown (`InvenTreeWriter.send`)
  - [x] Read-back, checks 1 and 2: the marker's entries carry the plan's deltas, and the totals are the snapshot plus the plan (`readback.verify`)
  - [x] Read-back, check 3: no entry without the marker on the named or created stock items since the snapshot (`InvenTreeReader.item_entries`, `Snapshot.history`)
  - [x] Apply orchestrator: writer lock, judge again, marker check, precondition, intent, send, read-back, state (`apply.apply_request`)
  - [x] Read back a request a read error left `applied` (`apply.readback_request`)
  - [x] The views a write acts on, rebuilt from the dry-run record the thread was shown, refused unless the plan was made from it ([0016](docs/decisions/0016-apply-reads-the-dry-run-record.md), `plan_record.views_of`)
  - [x] The plan of a reply that could apply in full is made with the reply, and any other outcome but `unchanged` leaves no plan current (`Store.finish_dry_run`, `plan_record.plan_of`)
  - [x] A message that joins a post, or a noted edit or deletion, stales its plan at once; a revision that reads the same makes it again
  - [x] The step for one request: write an `approved` one, read back an `applied` one, recover an `unknown` apply or undo (`applier.work_request`, `Store.requests_to_work`); a plan is approved whole (`Store.approve_plan`)
  - [x] A request waits, keeping its approval, while a sibling is due or in flight, and goes back to review when its plan stopped at an earlier request or the store refuses it for good (`Store.apply_waits_for`, `Store.plan_stopped_before`)
  - [x] Worker: run the step each request is due for, an outage pausing every step, a broken record an operator error (`Worker.work_due`; `Worker.apply` not given by `run.py` yet)
  - [x] The run loop gives the worker its apply step and undo when writes are on and the server is tested (`run.inventree_steps`)
  - [x] The run loop approves by policy: each plan judged once right after its reply, only the plan a post's first dry run made ([0019](docs/decisions/0019-policy-judges-the-first-plan-once.md); `Worker.approve_due`, `run.policy_approver`)
  - [x] The Apply button: an approver's click approves a plan that waits
    - [x] A click's approval: only an approver, only the current plan revision before its expiry, judged by the gate with the click (`applier.approve_by_click`)
    - [x] The button on a reply that made a plan, with writes on, and the worker's click handler, bound to the stored reply (`Worker._handle_apply`, `run.click_approver`)
    - [x] The button taken down once its plan is written, replaced, closed or expired, with a line that says which (`Worker.take_down_buttons`, migration 0022)
- [x] Unknown outcomes: quarantine, recovery, and new attempts only by a human
  - [x] Recovery of an unknown apply after the settle time: marker, then read-back or comparison with the snapshot (`apply.recover_request`)
  - [x] A crash in `applying` or `undoing` becomes unknown at start (`Store.conclude_crashed_writes`, run by `run.run` at start)
  - [x] Recovery of an unknown undo (`apply.recover_undo`, ADR 0018)
  - [x] The worker runs recovery (`Worker.work_due`)
- [x] Plans with several requests, and the `partially_applied` state: read from the request states, not stored; the receipt says "Partly applied" (`replies.receipt`; on a real server: `test_partial_plan_server.py`)
- [x] Receipt in the thread with the exact changes and a link to InvenTree
  - [x] The message: each movement's state, its exact changes, links to InvenTree and its marker (`replies.receipt`); due once no write of the plan is due or in flight and one concluded unreported, recorded with the states it reported (`Store.receipts_due`, `Store.receipt_posted`, migration 0019)
  - [x] Posted by the worker, with the Undo button; a receipt out of date by the time Slack returned is replaced (`Worker.post_receipts`)
- [x] Undo by button, authorized and idempotent, built from the observed history
  - [x] The undo request, built from the apply's history entries and refused when the stock moved on since (`inverse.reversals`, `check_undo`, `undo_request`)
  - [x] The undo attempt: store, send, read-back (`apply.undo_apply`, ADR 0017)
  - [x] Whether a person may ask for an undo now: state, `undo.max_age_hours`, earlier undos, poster or approver (`Store.undo_refusal`, `applier.undo_refusal`)
  - [x] The Undo button: on the receipt with a confirm dialog, checked against the newest receipt, the clicker and the store, then `undo_apply` (`replies.receipt`'s `undo`, `Worker._handle_undo`)
  - [x] A new receipt in the thread that reports an undo: due once the undo concluded, headed "Undone" or "Partly undone" (migration 0020: `receipt_state` takes `undone` and `undo_failed`; an undo forgets the reported state)
  - [x] Recovery of an unknown undo run by the worker (`Worker.work_due`)
- [x] Default policy ([0004](docs/decisions/0004-default-write-policy.md)): clear take-outs apply automatically, other movement types wait for "Apply", `policy.auto_apply` per movement type
  - [x] `policy.default_source`: a movement that states no source takes its type's default, which the policy counts while it is still the default ([0020](docs/decisions/0020-default-source.md))
- [x] Instance lock: one connector per database (`Store.acquire_lock`, heartbeat, `run.run`; `test_run.py`)
- [x] Alias storage, read into the matcher's alias rule; a plan that was confirmed, applied and verified stores its whole-word and fuzzy matches that fit one record as aliases (a pick among candidates teaches nothing, [0021](docs/decisions/0021-a-pick-teaches-nothing.md)), for parts and locations, with no further question ([0012](docs/decisions/0012-first-confirmation-becomes-alias.md)); undo forgets the alias it taught (0012 as built: it forgets rather than offers to)
  - [x] Storage (migration 0021, `Store.learn_alias` from a verified request only, `Store.aliases`), read into the run loop's catalog (`catalog.with_aliases`), a learned alias rechecked against new rivals (`matching._rechecked`)
  - [x] Learning: a verified plan an approver's click applied stores each text the whole-word and fuzzy rules fit to its one record (a pick among several teaches nothing while #174 is open); the receipt names them; an undo forgets them
    - [x] A clicked plan, once every request is verified, stores the poster's texts a person confirmed or corrected that fit their record alone (a pick among candidates teaches nothing, [0021](docs/decisions/0021-a-pick-teaches-nothing.md)); the receipt names them (`learning.lessons`, `Worker._learn`, `Store.aliases_taught_by`)
    - [x] An undo forgets the aliases its plan taught, as it begins (ADR 0012 as built; `Store.begin_attempt`)

**DoD:** integration tests against every pinned InvenTree version, with one
job on PostgreSQL, prove each line below.

- The same post applied 10 times, in parallel and across a restart, moves
  stock once.
- A response that arrives after the client timeout never leads to a second
  write, with or without a human retry.
- A stock change by another user between precondition and write is
  detected and reported.
- A plan of two requests whose second request fails ends as
  `partially_applied`, and undo restores the first.
- Undo restores the exact starting quantities. Undo after the stock moved
  on is refused. Two undo clicks move stock once. A crash during undo is
  recovered.
- Depletion with deletion, and serialized stock, are verified and undone
  correctly or go to review.
- The full suite with writes off records zero requests other than GET.
- A failed read-back is reported in Slack and blocks further writes for
  that post.
- A clear take-out applies without a click and gets a receipt with Undo.
  A return with no review reason still waits for "Apply". Stripping or
  varying every model output never turns a blocked movement into an
  automatic one.
- A take-out whose part was a whole-word match waits for "Apply" the first
  time and applies without a click the second time. A confirmed dry run, a
  rejected plan and an expired plan teach no alias. A learned alias is
  asked again when a new record fits its text.

## M4: alias learning

- [x] Alias admin: list, remove and export (`stockthread aliases`) (storage and learning are built in M3, [0012](docs/decisions/0012-first-confirmation-becomes-alias.md))
- [x] Learned aliases are covered by the corpus (corpus 0.3.0: `rivals` in `catalog/aliases.json`; `test_every_learned_alias_decides_a_scored_match`)
- [x] An admin adds an alias by hand (`stockthread aliases add`, [0021](docs/decisions/0021-a-pick-teaches-nothing.md))
- ~~Aliases with words like "my" are stored per user~~ dropped from v1.0 by [0021](docs/decisions/0021-a-pick-teaches-nothing.md): they would almost never fire
- [x] An alias is checked against InvenTree when it is used (the matcher ignores one whose record cannot be matched; `check-setup` reports it by id, the run log by record)

**DoD:** after a confirmed correction, the same wording matches without a
question. An unconfirmed or model-suggested alias never changes a match.
Removing an alias restores the earlier behavior. An alias that points to
an inactive part or a deleted location is ignored and reported.

## v1.0: hardening and docs

- [ ] Full documentation: README, quickstart, configuration reference, architecture, operations, troubleshooting (all six written, architecture on 2026-10-26; the quickstart's live step 7 waits on #182)
- [ ] Threat model, final version, and security review, including prompt injection through message text
- [ ] A 24-hour run on small hardware with forced disconnects (Socket Mode link faults tested end to end, #212; the soak harness `tests/soak/soak.py`, #213; the 24-hour run itself is next)
- [x] Upgrade and backup notes for the audit store ([operations](docs/operations.md))
- [x] Supported version matrix for InvenTree and Slack transports ([versions](docs/versions.md), checked against CI)
- [ ] Prepublication audit: full-history leak scan, license headers, docs check, private vulnerability reporting on
- [x] Release process, without publishing until Dan says so ([releasing](docs/releasing.md); versions and changelog checked by `tests/test_release.py`; where to publish is Dan's)
- [x] Any unit but a count (feet, metres, a box) asks for the quantity in the part's stocking unit, and no request
  is built for a line that still has a unit (#151)
- [x] Read each part's InvenTree unit; the quantity question names it ("in m"), or pieces when the part has
  none, for a part matched surely enough to write
- [x] A length in the part's own unit ("50 m" of a part stocked in metres) needs no question; the part's units are
  read again right before the write
- [x] Retention: stored message text is blanked `storage.retention_days` after a post is done with (T10, #153)

**DoD:** a fresh admin completes the quickstart in about 15 minutes without
help. No issue labeled `agent-backlog` with high severity is open. Dan has
approved the prepublication audit.

## After v1.0 (not planned in detail)

- Photo extraction with a vision model, beyond the caption
- Barcode and QR confirmation for serials
- More chat platforms behind the same core
