# 0023: InvenTree 1.6 joins the tested range; 1.4 stays in it

- Status: Accepted
- Date: 2026-10-29
- Decided by: agent (FYI to Dan in the status issue)
- Source: amends [0007](0007-inventree-read-client.md)

## Context

ADR 0007 set the tested range to API 511 (1.4.3) to 530 (1.5.6), the versions the integration workflow runs. On a
version outside it, stock writes stay off. InvenTree 1.6.3 (API 561) became `stable` on 2026-10-27, so a site that
upgrades would lose writes. The design also said CI ran the floating `stable` tag; no workflow did, so 1.6 went
unnoticed from 1.6.0 (2026-10-18) on.

## Decision

1. The tested range is API 511 to 561. The integration workflow runs 1.6.3 on SQLite and PostgreSQL next to 1.4.3
   and 1.5.6 (six jobs, up from four).
2. 1.4.3 stays. Narrowing the range turns writes off for a site still on 1.4, which is a product decision for Dan,
   not a side effect of adding a version.
3. No floating `stable` job. A scheduled run that fails would turn main red and pause merges for a reason no change
   caused. A new release joins the range by a pull request whose integration tests pass against it; the design now
   says so.

## Consequences

- Measured on 1.6.3 (both databases): notes over 512 characters are a 400 that changes nothing, where 1.5.6 with
  PostgreSQL committed the change and answered 500; a refused line's errors are keyed by index
  (`{"items": {"0": ...}}`); and a second user with the same email is refused (test fixture only). The connector
  needed no code change: its writer already reads a dict as a field error, and its notes stay under 200 characters.
- Versions between the pinned ones (1.5.7, 1.5.8, 1.6.0 to 1.6.2) are inside the range without being run, as 1.4.4
  and 1.5.0 to 1.5.5 already were.
- Each integration run costs about half again as many Actions minutes. It runs only when InvenTree-facing paths change.
