# Decisions

One file per decision, named `NNNN-slug.md`. Numbers are never reused. A
decision is changed by a new record that supersedes the old one, not by
editing history.

| No. | Decision | Status |
| --- | --- | --- |
| [0001](0001-synthetic-eval-corpus.md) | The eval corpus is fully synthetic and stack-independent | Accepted |
| [0002](0002-idempotency-unit.md) | A post can hold several movements, and the request is the unit of idempotency | Accepted |
| [0003](0003-standalone-python-service.md) | A standalone Python service with a pure core and thin adapters | Accepted |
| [0004](0004-default-write-policy.md) | Clear take-outs apply automatically, everything else is confirmed | Accepted |
| [0005](0005-project-name.md) | The project is named stockthread | Accepted |
| [0006](0006-python-tooling.md) | Python tooling: hatchling, ruff, mypy strict, pytest, make | Accepted |
| [0007](0007-inventree-read-client.md) | The InvenTree read client: httpx, GET only, a tested API range | Accepted |
| [0008](0008-mit-license.md) | The project is licensed under MIT | Accepted |
| [0009](0009-audit-store.md) | The audit store: stdlib sqlite3, SQL migrations, guards in the database | Accepted |

## Template

```markdown
# NNNN: Title

- Status: Proposed | Accepted | Superseded by NNNN
- Date: YYYY-MM-DD
- Decided by: Dan | agent (FYI to Dan)
- Source: link to the issue, if any

## Context

What forces the decision.

## Decision

What we do.

## Consequences

What gets easier, what gets harder, and what would make us revisit this.
```
