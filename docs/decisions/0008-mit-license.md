# 0008: The project is licensed under MIT

- Status: Accepted
- Date: 2026-09-29
- Decided by: Dan
- Source: [#3](https://github.com/dpellinen-mscs/slack-inventree-connector/issues/3), answer 2A

## Context

The project needs a license before anyone else can use it. The options were
MIT, Apache-2.0 (adds a patent grant and some paperwork) and AGPL-3.0
(forces hosted forks to share changes, which puts off many users).
InvenTree, its Python client and Slack's Bolt are all MIT.

## Decision

- The project is licensed under MIT. The text is in [`LICENSE`](../../LICENSE),
  with the copyright held by Dan Pellinen.
- `pyproject.toml` declares it with an SPDX expression (`license = "MIT"`,
  PEP 639), so built packages carry the license file and metadata.
- Source files carry no per-file license header. MIT does not need one,
  and a header in every file adds noise for a small codebase.
- The origin credit is still open in #3.

## Consequences

- Contributions come in under MIT (inbound equals outbound). CONTRIBUTING
  says so, and there is no CLA.
- Dependencies must be MIT-compatible. That is true of everything used so
  far. A copyleft dependency would need a new decision.
- The license says nothing about going public. The repo stays private
  until Dan decides otherwise.
