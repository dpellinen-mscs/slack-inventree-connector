# 0012: A whole-word match is confirmed once, and that confirmation becomes an alias

- Status: Accepted
- Date: 2026-09-30
- Decided by: Dan
- Source: [#28](https://github.com/dpellinen-mscs/slack-inventree-connector/issues/28), answer "C"

## Context

The matcher calls a part *matched* when every word of the text is one of
the part's words and exactly one part fits ("Disc 40" → "Norvane Disc 40
Access Point"). On the synthetic corpus that is 80 of 99 part matches;
only 19 are exact, normalized or a confirmed alias. ADR
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
- **Only a confirmation does this.** A dry run, a plan that expired or was
  rejected, and anything a model suggested never create an alias. This
  keeps the brief's rule: only confirmed human corrections become aliases.
- Typos and plurals with digits ("ondrl", "d40s") are fuzzy candidates, not
  whole-word matches. Picking one from the candidate list is a correction,
  and it becomes an alias under the same rule.

Details decided by the agent (FYI to Dan):

- An alias learned this way is an ordinary alias: it is listed, removable
  and exported by the alias admin, and it is checked against InvenTree
  when it is used (M4).
- An alias is stored only once the plan it was confirmed in was applied
  and verified. With writes off, a confirmation of a dry run still stores
  it, because the person confirmed the reading.
- If the same text later fits a different part (an alias and a catalog
  change disagree), the match is ambiguous and is asked again.

## Consequences

- Alias storage and the matcher's alias rule move from M4 to the slice that
  builds "Apply" (M2/M3); alias admin and per-user aliases stay in M4.
- On a fresh install most take-outs need one click each until the busy
  items have been confirmed once. That is the accepted cost of never
  writing on a guess.
- A wrong confirmation teaches a wrong alias. Undo of the movement must
  offer to forget the alias it taught, and the receipt names the alias
  that was learned.
- Revisit if the clicks in the first days turn out to be a real burden
  (option B in #28: a whole-word match applies when it is the only fit).
