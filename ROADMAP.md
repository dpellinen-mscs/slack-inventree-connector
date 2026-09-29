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
4. At most one movement is applied per Slack post, across retries and restarts.
5. Every write is read back and verified.
6. Every applied movement can be undone.

## Phase 0: research and design

- [x] Research notes: InvenTree, Slack, prior art and stack (`docs/research/`)
- [x] Design document (`docs/design.md`)
- [x] Roadmap (this file)
- [x] Eval corpus v0.1 with format, schema and validator (`eval/`)
- [x] Lean CI for the corpus
- [ ] First decision bundle answered: shape and stack, default policy, name and license
- [ ] Docker works on the build machine

## M0: foundations

Blocked on the shape and stack decision.

- [ ] Project scaffold: package layout, formatter, linter, type checker, test runner
- [ ] CI: format, lint, types and unit tests on every PR, with dependency caching
- [ ] Configuration: typed settings from environment and file, validated at startup, secrets redacted in every log and error
- [ ] Domain model: post, candidate movement, line item, match result, review reason, decision
- [ ] Audit store: schema, migrations, append-only event log, idempotency key table
- [ ] InvenTree test harness: pinned versions in Docker, seeded from `eval/catalog/`, torn down after each run
- [ ] InvenTree client: read parts, locations and stock items, with API version check at startup
- [ ] `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md`

**DoD:** a contributor can clone, install and run the full gate with one
documented command. Integration tests read the seeded catalog from a real
InvenTree in CI. No code path can write stock yet.

## M1: read-only extraction and matching, dry-run in Slack

- [ ] Slack ingest over Socket Mode and HTTP, with signature verification on HTTP
- [ ] Channel allowlist, bot and edit filtering, event deduplication
- [ ] Grouping: photo with caption, sibling messages and threads become one post
- [ ] Rule-based extractor that needs no LLM
- [ ] LLM extractor behind a provider interface, with a strict output schema
- [ ] Eval runner: scores either extractor against the corpus, using recorded fixtures in CI
- [ ] Validator: deterministic contradiction checks and text-level review reasons
- [ ] Matcher: exact, normalized, alias, then fuzzy candidates, for parts and locations
- [ ] Dry-run reply in the Slack thread: what would change, and every review reason
- [ ] Slack app manifests and the quickstart

**DoD:** a post in an allowlisted channel gets a dry-run reply in its thread
within 10 seconds. On the corpus, the rule-based extractor has zero false
movements and zero missed safety flags. Match accuracy and extraction scores
are published in the docs. Slack fixtures and signature tests pass in CI. The
quickstart reaches the first dry-run in about 15 minutes.

## M2: review and clarification

- [ ] Review items with explicit reasons, stored in the audit trail
- [ ] One focused question per post in the thread, with buttons for short candidate lists
- [ ] Answers by button or by thread reply update the candidate and re-run validation
- [ ] Authorization: who may answer and who may approve
- [ ] Source selection: choose concrete stock items for each line, serials included
- [ ] Timeouts and reminders for unanswered questions
- [ ] Admin guidance: how to model vehicles and sites as stock locations

**DoD:** every corpus case with a review reason produces the expected
question. An answered question leads to a fully resolved dry-run plan with
concrete stock items. Button payloads are covered by recorded fixtures,
including clicks by users who are not allowed to answer.

## M3: apply, verify, receipt and undo

- [ ] Request builder for transfer, remove, add and count
- [ ] Write gate: global switch, policy and deterministic validation must all pass
- [ ] Idempotent apply keyed on the Slack post, safe across retries, restarts and concurrent workers
- [ ] Read-back verification after every write, with a clear failure state
- [ ] Receipt in the thread with the exact changes and a link to InvenTree
- [ ] Undo by button, authorized and idempotent, with its own read-back
- [ ] Crash recovery: a movement interrupted mid-apply is reconciled at startup

**DoD:** integration tests against every pinned InvenTree version prove that
the same post applied 10 times, in parallel and across a restart, moves stock
once. Undo restores the exact starting quantities. With writes off, no test
can produce a write request. A failed read-back is reported in Slack and
blocks further writes for that post.

## M4: alias learning

- [ ] Confirmed corrections become alias proposals
- [ ] Only a confirmation by an authorized human creates an alias
- [ ] Alias admin: list, remove and export
- [ ] Aliases feed the matcher and are covered by the corpus

**DoD:** after a confirmed correction, the same wording matches without a
question. An unconfirmed or model-suggested alias never changes a match.
Removing an alias restores the earlier behavior.

## v1.0: hardening and docs

- [ ] Full documentation: README, quickstart, configuration reference, architecture, operations, troubleshooting
- [ ] Threat model and security review, including prompt injection through message text and photos
- [ ] Load and soak test on small hardware
- [ ] Upgrade and backup notes for the audit store
- [ ] Supported version matrix for InvenTree and Slack transports
- [ ] Prepublication audit: full-history leak scan, license headers, docs check
- [ ] Release process, without publishing until Dan says so

**DoD:** a fresh admin completes the quickstart in about 15 minutes without
help. The weekly audit has no open high-severity finding. Dan has approved
the prepublication audit.

## After v1.0 (not planned in detail)

- Photo extraction with a vision model, beyond the caption
- Barcode and QR confirmation for serials
- More chat platforms behind the same core
