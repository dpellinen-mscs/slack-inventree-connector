# 0014: Two Slack fixtures may come from the dev workspace capture

- Status: Accepted
- Date: 2026-10-01
- Decided by: Dan
- Source: [#53](https://github.com/dpellinen-mscs/slack-inventree-connector/issues/53), answer A

## Context

The M1 definition of done asks for two Slack fixtures from a real
workspace capture, so that tests run against the exact field layout Slack
sends and not only the layout the documentation describes. A live run in
the dev workspace produced that capture: the channel history and one thread
as Slack listed them during catch-up. ADR 0001 keeps every fixture
synthetic.

## Decision

- Two fixtures, `conversations.history` and `conversations.replies`, may be
  derived from the dev workspace capture. The dev workspace is a test chat:
  every post in it was scripted, so its message text is already synthetic.
- Every workspace, channel, user, bot and app id, every client message id
  and every display name is replaced with the synthetic ids the other
  fixtures use (`T0ACME001`, `U0SAM0001`, `B0STOCKBOT` and so on). Keys
  outside a fixed allowlist are dropped, including profile and avatar data.
- The derivation runs outside the repository, next to the private
  capture, and refuses to write if any identifier from the input survives
  in its output. Each derived fixture says where it came from.
- This applies only to the designated dev workspace. A capture from any
  other workspace still never enters the repository.

## Consequences

- The catch-up tests gain a check against the real response layout, which
  has more fields than an event (thread counts, `latest_reply`, rich text
  blocks).
- The repository then holds files that began as a capture. Message text and
  timestamps are kept; nothing that identifies a person, workspace or app is.
- ADR 0001 still holds for the eval corpus and for every other fixture.
