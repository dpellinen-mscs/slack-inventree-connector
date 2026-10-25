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
   the model raised it. The reply shows the source as "not stated: the default source". A text that names a source
   the reading left out ("from", "off" or "out of" before a word, but not "dropped off") gets no default: its
   implied-source reason says so (`validate.DETAIL_SOURCE_LEFT_OUT`) and the thread asks for the source. Otherwise a
   model that leaves out "from shelf B" would move stock from the default with no click. A post that says "from" for
   another reason ("a call from Sam") costs one question.
3. A `default` match can authorize a write, as an exact match can. For the policy it counts as an automatic place
   only for a source the post does not state, and only while it is still that type's default when judged
   (`GateNow.default_sources`). A changed default leaves the plan waiting for Apply. A default never makes a part or a
   destination automatic. A default source that is a mapped vehicle waits unless it is the poster's own, as for any
   vehicle the poster did not name exactly. An approved request whose default changed before its write is held
   unwritten at the check before the write; it stays approved, and is written only if that check passes again
   before the plan expires (as for a vehicle mapping that changes).
4. A movement type picked in the thread takes the picked type's default, or none.
5. `receive` and `count` take no default: they take stock from no source, and the settings refuse one.
6. Set a default only for a type whose unstated source really is that one place. A take to a site often comes off
   the poster's truck: a warehouse default there would write the wrong source with no click.

## Consequences

- An admin who sets `default_source = { take_to_vehicle = "Warehouse/Main Stock" }` gets "2 Disc 40 on my truck"
  applied with no click, once the part is matched by an exact, normalized or alias rule.
- The dry-run record stores the `default` rule, so the views rebuilt for the write (ADR 0016) carry it.
- No alias is learned from a default: there is no text to learn.
