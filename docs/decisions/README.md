# Decisions

One file per decision, named `NNNN-slug.md`. Numbers are never reused. A
decision is changed by a new record that supersedes the old one, not by
editing history.

| No. | Decision | Status |
| --- | --- | --- |
| [0001](0001-synthetic-eval-corpus.md) | The eval corpus is fully synthetic and stack-independent | Accepted |
| [0002](0002-idempotency-unit.md) | A post can hold several movements, and the request is the unit of idempotency | Accepted |

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
