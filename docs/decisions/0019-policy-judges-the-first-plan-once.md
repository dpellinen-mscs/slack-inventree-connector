# 0019: The policy judges a post's first plan once, right after its reply

- Status: Accepted
- Date: 2026-10-08
- Source: approval by the policy in the run loop (ROADMAP M3; ADR 0004)

## Context

ADR 0004 lets the policy apply a clear take-out with no click. The run
loop has to decide *when* the policy judges a plan, and which plans. Three
cases make "judge every current plan on every step" unsafe:

- **Writes turned on later.** Every current plan from the last
  `plan_expiry_hours` would be written at once, under replies that said
  "Dry run" when they were posted.
- **Settings changed under a plan.** A return that waited for Apply would
  be written without a click once someone adds `return` to
  `policy.auto_apply`.
- **An edit after an undo.** A person undoes an automatic take-out, then
  fixes a typo in the post. The edit makes a new plan that reads the same;
  the earlier revision left no stock change, so the store would approve
  it, and the movement the person just took back would be written again.

## Decision

1. The worker judges a plan by the policy once, as soon as no event waits
   in the queue after the reply that made it is recorded
   (`Worker.approve_due`), whatever the verdict. A plan the policy does not
   approve waits for Apply.
2. Only a post's first plan (`PlanRow.revision == 1`) is ever approved by
   the policy (`applier.approve_by_policy`). Any later plan, made after an
   edit, a joined message or an answer, waits for Apply even when the gate
   would allow it.
3. Which plans wait to be judged is kept in memory only. A plan whose
   judgement a restart cut off waits for Apply. An outage (InvenTree, the
   network, the disk) puts the judgement off, 30 s doubled with each
   failure, for at most `APPROVE_FOR_SECONDS` (10 minutes) from the reply;
   after that the plan waits for Apply. Any other failure is logged as an
   error and the plan waits for Apply.
4. `stockthread run` gives the worker its approver (`run.policy_approver`)
   only with writes on and a tested server, as it does the apply step. Each
   judgement reads the catalog's vehicles and the server's version again.
   The gate is judged again before the write (ADR 0016).

## Consequences

- Every failure mode fails closed: the movement waits for a click instead
  of being written late or unasked.
- Until the Apply button exists, a take-out whose judgement was cut off by
  a restart, or that was edited before it was judged, is never written.
  The Apply button (next) gives those a way through.
- A post edited before its write needs a click even when the edit changed
  nothing that matters. Revisit if that turns out to be common.
