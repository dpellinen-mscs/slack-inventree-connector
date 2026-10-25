# 0020: A configured default source settles an implied source

- Status: Accepted
- Date: 2026-10-25
- Source: #188 (`policy.default_source` was accepted and documented but never read); design section 6, "Implied
  source"; ADR 0004

## Context

The usual take-out names no source: "2 Disc 40 on my truck". The brief makes an implied source a review reason
"until policy says otherwise". Design section 6 names that policy: `policy.default_source`, one location per movement
type, unset by default. The setting was validated and shown in `stockthread.example.toml`, but nothing read it. With
the default policy, almost no take-out could apply without a click, so ADR 0004's automatic path was close to empty.

## Decision

1. Each catalog snapshot resolves every `policy.default_source` path as `[vehicles]` paths are resolved: the whole
   path, letter for letter, of one location that can hold stock (`catalog.assign_default_sources`). A path that names
   none is logged once and reported by `check-setup`. Its type keeps the implied-source reason.
2. In the dry run, a movement that states no source, and whose type has a resolved default, gets that location as
   its source. The match has a rule of its own, `default`. The implied-source reason is dropped, whether the rules or
   the model raised it. The reply shows the source as "not stated: the default source".
3. A `default` match can authorize a write, as an exact match can. For the policy it counts as an automatic place
   only for a source the post does not state, and only while it is still that type's default when judged
   (`GateNow.default_sources`). A changed default leaves the plan waiting for Apply. A default never makes a part or a
   destination automatic. A default source that is someone's mapped vehicle waits, as any vehicle the poster did not
   name exactly does.
4. A movement type picked in the thread takes the picked type's default, or none.

## Consequences

- An admin who sets `default_source = { take_to_vehicle = "Warehouse/Main Stock" }` gets "2 Disc 40 on my truck"
  applied with no click, once the part is matched by an exact, normalized or alias rule.
- The dry-run record stores the `default` rule, so the views rebuilt for the write (ADR 0016) carry it.
- No alias is learned from a default: there is no text to learn.
