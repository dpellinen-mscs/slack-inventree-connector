# Threat model

It lists what can go wrong, which control answers it, and where the tests
that prove the control live. The controls themselves are described in
[`design.md`](design.md) sections 4 to 8. This revision (October 2026)
checks every row against the code, and the security review below closed
the v1.0 item.

Status: **built** means the code exists and has tests on `main`, or in the
open PR named. **Partly built** says which part is missing. **Open** means
nobody has an answer yet.

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
| T1 | Forged Slack events or button clicks over HTTP | The signature check is always on, with no switch. HTTP mode refuses to start without a signing secret. Stale timestamps are rejected, and so is one that is not plain digits (a 401, never a 500 whose traceback quotes it). Bodies over 4 MiB are refused before they are read, and a refused request is not parsed or logged. | Built: `slack/http_server.py`; test vectors and fixtures (`tests/fixtures/slack/`), through Bolt and over a real socket (`tests/test_slack_http.py`, `tests/test_slack_listener.py`) |
| T2 | Events from another workspace, app or channel | Workspace id, app id and the poster's or clicker's organization are checked on every payload. Channels are allowlisted, and an empty allowlist means the connector acts nowhere. | Built: `slack/ingest.py`; `tests/test_slack_ingest.py` |
| T3 | Replayed or retried deliveries cause two writes | Deduplication on `event_id` and on message key. The request key and its state are enforced by unique constraints and compare-and-set. | Built: event queue and `Store.begin_attempt`; `tests/store/test_event_queue.py`, `tests/store/test_store.py` (racing stores), `tests/test_worker_undo.py` (two undo clicks) |
| T4 | Someone who is not an approver approves, undoes or answers | Approvers are listed by Slack user id, and display names are never trusted. An approver-only click or answer counts only if its author is an approver when it is applied. A question button holds a random id of 128 bits. The Undo button holds the write's marker (110 random bits). The Apply button holds only the plan revision, and counts only on the stored dry-run reply of that revision. Every click is checked against its stored channel and message. | Built: `ingest._action`, `applier`, `gate.judge`, `core/answers.py`; `tests/test_worker_apply_click.py`, `tests/test_worker_undo.py`, `tests/test_gate.py`. An empty approver list stops every Apply click and approver answer (the poster may still Undo), not the policy: see T17 |
| T5 | A reaction or bot post triggers a write | Undo is a button, not a reaction, and the app asks for no reaction scope. Every message with a bot id, bot profile, app id or the `bot_message` subtype is dropped, edits included. | Built: `ingest._is_bot`; `tests/test_slack_ingest.py`, `tests/test_manifest.py` |
| T6 | Prompt injection makes the model steer a write | The model only proposes a candidate. Review reasons come from rules, and the model can only add them. Items, quantities and places must appear in the text (`ITEM_NOT_IN_TEXT`, `QUANTITY_NOT_IN_TEXT`, `LOCATION_NOT_IN_TEXT`). Matching is deterministic, and the prompt has no catalog data, ids or names. The rules raise the `suspicious_instructions` flag whatever the model says. The corpus has injection cases, and any deviation on them is a hard failure. A reading that holds another person's message (a reply in someone's thread) waits for a person, since its "my truck" is read as the poster's. The aim: injection gains nothing a poster could not type, as the same text, posted plainly, passes the same checks. A take-out whose text has a return word needs a person (#199). A movement whose fields no single clause holds, where one of its clauses names another place on the same side or a number it does not use, needs a person: a model cannot mix the fields of two stated movements into one nobody typed (#200). A clause that names another vehicle, job or site is a rival with no "from"/"to" word or number ("van 4 got the drops", "the Smith job got the drops"); "for the Smith job" says why and is none. Known gap: a rival place named by neither kind of word ("the Riverside Apartments got the drops"). | Partly built: `core/validate.py`, `llm/extract.py`, `eval/tools/score.py`; `tests/test_gate_model_output.py` (every recorded post, made-up output never approved), `tests/core/test_validate.py`, `tests/llm/test_recorded_corpus.py`, `tests/test_worker.py` (another person's thread reply). `tests/core/test_validate.py`, `tests/test_gate_model_output.py` (every recorded return read as a take-out; one take mixed from two sibling messages). A take to a site needs a destination under `locations.sites` for the policy (ADR 0022), so a return read as one waits. |
| T7 | Model confidence, or a missing flag, authorizes a write | The write gate has three parts: switch, policy and validation. The output schema has no confidence, so nothing can vary it. A pipeline test strips the model's flags and makes up its fields on every recorded post, and checks that "blocked" never becomes "allowed". | Built: `gate.py`; `tests/test_gate_model_output.py`, `tests/test_gate.py` |
| T8 | Writes happen before the operator means them to | `writes.enabled` defaults to false. With writes off, or on an untested server, the worker gets no apply, approve or undo step, and the writing client is never opened. The read client rejects every method except GET at the transport level. The writer lets out only a marked POST to a stock path of the server. | Built: `run.py`, `inventree.py`, `writer.py`; `tests/test_run.py`, `tests/test_inventree.py`, `tests/test_writer.py`, `tests/integration/test_writes_off_server.py` |
| T9 | The InvenTree token leaks through a redirect, a log or an error | The client never follows redirects, reads only paths below `/api/`, and never repeats response bodies or tokens in errors. Secrets are refused in the config file and printed as `***`. | Built: `tests/test_inventree.py`, `tests/test_config.py`, `tests/test_writer.py` |
| T10 | Message content leaks to logs, InvenTree or the audit log | Logs and `audit_log` hold ids and codes only. InvenTree notes hold the marker, a message link and a user id. Retention blanks text. The database file is created mode 0600. Text from Slack is escaped before it reaches a terminal (`trail`, `aliases`). | Built: `tests/core/test_write_request.py`, `tests/store/test_store.py`, `tests/store/test_retention.py`, `tests/test_trail.py`, `tests/test_printable.py` |
| T11 | Message content leaks to the LLM provider | The docs say plainly that text leaves the machine. The rule-based extractor reads every post first, and only the posts it declines go to a model. With no `[llm]` section nothing leaves the machine, and a local model on this machine needs no key. Before any call, strings that look like secrets (tokens, keys, JWTs, private keys, webhook URLs, URL passwords, phone numbers, the word after a secret label) are masked; this is a second layer, since no pattern finds every secret ("the shop wifi password for today is X" passes) and mentions lose their ids. The model's answer is capped (20,000 characters, movement and line counts, number sizes) and parsed strictly. | Built: `llm/extract.py` (`mask_secrets`, `parse_output`), `llm/openai_compatible.py`; `tests/llm/test_extract.py`, `tests/llm/test_openai_compatible.py` |
| T12 | A write happened, but the connector believes it did not (or the reverse) and writes again | Unknown outcomes (every 5xx, timeouts, non-InvenTree bodies) are never sent again by the connector ([ADR 0015](decisions/0015-unknown-writes-are-never-retried.md)). Recovery waits a settle time, then checks the marker and the quantities. A marker that is not found proves nothing: measured, a 500 on 1.5.6 with PostgreSQL can follow a committed change that has no history entry. | Built: `apply.recover_request`, `apply.recover_undo`; `tests/test_apply_request.py`, `tests/test_worker_apply.py`, characterization `tests/integration/` |
| T13 | A concurrent writer in InvenTree changes stock during an apply | A snapshot and precondition check before the write. Read-back after it checks the history deltas, the quantities, and that no other writer left an entry on the touched stock items, so any of these ends in `verify_failed`. Measured: InvenTree itself can move less than asked, or (1.4.3 with PostgreSQL) create stock under concurrency. A writer that touches none of the connector's items is not detected. | Built: `precondition.py`, `readback.py`; `tests/test_precondition.py`, `tests/test_readback.py`, `tests/integration/test_readback_server.py` |
| T14 | Two connector instances write at once | A single-instance lock with a heartbeat, checked in the same transaction that starts a write. It cannot cover two instances on different databases, and the docs warn against that setup. | Built: `tests/store/test_store.py` |
| T15 | Someone tampers with the audit trail or idempotency keys, or a bug does | Triggers reject updates and deletes of `audit_log`, changes to key columns, `REPLACE` collisions and changes to the recorded intent of a write. | Built: `tests/store/test_store.py`, `tests/store/test_event_queue.py`, `tests/store/test_aliases.py` |
| T16 | Undo does more harm than the original write | Undo reverses what the stock history shows the write did, not what the plan said, only if nothing else touched that stock since, through the same protocol (marker, snapshot, one write, read-back). Only the poster or an approver may ask, within `undo.max_age_hours` by the store's clock, after a Slack confirm dialog. Otherwise it refuses, changes nothing, and tells the person who clicked why ("undo it in InvenTree" past the age limit). | Built: `apply.undo_apply`, `inverse.py`, `applier.undo_refusal`; `tests/test_undo_apply.py`, `tests/test_inverse.py`, `tests/test_worker_undo.py` |
| T17 | Unsafe configuration: writes on with no approvers, broad policy, wrong server | Unknown keys, wrong types and secrets in the file are refused. `check-config` prints the effective settings; `run` logs whether writes are on and where text goes. The API version is checked, and writes stay off outside the tested range: the gate blocks every plan on an untested server. `check-setup` warns when the InvenTree token would go over plain http to another machine (an LLM key over plain http is refused outright). | Built: config (#10), version check (#16) in the gate (#106); `check-setup` warns, with writes on, when `slack.approvers` is empty (the policy still applies clear take-outs) or `policy.auto_apply` goes beyond the take-outs (`setup_check.check_policy`; `tests/test_setup_check.py`) |
| T18 | The policy applies a write nobody clicked | Only movement types in `policy.auto_apply` (default: the two take-outs), with no review reason, no model reason, no person's answer, every part and place found by the exact, normalized or alias rule, and every vehicle the poster's own or named exactly. A take to a site also needs its destination under `locations.sites` ([ADR 0022](decisions/0022-sites-for-take-to-site.md)). The policy judges a post's first plan once ([ADR 0019](decisions/0019-policy-judges-the-first-plan-once.md)). | Built: `gate._not_automatic`, `applier.approve_by_policy`; `tests/test_gate.py`, `tests/test_worker_approve.py` |
| T19 | A default source moves stock from the wrong place | `policy.default_source` settles only a source the text does not state. A text that names one ("from", "off", "out of", "outta", "source:", "at" a storage word, any typed or symbol arrow; punctuation or a digit may follow "from") while the reading has none never takes the default, so a model cannot pick the source by leaving it out. Known gaps: a source with no marker word ("pulled 2 ONTs shelf B to van 3", "van 3 gave me 2"), a typo of "from" that is a word ("form"), and "at" a vehicle, which may be either side. | Built (ADR 0020): `core/validate.py` (`DETAIL_SOURCE_LEFT_OUT`), `catalog.assign_default_sources`, `gate.py` (`GateNow.default_sources`); `tests/core/test_validate.py`, `tests/test_gate.py`, `tests/test_worker.py`, `tests/test_gate_model_output.py` (every recorded source left out) |
| T20 | An alias makes a wrong match authorize writes with no click | An alias is learned only from a plan an approver confirmed and InvenTree read back as written ([ADR 0012](decisions/0012-first-confirmation-becomes-alias.md)), and a later rival makes it ask again. Edits and undos forget what a plan taught. Policy approvals, dry runs and model text teach nothing. An alias whose record cannot hold stock is ignored and reported. Admins list, export and remove aliases. An alias an admin types (`aliases add`) is shown with its record and every rival before `--yes` stores it. The audit log keeps each alias's record also after it is removed. | Built: `learning.py`, `Store.learn_alias`; `tests/test_worker_learn.py`, `tests/store/test_aliases.py`, `tests/test_learning.py`. `aliases add` (ADR 0021) and `aliases history`: `__main__.py`, `Store.add_alias`, `Store.alias_events`; `tests/store/test_aliases.py` |
| T21 | A click on an old Apply button writes a plan nobody saw | The Apply button has a Slack confirm dialog. It counts only on the reply of the plan it shows, only while later dry runs are unchanged and the plan hash matches, and only if the reply showed every line. It is taken down once a decision is made, the plan expires, or a write begins. | Built: `Worker._handle_apply`, `slack/replies.py`; `tests/test_worker_apply_click.py` |
| T22 | Someone acts for another person, or a post is acted on long after it was made | A post that names someone else as the actor, or mentions a person, needs an approver (`ACTOR_NOT_POSTER`), whatever the model says: the rules see any name in any alphabet before a verb, "has taken", "taken by Mike" and "Mike and I". A name the rules cannot tell from other words ("Mike's van got 2") still rests on the model's flag. A post found by catch-up after downtime is marked late and needs an approver. Plans expire after `slack.plan_expiry_hours`. | Built: `core/validate.py`, `slack/catchup.py`; `tests/core/test_validate.py`, `tests/test_slack_catchup.py`, `tests/test_worker.py` |

## Security review (October 2026)

Every row was traced against the code by reviewers with no prior context,
in four passes on 2026-10-27. What they found and what changed:

- T1: a timestamp header that is not a number gave a 500 with the header
  in the log (#232).
- T22 (major): actor wordings the rules missed, such as "José took" and
  "taken by Mike" (#234).
- T17: a warning for a token sent over plain http to another host (#235).
- T11: more credential shapes masked before text goes to a provider (#236).
- T6: a rival vehicle (#231), job or site (#237) in a mixed movement.
- T19 (major): source wordings the check missed, typed arrows as Slack
  sends them, and a slow pattern on long input (#238).
- T2, T3, T4, T5, T7, T8, T9, T10, T12 to T16, T18, T20, T21: no finding.
  T2 accepts a user's message with no `user_team` or `team` field. The
  reviewers found no Slack delivery from a person without one, so this
  is noted, not changed, until a recorded event shows otherwise.

The known gaps left are named in rows T6, T19 and T22.

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
- [`SECURITY.md`](../SECURITY.md) names GitHub's private vulnerability
  reporting as the channel. It has to be turned on in the repository's
  settings when the project goes public; until then nobody outside can
  report.
- Photo handling: what is stored, for how long, and what is sent to a
  vision model. Out of scope for v1.0 text-only extraction.
