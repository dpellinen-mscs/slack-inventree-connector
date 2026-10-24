# 0016: Apply acts on the dry-run record the thread was shown, never on a new reading

- Status: Accepted
- Date: 2026-10-06
- Source: worker wiring of the apply protocol (ROADMAP M3, "Worker: run `apply_request` ...")

## Context

`apply_request`, `recover_request` and `readback_request` each take the
`MovementView` the request was built from, and `apply_request` judges the
whole plan again (`gate.judge`) from the views. The worker builds views in
memory during a dry run and keeps only the dry-run record
(`Store.finish_dry_run`, the JSON of `worker._record`). An apply can come
much later than the dry run: after an "Apply" click, after a restart, or
in recovery after the settle time. So the views must come from somewhere
that outlives the process.

Three sources were possible:

- **Run the pipeline again at apply time** (extract, match, resolve). The
  model is not deterministic, and the catalog, aliases and vehicles may
  have changed since. The write could then differ from what the reply in
  the thread showed and what a person approved.
- **Store a separate plan payload** with each plan revision. It would
  repeat what the record already holds, and two copies can disagree.
- **Rebuild the views from the dry-run record.** The record is what the
  reply showed (the reply is rendered from the same views), and it already
  holds every field the gate, `plan_requests` and the precondition read:
  movement type and flags, the place texts and matches, each line's text,
  quantity, unit, pack size, serials and part match, the reasons, and each
  line's stock takes.

## Decision

- Apply, recovery and read-back act on views rebuilt from the dry-run
  record of the plan's revision, and on nothing else. The connector never
  extracts, matches or picks stock again to write.
- A plan revision is created from a record that could apply in full (no
  movement has a gate reason). Its `input_hash` is the SHA-256 of the
  canonical JSON of the record's `movements`. Rebuilding refuses a record
  whose hash differs from the plan's, a record retention removed, and a
  movement with any gate reason: the request is then not written, and stays
  where it is for the operator.
- Rebuilt picks carry only their takes, the one part of a pick a write
  uses. A pick with reasons cannot occur in a record that could apply.
- What the gate reads besides the views is read fresh at each judgement:
  the settings (switch, policy, approvers), the server's version check,
  the vehicles from the current catalog, and the answers, the rejection
  and the Apply click from the store.
- Checks of the dry run itself are not made again: a lower
  `max_quantity` set after a plan was made does not block that plan.
- Stock is read fresh too: the precondition (step 3) compares the rebuilt
  picks with InvenTree now, and sends the request back to review when they
  no longer hold.

## Consequences

- What is written is exactly what the thread was shown. A change in the
  catalog, the aliases or the model after the dry run cannot change a
  write; it can only block one (the gate or the precondition).
- Retention, when it is built, must keep the record of every plan revision
  with a request that is not in a final state, or that an undo may still
  reverse. (Retention, PR #166: a `planned` request whose plan has expired
  counts as final here. Nothing can approve it any more: a click is refused
  as expired and the policy judges a plan only right after its reply.)
- The record format is now an input of writes. A change to it needs the
  rebuild to read both the old and the new form, or a migration, and a
  round-trip test (record → views → record) guards it.
