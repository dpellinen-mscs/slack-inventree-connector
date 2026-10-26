# 0022: A take to a site applies with no click only when its destination is under a configured site

- Status: Accepted
- Date: 2026-10-26
- Decided by: Dan
- Source: [#210](https://github.com/dpellinen-mscs/slack-inventree-connector/issues/210), answer "A"; the rest of
  [#199](https://github.com/dpellinen-mscs/slack-inventree-connector/issues/199); amends
  [0004](0004-default-write-policy.md)

## Context

ADR 0004 lets the policy write a clear `take_to_site` with no click. The connector could not tell a site from any
other location, though. A return, a transfer and a take to a site are all one InvenTree transfer, and only the
take-outs are in the default `policy.auto_apply`. So a post the model reads as a take to a site, when it is a return
("dropped 2 off van 3 at shelf A"), put stock back on a shelf with no click, while a return read correctly waits for
Apply. #204 catches the return words ("returned", "back to"), but not every wording.

## Decision

1. A new setting, `locations.sites`: the whole paths of the stock locations that are sites, such as `["Sites"]`.
   Each path names one location, letter for letter; a structural folder counts. The sites are those locations and
   every location under them, by each location's parent in InvenTree (`catalog.assign_sites`), never by a path
   prefix, since a name may hold a "/".
2. The policy approves a `take_to_site` only when its destination is one of those locations now (`GateNow.sites`,
   read again for each judgement, as vehicles are). Anything else waits for an approver's Apply. Nothing changes for
   a click: an approver may apply a take to any place.
3. Unset, the default, means every take to a site waits for Apply. With writes on and `take_to_site` in
   `policy.auto_apply`, `check-setup` warns. A path that names no location is an error in `check-setup` and a warning
   in the log when the catalog is read; its sites then count as none.

## Consequences

- One more setup line before site drop-offs go through with no click. An admin who follows
  [locations](../locations.md) has the "Sites" folder already.
- A wrong type can still be written with no click only when the model calls a stock room destination a site, which
  takes an admin putting that stock room under a site folder.
- A site moved out of its folder in InvenTree stops counting at the next catalog read.
