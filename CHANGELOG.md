# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions will
follow [Semantic Versioning](https://semver.org/) from the first release.

## [Unreleased]

Nothing is released yet. With `writes.enabled = true`, the connector writes clear movements of the types in
`policy.auto_apply` with no click (by default take-outs to a vehicle or a site), and any other movement the gate
allows once an approver clicks Apply on its dry-run reply (by default returns, receipts, counts and the rest). A
reply too long to show in full carries no Apply button. An approved plan is never written once it has expired
(`slack.plan_expiry_hours`): it goes back to review; a plan whose first write began is finished.

### Changed

- A length in its part's own unit ("50 m of drop cable" for a part stocked in m, "50 ft" for one in ft) asks no
  quantity question and may apply, for a part matched surely enough to write. Units are never converted: cm, mm,
  inches, any other unit, containers and anything that may multiply the length ("3 runs of 50 m", "2x 50 m",
  "twice") still ask. The part's units are read from InvenTree again right before
  the write, and a part stocked in another unit since the dry run goes back to review unwritten.

### Fixed

- `stockthread trail` prints every character of Slack text that is not printable (control characters, DEL, C1
  codes, bidi overrides) escaped as `\uXXXX` (a surrogate pair above U+FFFF), so a post or answer cannot move the
  cursor or reorder the terminal line.
- A consume of serialized stock no longer reads "could apply": InvenTree answers a remove on a serialized item with
  success and changes nothing, so the dry run shows the items and a review reason instead.
- A question and the revisions of its post now expire at the same time, fixed by the post's first dry run
  (migration 0015). Before, an answer given late to a thread whose root was older than its newest message was
  recorded but never acted on.
- An answer taken just before its plan expired, whose revision ran after it, was shown as answered and then
  nothing came of it. That revision now posts one notice that the answer came too late (issue #75).

### Added

- Retention (design section 6, threat model T10): `storage.retention_days` (30) after a post is done with,
  the worker blanks its stored message text, the raw Slack events and dry-run results, keeping the rows, ids,
  states and audit log. Text a write, a read-back or an undo may still need is kept. Each expiry is audited.
- Confirmed aliases are stored (migration 0021, `Store.learn_alias`, only from a verified request) and the run loop
  matches with the aliases stored now (`catalog.with_aliases`). A learned alias is asked again when a record its text
  did not fit when it was confirmed fits now ("Disc 40" once a "Disc 40 Pro" exists; ADR 0012). Nothing learns an
  alias yet.
- `stockthread check-setup` warns about each stored alias whose part is inactive, deleted, a template or virtual, or
  whose location is structural or gone, by id: the matcher ignores it, and `aliases remove <id>` forgets it.
  The run log names their records once each time that list changes.
- `stockthread aliases list | export | remove <id>`: the stored aliases with the post each was learned from, as text
  or JSON, and their removal (audited as the admin's; the matcher stops using it at its next catalog read).
- With writes on, the policy approves a clear movement of a type in `policy.auto_apply` (by default a
  take-out) right after its dry run (ADR 0004, 0019): it is
  written, read back, and gets a receipt with an Undo button. Each plan is judged once, and only the plan a
  post's first dry run made: one made after an edit or a joined message waits for Apply.
- `stockthread trail <message link>`: one post's dry runs with every review reason and who raised it, its
  questions with their answers and who gave them, and its audit log. It only reads the database, and prints
  ids and codes unless `--text` is given.
- Source selection rule (`core/resolve.py`): which stock items a line takes. It considers only items that are
  OK and unallocated, prefers one item that covers the quantity, otherwise splits oldest first, moves
  serialized stock by serial number, and gives a review reason when batches or supplier parts differ.
- The dry run reads the stock of each settled line (part, place and quantity matched or answered) and shows
  the stock item ids it would act on; stock that does not settle the pick (none, too few, a choice that
  matters) is a review reason. A failed stock read makes the post wait, like a failed catalog read.
  The lines of one post share the stock they read: a later line picks from what the earlier ones left (only a
  shortage they cause says so), and a counted item that another line acts on, or an add into an item that an
  earlier line takes in full, is a review reason.

- `stockthread check-setup`: reads InvenTree and reports the server version, what the token's user lacks
  or has beyond the minimum roles, each `[vehicles]` path that is not exactly one stock location, and
  people who have posted with no vehicle set. Exit code 1 when something must be fixed.
- A question whose plan expired is taken out of its reply with a line that says nothing from the post
  will be applied, and it takes no answer from then on (migration 0014).
- Questions in the thread: an answer by button is acted on at once. A revised dry run applies every
  answer about the same reading, validates again and asks the next question; a "no" to a doubt
  about the reading says it was rejected and that nothing of it will be applied (migration 0013).
- HTTP mode for `stockthread run` (`slack.transport = "http"`): waitress serves the signed Events API
  and button clicks at `[slack.http]` `host`, `port` and `path`, with `manifests/slack-http.json`.
- Design, roadmap and architecture decisions (`docs/`).
- A synthetic eval corpus with a schema, a validator and a reference scorer (`eval/`).
- The Python package scaffold with one-command checks (`make check`).
- Typed configuration from a TOML file and environment variables, with a `check-config` command.
  Secrets in the file are refused, and secrets are redacted everywhere.
- The core domain model and the request state machine of the apply protocol.
- Synthetic Slack payload fixtures and signature test vectors.
- A read-only InvenTree client (GET only), with an API version check.
- Slack ingest checks: workspace, app, channel allowlist, subtypes, bots, duplicates, and the
  approver check for Apply, each drop with a reason.
- A rule-based extractor that needs no LLM. It reads the common one-message shapes and declines
  everything else, and it has zero hard failures on the eval corpus.
- A validator that derives review reasons by rule: flags that follow from the fields, grounding of
  items and quantities in the text, the quantity limit, contradictions, and questions, plans,
  negations, hedges and instructions in the text.
- The dry-run reply renderer: what would change, and every review reason in words, with post
  text escaped so it can never become a Slack mention or link.
- The durable Slack event queue in the store (migration 0002): store before acknowledging,
  pick up unfinished events at startup, and stop retrying an event that keeps failing.
- Integration tests against real InvenTree 1.4.3 and 1.5.6 in Docker, including characterization tests
  of the server behavior the design depends on.
- Grouping of Slack messages into posts (migration 0003): siblings from the same user within the
  window, thread replies with their root, in any arrival order; a message never changes post.
- A threat model, first version (`docs/threat-model.md`).
- The Slack listener on Bolt for Python, for Socket Mode and HTTP: payloads are checked, stored in the
  durable queue and only then acknowledged. HTTP requests are always signature-checked.
- `stockthread run`: connects to Slack over Socket Mode and posts dry runs until stopped. It checks
  the bot token, InvenTree and the catalog before connecting, and refuses a second instance on the
  same database. HTTP serving is not built yet. A rate-limited reply is tried again after the time
  Slack names, without blocking, and the catalog read has a time limit, so the instance lock never
  goes stale under a working instance.
- The store refuses a change of a row id under any of its names (`rowid`, `oid`), not only `id`
  (migration 0005).
- A Slack app manifest for Socket Mode (`manifests/slack-socket.json`) with the minimum scopes, and a
  quickstart in the README.
- Slack connections verify against the system's certificate authorities plus certifi's, so a
  python.org build of Python on macOS works without its "Install Certificates" step.
- The worker (migration 0004): stored events become grouped posts, and once a post's sibling window
  has passed it gets one dry-run reply in its thread. Posts that are not read as a movement get no
  reply. Clicks are recorded and not acted on yet; edits and deletions are noted whichever event
  Slack delivers first, and read as described under revised dry runs below. Each dry run keeps a
  record of what was decided and why (candidates, matches, reasons).
- The validator reads a quantity expression as a whole ("2 of the 5", "half a dozen") and checks the
  unit against the text: a swapped, missing or made-up unit needs review.
- An optional `[llm]` settings section (`base_url`, `model`, `timeout_seconds`; key in `LLM_API_KEY`).
  With it, a post the rule-based extractor declines is read by the model, and gets a dry run like
  any other. Without it, no post leaves the machine. A provider outage makes the post wait; after
  15 minutes it is left unanswered and its record says so (ADR 0011). A post the model refuses or
  does not finish is left unanswered at once, with `llm_no_reading` in its record.
- A `[vehicles]` settings section (Slack user id to stock location path): "my truck" in a post is the
  poster's vehicle. A path that is not found in InvenTree is named in the log and never guessed.
- `python -m stockthread.llm.record`: records a model's raw answers to the eval corpus, so a model is
  scored in CI without a provider. A recording of `anthropic/claude-haiku-4.5` on prompt `llm-0.2`
  is in `eval/recorded/`; behind the rules it has no hard failure on corpus 0.2.0.
- The review reason `photo_not_read`: every movement of a post with a photo waits for a person,
  because no extractor sees photos. With the default policy, a clear take-out with a photo is
  confirmed rather than applied.
- Prompt `llm-0.2`: a movement that a later message takes back ("scratch that") is no movement.
- An LLM extractor behind a provider interface: a versioned prompt,
  a strict output schema (output that does not fit declines the post), masking of strings that
  look like credentials before text is sent, and an adapter for OpenAI-compatible endpoints.
- Catch-up after downtime: posts made while the connector was down or disconnected are read from
  the channel history (up to `slack.catch_up_hours`, and never from before the connector first
  watched the channel) and answered with the review reason `late_ingest` (migration 0006).
- Revised dry runs (migrations 0007 to 0009): a message that joins a post after its dry run, or an
  edit or deletion of one of its messages, makes the post read again. A different reading posts a
  *Revised dry run*, the same reading posts nothing, and no movement any more posts one notice
  that the reply above no longer stands. A post is read as its messages are now: each as its newest
  edit shows it, a deleted one left out, and a change that cannot be read withholds the post.
