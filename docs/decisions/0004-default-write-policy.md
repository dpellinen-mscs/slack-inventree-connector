# 0004: Clear take-outs apply automatically, everything else is confirmed

- Status: Accepted
- Date: 2026-09-29
- Decided by: Dan (the details marked "agent" are FYI to Dan)
- Source: [#4](https://github.com/dpellinen-mscs/slack-inventree-connector/issues/4), answer "2B with two refinements"

## Context

Once an admin turns writes on, the connector needs a default policy.
Dan runs a similar system today. His priority: ship, make mistakes
visible, and make them easy to undo.

## Decision

These invariants do not change:

- Writes are off on a fresh install. Turning them on is an explicit
  configuration step. Dry-run is always available.
- Model confidence never authorizes a write by itself. Every write passes
  the deterministic gate (design section 6).
- Aliases are learned only from confirmed human corrections.

Once writes are on, the default policy is:

1. **Clear take-outs apply automatically.** A `take_to_vehicle` or
   `take_to_site` movement with no open review reason is applied without a
   click. The connector replies in the thread with a receipt and an Undo
   button.
2. **Intake, receiving and returns are confirmed.** A `receive` or
   `return` movement always waits for one click on "Apply", even when it
   has no review reason.
3. **Doubt becomes one question.** When the destination, the part, or the
   model's own self-check is in doubt, the connector asks one focused
   question in the thread, or offers a short candidate list with buttons.
   Nothing is applied until it is answered.

Details decided by the agent (FYI to Dan):

- `transfer` (between two stock locations), `consume` and `count` are
  confirmed like returns. They are not take-outs, and a wrong count or
  removal is harder to see than a wrong transfer to a vehicle. `unclear`
  never applies.
- The policy is configurable per movement type
  (`policy.auto_apply`, default `[take_to_vehicle, take_to_site]`). An
  empty list gives a review-first install.
- "No open review reason" for auto-apply means, in addition to the gate in
  design section 6: every part and location is matched by an exact,
  normalized or alias rule (a fuzzy candidate is doubt about the part), the
  vehicle resolves through a mapped Slack user or an exact location name,
  and the model raised no flag. If the rule-based extractor also produced a
  candidate and it disagrees with the LLM extractor, that is a review
  reason. If the rule-based extractor produced nothing, that alone is not.

## Consequences

- Undo and the receipt are on the critical path of the first write
  milestone. Auto-apply must not ship without them (M3).
- The gate needs tests for both paths: an auto-apply that is allowed, and
  one that each review reason blocks. The pipeline-level test from design
  section 6 (varying and stripping every model output never moves the gate
  from blocked to allowed) covers auto-apply too.
- The eval scorer counts an invented movement without a flag as a hard
  failure, because it would be applied without a click.
- Revisit if undo turns out to be used often for auto-applied take-outs.
