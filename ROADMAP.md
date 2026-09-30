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
- [ ] Domain model: post, plan revision with input hash, request, line item, match result, review reason, decision
- [x] Audit store: schema, migrations, append-only log, request keys, marker and snapshot columns ([0009](docs/decisions/0009-audit-store.md))
- [x] InvenTree test harness: pinned versions in Docker, seeded from `eval/catalog/`, torn down after each run, with one PostgreSQL job
- [ ] Characterization tests for every InvenTree behavior the design relies on (design section 11)
- [x] Characterization of a transfer with the server-wide "merge stock on transfer" setting on
- [x] Fault proxy for delayed, dropped and failed responses (`tests/support/faultproxy.py`)
- [x] InvenTree client: read-only, rejects every method except GET, with API version check at startup ([0007](docs/decisions/0007-inventree-read-client.md))
- [x] Threat model, first version ([`docs/threat-model.md`](docs/threat-model.md))
- [ ] `SECURITY.md` (needs a contact, see the threat model), `CONTRIBUTING.md` (done), `CHANGELOG.md` (done)

**DoD:** a contributor can clone, install and run the full gate with one
documented command. Integration tests read the seeded catalog from a real
InvenTree in CI. The characterization tests pass on every pinned version,
and each failure is reflected in the design. No code path in the package
can write stock yet: only the test suite has a raw HTTP helper.

## M1: read-only extraction and matching, dry-run in Slack

- [ ] Slack ingest over Socket Mode and HTTP, with signature verification on HTTP (listener done: `slack/listener.py` checks, stores, then acknowledges; the worker is `worker.py`; `stockthread run` serves Socket Mode; HTTP serving is not built yet)
- [x] Workspace and app id checks, channel allowlist, subtype allowlist, bot filtering, deduplication (`slack/ingest.py`; in-memory dedup until the durable queue)
- [ ] Durable event queue (store side done: `slack_events`, migration 0002), and catch-up from channel history after downtime
- [x] Grouping: photo with caption, sibling messages and threads become one post (`core/grouping.py`, migration 0003; the worker places each message)
- [x] Rule-based extractor that needs no LLM (`core/extract_rules.py`; one message per post for now, declines the rest)
- [ ] LLM extractor behind a provider interface, with a strict output schema (`llm/extract.py`: prompt `llm-0.1`, strict parser, secret masking; `llm/openai_compatible.py`; not yet configured, wired into the worker or scored on a model)
- [ ] Eval runner: scores either extractor against the corpus, using recorded fixtures in CI
- [x] Validator: deterministic contradiction checks and text-level review reasons (`core/validate.py`)
- [x] Matcher: exact, normalized, alias, then fuzzy candidates, for parts and locations (`core/matching.py`; the catalog snapshot from InvenTree comes with Resolve)
- [ ] Dry-run reply in the Slack thread: what would change, and every review reason (renderer `slack/replies.py`; `worker.py` posts it once the sibling window has passed; `stockthread run` serves it; not yet tried in a real workspace)
- [ ] Slack app manifests and the quickstart (Socket Mode manifest `manifests/slack-socket.json` and the README quickstart done; install and `check-config` followed from scratch; not yet followed end to end in a real workspace; the HTTP manifest comes with HTTP serving)

**DoD:** a post in an allowlisted channel gets a dry-run reply in its thread
within 15 seconds. A post that is not a movement gets no reply. A post made
while the connector was down is found at restart and flagged as late. On
the corpus, both extractors have zero hard failures, the LLM extractor on
recorded outputs. Match accuracy and extraction scores are published in the
docs. Slack fixtures and signature tests pass in CI, for events and for
interactivity payloads. Two fixtures come from a real workspace capture.
The quickstart reaches the first dry-run in about 15 minutes.

## M2: review and clarification

- [ ] Review items with explicit reasons, stored in the audit trail
- [ ] One focused question per post in the thread, with buttons for short candidate lists
- [ ] Answers by button or by thread reply update the candidate and re-run validation
- [ ] Authorization: who may answer and who may approve, by button and by thread reply
- [ ] Plan revisions: an edit, a deletion or a new thread message makes a plan stale
- [ ] Source selection: choose concrete stock items for each line, serials included
- [ ] Plan expiry
- [ ] Setup check: users without a vehicle, token roles, locations that are missing
- [ ] Admin guidance: how to model vehicles and sites as stock locations

**DoD:** every corpus case with a review reason produces the expected
question. An answered question leads to a fully resolved dry-run plan with
concrete stock items. A button of a stale or expired plan does nothing and
says why. Button payloads are covered by recorded fixtures, including
clicks and thread answers by users who are not allowed to answer, and
payloads whose channel or message does not fit the stored id.

## M3: apply, verify, receipt and undo

- [ ] Request builder for transfer, remove, add and count
- [ ] Write gate: switch, policy and deterministic validation must all pass, at approval and again before the write
- [ ] Apply protocol: writer lock, marker check, snapshot, single write, read-back
- [ ] Unknown outcomes: quarantine, recovery, and new attempts only by a human
- [ ] Plans with several requests, and the `partially_applied` state
- [ ] Receipt in the thread with the exact changes and a link to InvenTree
- [ ] Undo by button, authorized and idempotent, built from the observed history
- [ ] Default policy ([0004](docs/decisions/0004-default-write-policy.md)): clear take-outs apply automatically, other movement types wait for "Apply", `policy.auto_apply` per movement type
- [ ] Instance lock: one connector per database

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

## M4: alias learning

- [ ] Confirmed corrections become alias proposals
- [ ] Only a confirmation by an authorized human creates an alias
- [ ] Alias admin: list, remove and export
- [ ] Aliases feed the matcher and are covered by the corpus
- [ ] Aliases with words like "my" are stored per user
- [ ] An alias is checked against InvenTree when it is used

**DoD:** after a confirmed correction, the same wording matches without a
question. An unconfirmed or model-suggested alias never changes a match.
Removing an alias restores the earlier behavior. An alias that points to
an inactive part or a deleted location is ignored and reported.

## v1.0: hardening and docs

- [ ] Full documentation: README, quickstart, configuration reference, architecture, operations, troubleshooting
- [ ] Threat model, final version, and security review, including prompt injection through message text
- [ ] A 24-hour run on small hardware with forced disconnects
- [ ] Upgrade and backup notes for the audit store
- [ ] Supported version matrix for InvenTree and Slack transports
- [ ] Prepublication audit: full-history leak scan, license headers, docs check
- [ ] Release process, without publishing until Dan says so

**DoD:** a fresh admin completes the quickstart in about 15 minutes without
help. No issue labeled `agent-backlog` with high severity is open. Dan has
approved the prepublication audit.

## After v1.0 (not planned in detail)

- Photo extraction with a vision model, beyond the caption
- Barcode and QR confirmation for serials
- More chat platforms behind the same core
