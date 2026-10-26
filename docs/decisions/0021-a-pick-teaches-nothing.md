# 0021: A pick among candidates teaches nothing; an admin adds an alias by hand

- Status: Accepted
- Date: 2026-10-25
- Decided by: Dan
- Source: [#174](https://github.com/dpellinen-mscs/slack-inventree-connector/issues/174), answer "A"; supersedes the
  last bullet of [0012](0012-first-confirmation-becomes-alias.md)'s decision

## Context

ADR 0012 let a pick from a candidate list become an alias once its plan was applied. A text that fits several
records is ambiguous by definition: Sam writes "cable", picks Patch Cable 1 m from three cables, and every later
"cable" from anyone would write Patch Cable 1 m with no click. ADR 0012's recheck does not catch it, because the
rivals it stores are exactly the ambiguity the pick settled. The pick says what one post moved, not what the words
always mean.

## Decision

Dan's words: "Choosing among ambiguous candidates should resolve only the current post, not teach an alias that
automatically applies to later posts. Retain safe alias learning when the poster's words fit one record, and allow
admins to add deliberate aliases by hand."

1. A pick among several records, or a typed correction to a record the text does not fit, settles the current post
   only. It teaches no alias.
2. A confirmed text is still learned when the whole-word and fuzzy rules fit it to that one record (ADR 0012, "as
   built"; `learning.lessons`).
3. `stockthread aliases add part|location TEXT ID` stores an alias an admin typed, for everyone's posts. It is never
   rechecked against rivals, and no confirmation takes it over. Details decided by the agent (FYI to Dan):
   - It reads InvenTree first and refuses a record the matcher would ignore (inactive, template or virtual part;
     structural or missing location); as for a learned alias, a text with no letter, only articles, or a word like
     "my" or "this" (`learning.DEICTIC`: an alias is for everyone), and a place named by vehicle words alone ("the
     van" names another van once there are two); and a text that is some record's exact or normalized name, or
     several records' (the matcher tries names before aliases, so the alias would never be used).
   - It replaces a learned alias for the same text, and refuses while another admin's alias stands for it: removing
     that one first is a deliberate step. Audited as `alias_added` and `alias_replaced`, by `admin`.
   - Records are given by id, as in the record's InvenTree address. Without `--yes` the command stores nothing: it
     prints the part's name and IPN or the location's path, the other records the whole-word or fuzzy rule fits the
     text to now (posts that would have asked which one no longer ask), and the learned aliases it would replace, so
     a wrong id shows before any post can use it.

## Consequences

- A text the team always means one way costs one click per post until an admin adds it by hand.
- Per-user aliases ("my" words, M4) would almost never fire: "my truck" goes through `[vehicles]`, and a "my" text is
  learned only when its other words fit one record, which the whole-word rule already matches. They are dropped
  from v1.0; revisit if posts show a need.
- Revisit if the clicks on ambiguous words turn out to be a real burden (option B on #174: learn a pick per poster).
