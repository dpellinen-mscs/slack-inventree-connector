# 0003: A standalone Python service with a pure core and thin adapters

- Status: Accepted
- Date: 2026-09-29
- Decided by: Dan
- Source: [#4](https://github.com/dpellinen-mscs/slack-inventree-connector/issues/4), answer 1A

## Context

The connector could be a standalone service, an InvenTree plugin, or a
TypeScript service. Design section 3 lists the tradeoffs. The users and the
likely contributors come from the InvenTree community, which runs Python.

## Decision

- One Python package, deployed as one service (one container) next to
  InvenTree. It talks to InvenTree only through the REST API.
- Inside the package: a pure core (extract, validate, match, plan, gate)
  with adapters for Slack, InvenTree and storage. An adapter interface
  exists only where a second implementation exists or is planned (for
  example the LLM provider, and the rule-based extractor next to the LLM
  extractor).
- Slack through Bolt for Python. Socket Mode is the default, so no public
  URL is needed. HTTP mode with signature verification is also supported.
- No InvenTree plugin before v1.0. A thin plugin can come later if users
  ask for settings inside the InvenTree UI.

## Consequences

- The connector works across InvenTree versions that share the REST API,
  and a bug in it cannot touch the InvenTree database directly.
- Integration tests run the service against a real InvenTree in Docker.
- The admin runs one more container. The quickstart must keep that to one
  `docker compose` step.
- Tooling choices for the package (formatter, type checker, test runner,
  dependency manager) are made in the M0 scaffold and recorded there.
