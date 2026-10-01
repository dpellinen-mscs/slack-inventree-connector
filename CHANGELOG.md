# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions will
follow [Semantic Versioning](https://semver.org/) from the first release.

## [Unreleased]

Nothing is released yet. The connector cannot write stock in this state.

### Added

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
  scored in CI without a provider. No model has been recorded yet.
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
