# 0002: A post can hold several movements, and the request is the unit of idempotency

- Status: Accepted
- Date: 2026-09-28
- Decided by: agent (FYI to Dan)

## Context

The brief asks for "at most one movement per Slack post, even across
retries and restarts". Real posts often describe more than one movement:
"2 ONTs to my truck, and returned 1 switch to receiving". Movements with
different destinations need separate InvenTree requests, and InvenTree
cannot run several requests as one transaction. A single state per post
cannot describe a plan whose first request succeeded and whose second
failed.

## Decision

The intent of the brief is kept: nothing is ever applied twice. The unit
is made precise.

- A post has at most one applied plan revision at a time.
- A plan revision holds 1 to n requests.
- Each request has the key `(post key, revision, sequence number)`, its own
  marker and its own state, and is applied at most once.
- Requests run in order. At the first failure or unknown outcome the rest
  are not sent, and the plan is `partially_applied`. The operator is
  offered an undo of the completed requests. Nothing is rolled back
  automatically.
- A new revision of a plan can be applied only after the previous one is
  verified as undone.

## Consequences

- One post can lead to several stock history entries in InvenTree, one
  set per request, each with its own marker.
- The receipt in Slack must show every request and its state.
- `partially_applied` needs its own tests and its own wording in Slack.
- If multi-movement posts turn out to be rare or confusing in practice,
  policy can send every post with more than one movement to review. That
  is a policy setting and does not change this model.
