# 0015: Recovery's conclusion lives on the attempt, and an unknown write is never retried

- Status: Accepted
- Date: 2026-10-04
- Supersedes: the last bullet but one of [0009](0009-audit-store.md) ("An attempt's `outcome` ... never the
  attempt outcome alone")
- Source: reviews of [#100](https://github.com/dpellinen-mscs/slack-inventree-connector/pull/100)

## Context

ADR 0009 said recovery's resolution of an unknown outcome lives in the
request's audit rows, and that code explaining a marker reads the state and
those rows. The marker check (design section 7, step 2) needs to know
whether recovery found an attempt's marker, and an `unknown` → `verify_failed`
audit row cannot say: both "found, quantities off" and "not found, changed"
end there.

Recovery also cannot prove that a write whose outcome is unknown will never
land. The server may still be running it after the client gave up, after any
settle time, and after the marker check of the next attempt. A missing marker
proves nothing either: the history entry may never have been saved (measured
on 1.5.6 with PostgreSQL) or may have been deleted.

## Decision

- Recovery records its conclusion on the attempt, once:
  `request_attempts.marker_found` (migration 0016), 1 found, 0 not found.
  A trigger refuses leaving `unknown` or `undo_unknown` without it, and
  `Store.recorded_requests` returns it to the marker check. The `outcome`
  column stays what was known when the write returned, as ADR 0009 says.
- Only an attempt that InvenTree refused (a known 4xx) may be followed by
  another attempt of the same kind. `begin_attempt` refuses an apply or an
  undo after an earlier one of that request that was accepted or unknown,
  whatever recovery concluded.
- `begin_attempt` refuses every apply of a post that has an unknown write
  whose marker recovery did not find: a new revision, or the next request,
  would add to it if it lands.
- Approval refuses the same cases, so such a request stays where recovery
  put it (`failed` or `verify_failed`) for the operator, who records the
  movement in InvenTree by hand if it is still wanted. A late landing is
  still reported by the marker check.
- An undo stays allowed from `verify_failed` reached by "not found,
  changed": the operator chose to reverse what was observed. If the apply
  lands after that undo, the stock carries the apply once and nothing is
  reversed twice; the marker check reports the late apply as a `hit`, and
  the post takes no further writes.

## Consequences

- No double write from a late landing, at the price of a post the connector
  will not write again after a timeout it could not resolve. That is rare
  (a timeout or a 5xx whose marker is not found), and a wrong stock write
  is worse than no write.
- Server-side idempotency would lift this. InvenTree has none today.
