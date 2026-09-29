# 0007: The InvenTree read client: httpx, GET only, a tested API range

- Status: Accepted
- Date: 2026-09-29
- Decided by: agent (FYI to Dan)

## Context

Design section 5 calls for a small async REST client of our own instead of
the official `inventree` package, which is synchronous, has no retries and
discards the responses of stock adjustments. M0 needs reads only: parts,
locations and stock items. No code path in the package may write stock
before the write gate exists (M3).

## Decision

- `stockthread.inventree.InvenTreeReader` is built on `httpx.AsyncClient`.
  Async tests use the pytest plugin that ships with anyio (a dependency of
  httpx), so there is no extra test dependency.
- It is GET only. It has no method for anything else, and a request hook
  refuses every other method before a request leaves the process. Writes
  will get a separate client behind the write gate.
- It reads only paths below `/api/`, never follows redirects (a redirect
  could carry the token elsewhere), retries only HTTP 429 (at most 3
  attempts, honoring `Retry-After` up to 30 s), and never repeats a
  response body, an exception text or the token in an error.
- The tested range is API 511 (InvenTree 1.4.3) to API 530 (1.5.6). These
  are the two versions the integration workflow runs. 1.4.3 is the last
  1.4 release with a Docker image, and 1.4.4 has the same API version. On any other version
  reads and dry-runs work, and stock writes stay off with a message.
- Integration tests start a disposable InvenTree in Docker
  (`tests/integration/start-inventree.sh`: SQLite, no worker, no proxy),
  seed it from `eval/catalog/`, and run in their own workflow that
  triggers only when InvenTree-facing paths change. Only the test suite
  holds a raw HTTP helper that can write.

## Consequences

- A new InvenTree release needs a CI run and a one-line range change
  before writes work on it. That is intended: the server is permissive
  (design section 5), so behavior must be characterized per version.
- The PostgreSQL job and a floating `stable` canary are not in the
  workflow yet. They are separate roadmap work, to keep the first
  harness small.
