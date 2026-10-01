# Threat model

First version, written during M0. It lists what can go wrong, which
control answers it, and whether that control exists yet. The final version
and a security review are v1.0 work (ROADMAP). The controls themselves are
described in [`design.md`](design.md) sections 4 to 8.

Status: **built** means the code exists and has tests on `main` or in an
open PR. **Designed** means it is specified in the design and not built.
**Open** means nobody has an answer yet.

## What we protect

1. **Stock records in InvenTree.** A wrong write is worse than no write.
2. **Secrets:** the InvenTree API token, the Slack tokens and signing
   secret, and LLM provider keys.
3. **Message content.** Slack posts and photos can contain customer names,
   addresses and other personal data.
4. **The audit trail.** The operator must be able to trust it to explain
   what the connector saw, decided and sent.

## Trust boundaries

```
Slack (workspace, users, bots) ──► connector ──► InvenTree REST API
                                      │  ▲
                        LLM provider ◄┘  └─ SQLite audit store (local file)
```

- Everything that arrives from Slack is untrusted, including text from
  users who are allowed to post.
- The LLM's output is untrusted. It is treated like more user input.
- InvenTree is trusted to tell the truth about its own state, but its
  answers can be late, partial or wrong about whether a write happened
  (see T12).
- The host that runs the connector, and its administrator, are trusted.

## Who might cause harm

- **A forger on the network** who sends requests to the HTTP endpoint.
- **A channel member**, careless or malicious, who posts misleading text
  or clicks buttons.
- **Someone who writes prompt injection** into a message, directly or by
  pasting text from elsewhere.
- **The LLM provider**, which receives message text.
- **Another InvenTree user or integration** that changes stock at the same
  time as the connector.
- **The operator**, through configuration mistakes.

## Threats and controls

| ID | Threat | Control | Status |
| --- | --- | --- | --- |
| T1 | Forged Slack events or button clicks over HTTP | The signature check is always on, with no switch. HTTP mode refuses to start without a signing secret. Stale timestamps are rejected. Bodies over 4 MiB are refused before they are read, and a refused request is not parsed or logged. | Built: test vectors and fixtures (`tests/fixtures/slack/`), through Bolt and over a real socket (`tests/test_slack_http.py`) |
| T2 | Events from another workspace, app or channel | Workspace id and app id are checked on every payload. Channels are allowlisted, and an empty allowlist means the connector acts nowhere. | Designed. Fixtures are built |
| T3 | Replayed or retried deliveries cause two writes | Deduplication on `event_id` and on message key. The request key and its state are enforced by unique constraints and compare-and-set. | Store built (#21). Ingest designed |
| T4 | Someone who is not an approver approves, undoes or answers | Approvers are listed by Slack user id and display names are never trusted. An empty list means nobody can approve. A button holds only a random id of 128 bits or more, checked against its stored channel and message. | Designed |
| T5 | A reaction or bot post triggers a write | Undo is a button, not a reaction. Every message with a `bot_id` is dropped. | Designed. Fixtures are built |
| T6 | Prompt injection makes the model steer a write | The model only proposes a candidate. Review reasons come from rules and the model can only add them. Items and quantities must appear in the text. Matching is deterministic, and the prompt has no catalog data. The corpus has injection cases, and any deviation on them is a hard failure. | Corpus and scorer built. Pipeline designed |
| T7 | Model confidence, or a missing flag, authorizes a write | The write gate has three parts: switch, policy and validation. Confidence is not an input. A pipeline test varies scores and strips flags and checks that "blocked" never becomes "allowed". | Designed. Request states built |
| T8 | Writes happen before the operator means them to | `writes.enabled` defaults to false. With writes off, the writing client does not exist in the process. The read client rejects every method except GET at the transport level. | Read client built (#16). Gate designed |
| T9 | The InvenTree token leaks through a redirect, a log or an error | The client never follows redirects, reads only paths below `/api/`, and never repeats response bodies or tokens in errors. Secrets are refused in the config file and printed as `***`. | Built (#10, #15, #16) |
| T10 | Message content leaks to logs, InvenTree or the audit log | Logs and `audit_log` hold ids only. InvenTree notes hold the marker, a message link and a user id. Retention blanks text. | `audit_log` built (#21). The rest is designed |
| T11 | Message content leaks to the LLM provider | The docs say plainly that text leaves the machine. Strings that look like secrets are masked before remote calls. A local model and a rule-based extractor are supported. | Designed |
| T12 | A write happened, but the connector believes it did not (or the reverse) and writes again | Unknown outcomes (every 5xx, timeouts, non-InvenTree bodies) are never retried without a human. Recovery waits a settle time, then checks the marker and the quantities. A marker that is not found proves nothing: measured, a 500 on 1.5.6 with PostgreSQL can follow a committed change that has no history entry. | Characterized (#22, #23). Store guards built (#21). Recovery designed |
| T13 | A concurrent writer in InvenTree changes stock during an apply | A snapshot and precondition check before the write. Read-back after it checks the history deltas, the quantities, and that no other writer left an entry on the touched stock items, so any of these ends in `verify_failed`. Measured: InvenTree itself can move less than asked, or (1.4.3 with PostgreSQL) create stock under concurrency. A writer that touches none of the connector's items is not detected. | Characterized (#23). Protocol designed |
| T14 | Two connector instances write at once | A single-instance lock with a heartbeat, checked in the same transaction that starts a write. It cannot cover two instances on different databases, and the docs warn against that setup. | Built (#21) |
| T15 | Someone tampers with the audit trail or idempotency keys, or a bug does | Triggers reject updates and deletes of `audit_log`, changes to key columns, `REPLACE` collisions and changes to the recorded intent of a write. | Built (#21) |
| T16 | Undo does more harm than the original write | Undo reverses what was observed, only if nothing else touched the stock since, and it goes through the same protocol. Otherwise it refuses and becomes a review item. | Designed |
| T17 | Unsafe configuration: writes on with no approvers, broad policy, wrong server | Unknown keys, wrong types and secrets in the file are refused. Startup prints the effective settings and whether writes are on. The API version is checked, and writes stay off outside the tested range. | Config built (#10). Version check built (#16) |

## Out of scope

- An attacker with shell or file access on the connector host. That person
  can read the tokens and the database. Protect the host and the file
  permissions.
- A compromised Slack workspace admin or InvenTree superuser.
- Denial of service against the connector. It fails closed: nothing is
  written when it is down, and catch-up marks late posts for review.

## Open questions

- InvenTree roles: the smallest set is measured (view on Part and Stock
  Location, view and add on Stock Item), but InvenTree grants "change" on
  Stock Item together with "add", so the connector's token can also edit
  stock item fields. That is broader than the connector needs, and only
  the client's own path and method checks narrow it.
- A security contact and disclosure process for `SECURITY.md`. This
  depends on how and when the project goes public.
- Photo handling: what is stored, for how long, and what is sent to a
  vision model. Out of scope for v1.0 text-only extraction.
