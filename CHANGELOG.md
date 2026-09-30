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
