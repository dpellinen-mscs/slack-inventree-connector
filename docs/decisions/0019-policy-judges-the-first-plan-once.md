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
2. Only the plan a post's first dry run made is ever approved by the
   policy: the worker queues a plan for judgement only from dry-run
   revision 1, and `applier.approve_by_policy` also requires plan row
   revision 1, no second dry run and no noted edit or deletion. A plan
   made after an edit, a joined message or an answer waits for Apply even
   when the gate would allow it. The plan row's revision alone is not
   enough: a post whose first dry run made no plan (chatter, or a
   question) gets its first plan row from a later dry run, and chatter
   edited into a take-out would otherwise be written with no click.

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
- The judgement waits for an empty event queue. In a very busy channel a
  plan can age out to "waits for Apply" without being judged; that fails
  closed, and the log says so for each plan.
- Once approved, the write itself is retried through outages by the apply
  step, with no 10-minute limit; an edit or deletion before the write
  begins still cancels it: `Store.begin_attempt` refuses to start an
  apply while any event waits in the queue, checked in the transaction
  that stores the intent, so an edit received during the reads before
  the write is handled first. Any event counts, not only one of the
  post: an unrelated message that comes during the reads puts the write
  off to the next step, and in a channel that never goes quiet an
  approved plan can wait until it expires. That fails closed. The reply still reads "Dry run" for the moment
  between the reply and the write; the receipt that follows says what was
  written. Both are revisited with the Apply button slice.
