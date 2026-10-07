# 0017: An undo is read back against the stock it was built on, and its outcomes reuse the apply's states

- Status: Accepted
- Date: 2026-10-07
- Source: the undo attempt (ROADMAP M3, "The undo attempt: store, send, read-back, recovery")

## Context

`apply.undo_apply` sends one undo with its own marker and reads it back,
as the design says ("It goes through the same protocol, with its own
marker, read-back and recovery"). Two questions were open.

**What the read-back compares with.** The apply's snapshot is the stock
before the apply. Comparing totals with it after the undo would fail
whenever anyone touched another stock item at the same place in the
meantime (a technician taking from another item on the same truck), which
`inverse.check_undo` allows: it only checks the items the undo acts on.

**Which state an accepted undo ends in.** The state table goes from
`undoing` to `undone`, `undo_failed` or `undo_unknown`. There was no state
for "InvenTree accepted the undo, but the read-back disagrees", and none
for "accepted, but the read-back could not read" (the apply has `applied`
for that).

## Decision

1. The undo stores its own snapshot with its intent
   (`inverse.UndoSnapshot`): every total of the movement's pairs, the pair
   each reversed item was read at, and the history of each item the undo
   checked, all from the reads `check_undo` passed. `inverse.verify_undo`
   checks one undo-marked entry per reversed item with the type and numbers
   the undo writes, each item where the undo leaves it holding what it
   should, each total moved by exactly the reversed quantities from that
   snapshot, and no entry on a reversed item since then but the request's.
2. A new edge: `undoing` → `verify_failed`, by the applier, when InvenTree
   accepted the undo and its read-back disagrees. The attempt's outcome is
   `accepted`. From `verify_failed` a second undo is refused by
   `begin_attempt` (an undo wrote), so the operator accepts the state as it
   is, as for an apply whose read-back disagreed.
3. A read error in the undo's read-back moves it to `undo_unknown` and
   raises. Recovery of an unknown undo (not built yet) finds the marker and
   reads it back, which is what an "undo accepted, not read back" state
   would need anyway. The attempt's outcome then says `unknown`, though
   InvenTree answered; the trail line of the run says why.

## Consequences

- No migration: states are checked in code, and `verify_failed` already
  exists.
- An undo whose stock changed again between its check and its send is
  reported by the read-back (`verify_failed`), never folded in.
- `undo.max_age` and who may ask for an undo are the caller's (the button);
  `undo_apply` does not decide them.
