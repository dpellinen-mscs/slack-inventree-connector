# 0018: An unknown undo that never ran is `undo_failed`, and recovery compares with the undo's own snapshot

- Status: Accepted
- Date: 2026-10-07
- Source: recovery of an unknown undo (ROADMAP M3; ADR 0017, decision 3)

## Context

`apply.undo_apply` leaves an undo `undo_unknown` when InvenTree's answer
was not one the connector can trust, when the send raised, or when its
read-back could not read (ADR 0017). The design's state table sent
`undo_unknown` to `undone`, `verified` or `verify_failed`. Two things were
open.

**Where "not found, nothing changed" goes.** `verified` says the apply's
read-back agreed with the plan. An undo can start from `verify_failed` and
`undo_failed` too, so going back to `verified` would forget that the
apply's read-back disagreed. The apply's own case, `unknown` with no
marker and no change, goes to `failed`.

**What "nothing changed" is measured against.** ADR 0017 says recovery
must not conclude it from a marker search alone. The apply's snapshot is
the stock before the apply, so it cannot be used.

## Decision

1. `undo_unknown` → `undo_failed` (by recovery, marker not found) replaces
   `undo_unknown` → `verified`. `undo_failed` now means "the undo did not
   change the stock": refused by InvenTree, or recovered as never run. As
   for an apply (ADR 0015), an undo after one whose outcome was unknown is
   refused by `begin_attempt` however recovery concluded, because it may
   still land. If it does land, the marker check reports it as a `hit`.
2. `apply.recover_undo` waits for `apply.settle_seconds` like
   `recover_request`. Then it rebuilds the undo from the entries of the
   apply that wrote (that apply's entries only, as `undo_apply` saw them
   before the undo could add its own). It also loads the undo's stored
   snapshot (`inverse.UndoSnapshot.from_json`) and refuses to go on if
   the stored undo body is not the one those entries make.
   - Undo marker found: the read-back of `undo_apply`
     (`inverse.verify_undo`), giving `undone` or `verify_failed`.
   - Not found: `inverse.undo_changes` compares the stock with the undo's
     snapshot. Every total must be unchanged, every reversed item must be
     where it was and hold what the apply left, and no reversed item may
     have a new history entry. There must also be no entry of the
     request's id from another attempt. If all of that holds, the result
     is `undo_failed`; otherwise it is `verify_failed`. Entries on a
     split's parent are not compared: the undo never writes there.
   - The apply's entries no longer show what the undo reversed (history
     deleted): `verify_failed`, with whether the marker was found.
3. A read error raises and changes nothing: the request stays
   `undo_unknown`, and recovery runs again later.

## Consequences

- No migration. States are checked in code, and the table in the design
  is tested against `core.requests`.
- A busy location (another writer changing a total in the meantime)
  turns "never ran" into `verify_failed`: a false alarm the operator
  resolves, never a false "nothing changed".
- Not wired yet: the worker calls neither recovery. That comes with the
  Undo button and the worker wiring (ADR 0004).
