# 0010: No attribution of prior work or users

- Status: Accepted
- Date: 2026-09-29
- Decided by: Dan
- Source: [#3](https://github.com/dpellinen-mscs/slack-inventree-connector/issues/3), answer 3A

## Context

The project needed a policy on whether to credit any prior work or users.
The options were no credit, a neutral line in the README, or a named
credit with written approval.

## Decision

- The README, docs, package metadata and commit messages contain no
  attribution of prior work or users. The project stands on its own.
- Docs describe the problem and the design in general terms ("small
  field-service teams"), never as the story of one team.
- All examples stay synthetic (Acme Fiber, Sam's truck), as
  [0001](0001-synthetic-eval-corpus.md) already requires.

## Consequences

- There is nothing to approve or keep in sync with a third party.
- The pre-publication audit checks for attribution text as well as leaks,
  across the full history.
- Adding a credit later is a new decision that supersedes this one. It
  is cheap to add, while removing published text is not.
