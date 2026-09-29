# 0005: The project is named stockthread

- Status: Accepted
- Date: 2026-09-29
- Decided by: Dan
- Source: [#3](https://github.com/dpellinen-mscs/slack-inventree-connector/issues/3), answer 1A

## Context

The working title `slack-inventree-connector` leads with "Slack", which
Slack's naming guidance discourages, and is long for a package name.
`stockthread` was free on PyPI, npm and GitHub on 2026-09-28.

## Decision

- The project, the Python package and the CLI are named `stockthread`.
- The tagline carries the keywords: "Slack to InvenTree stock movements".
- The license and the origin credit are still open in #3. Until they are
  decided, the repo has no license file.

## Consequences

- New code uses `stockthread` from the start. The docs switch to the name
  as they are touched.
- Renaming the GitHub repository is Dan's call (Settings → General). The
  connector does not depend on the repo name.
- Nothing is published. The PyPI name stays unclaimed until Dan decides to
  go public; the check is repeated before publication.
