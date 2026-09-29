# 0010: The project does not mention where it came from

- Status: Accepted
- Date: 2026-09-29
- Decided by: Dan
- Source: [#3](https://github.com/dpellinen-mscs/slack-inventree-connector/issues/3), answer 3A

## Context

stockthread is a fresh implementation of an idea that first ran as a
private pipeline. The options were no mention, one neutral line in the
README, or naming the original user with written approval.

## Decision

- The README, docs, package metadata and commit messages say nothing about
  an earlier system or who used it. The project stands on its own.
- Docs describe the problem and the design in general terms ("small
  field-service teams"), never as the story of one team.
- All examples stay synthetic (Acme Fiber, Sam's truck), as
  [0001](0001-synthetic-eval-corpus.md) already requires.

## Consequences

- There is nothing to approve or keep in sync with a third party.
- The pre-publication audit checks for origin mentions as well as leaks,
  across the full history.
- Adding a credit later is a new decision that supersedes this one. It
  is cheap to add, while removing a published mention is not.
