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

Learning as built (agent, 2026-10-24; `learning.lessons`, `Worker._learn`):

- Only a plan an approver's Apply click approved teaches, once every request
  of it is `verified`. A plan the policy approved needed no correction.
- In practice the confirmed text is an `answer` match: the gate builds no
  request from a bare whole-word or fuzzy match, so the thread asks to
  confirm it, and the poster's "yes" or pick is what the click then
  confirms.
- A name a person typed in the thread for a line or a place teaches the
  post's own text for it, not the typed name.
- Only the poster's own words are learned: a text must occur in a message
  the poster wrote, as the dry run that made the plan read it. An item or
  place text the model wrote, or read from a photo, is never learned, even
  once a person confirmed the movement.
- A text with a word like "my", "this", "his", "usual" or "ones", a text
  with no letter ("40"), a place named by a vehicle word alone ("the
  van"), and a text the same plan decided for two records are never
  learned (no per-user aliases yet).
- **Narrower than the decision above, pending Dan ([#174](https://github.com/dpellinen-mscs/slack-inventree-connector/issues/174)):** a text is learned
  only when its record is the one record the whole-word and fuzzy rules
  fit it to (articles ignored). So a pick from a
  candidate list ("cable" among three cables, "Disc 40" between the 40 and
  the 40 Pro) teaches nothing, and neither does a typed correction to a
  record the text does not fit ("norvane 40" when the poster took Disc
  60s, "hdmi cable" corrected to a DisplayPort cable) or any text the
  catalog does not back ("the regular", "ubiquity psu" for a part named
  "Power Supply"); an admin adds such an alias by hand. The pick says what one post moved, not what the words always
  mean, and the recheck above cannot catch it: the rivals it would store
  are exactly the ambiguity the pick settled. A wrong write is worse than
  a click, so this errs on asking.
- A confirmation never takes over an alias an admin typed for the same
  text, so no undo can forget one.
- An undo forgets what its plan taught, as soon as the undo begins and
  whether or not it succeeds (`Store.begin_attempt`, audited as
  `alias_forgotten`), rather than offering to: an undo says the plan may
  have been wrong.
- An edit or deletion of any message of the post (the poster's words, an
  answer, a reply) forgets every alias the post taught, as soon as it is
  noted (`Store.note_revision`, audited as `alias_forgotten` by `ingest`):
  it may take back what the alias was learned from. The stock stays moved
  until someone undoes it. A lesson read before an edit that was noted
  before it is stored is refused.

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
