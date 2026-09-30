# 0012: A whole-word match is confirmed once, and that confirmation becomes an alias

- Status: Accepted
- Date: 2026-09-30
- Decided by: Dan
- Source: [#28](https://github.com/dpellinen-mscs/slack-inventree-connector/issues/28), answer "C"

## Context

The matcher calls a part *matched* when every word of the text is one of
the part's words and exactly one part fits ("Disc 40" → "Norvane Disc 40
Access Point"). On the synthetic corpus that is 80 of the 99 single part
matches; 1 is fuzzy, and only 18 are exact, normalized or a confirmed
alias. ADR
[0004](0004-default-write-policy.md) lets a take-out apply without a click
only when every part is matched by an exact, normalized or alias rule. So
the question was whether a whole-word match may also apply on its own.

## Decision

- **A whole-word match never authorizes a write by itself.** A take-out
  whose part was matched by the whole-word rule waits for "Apply", as
  today. The list in ADR 0004 (exact, normalized, alias) stands.
- **The first confirmation of that text becomes an alias, with no further
  step.** When an authorized person presses "Apply" on a plan, each item
  text in it that was matched by the whole-word rule is stored as an alias
  for the part it was matched to. There is no separate "save this as an
  alias?" question. From then on the same text is an alias match, and a
  take-out with no other review reason applies automatically.
- **Only a confirmation that moved stock does this.** An alias is stored
  once the plan it was confirmed in was applied and verified, and at no
  other time. A dry run (confirmed or not, so nothing is learned while
  writes are off), a plan that expired or was rejected, and anything a
  model suggested never create an alias. This keeps the brief's rule: only
  confirmed human corrections become aliases.
- A single fuzzy match (a typo such as "ondrl") and a pick from a candidate
  list are corrections too, and become aliases under the same rule.

Details decided by the agent (FYI to Dan):

- An alias learned this way is an ordinary alias: it is listed, removable
  and exported by the alias admin, and it is checked against InvenTree
  when it is used (M4).
- Locations follow the same rule as parts. #28 asked about parts, but ADR
  0004 needs every part *and* location matched by an authorizing rule, so
  a location text confirmed in an applied plan ("truck 2") becomes an
  alias too. A location text with a word like "my" is stored for the
  person who posted it only (per-user aliases, M4); until those exist it
  is not stored at all.
- A learned alias is asked again when the catalog grows a rival. The alias
  stores the records that fitted its text by the whole-word and fuzzy
  rules when it was confirmed. When the alias is used, those two rules
  are run on the text again; a record that fits now and was not in the
  stored set makes the result ambiguous (the alias's record plus the new
  ones), and the next confirmation replaces the stored set. So "Disc 40",
  confirmed when only "Norvane Disc 40 Access Point" fitted, is asked
  again once a "Norvane Disc 40 Pro" exists. An alias an admin typed has
  no stored set and is not rechecked this way.

## Consequences

- Alias storage, learning on a verified apply, and reading aliases into
  the matcher move from M4 to M3 (see the roadmap); alias admin and
  per-user aliases stay in M4.
- On a fresh install most take-outs need one click each until the busy
  items have been confirmed once. That is the accepted cost of never
  writing on a guess.
- A wrong confirmation teaches a wrong alias. Undo of the movement must
  offer to forget the alias it taught, and the receipt names the alias
  that was learned.
- Revisit if the clicks in the first days turn out to be a real burden
  (option B in #28: a whole-word match applies when it is the only fit).
