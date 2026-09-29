# Slack fixtures

Synthetic Slack payloads for tests. No fixture comes from a real workspace:
the ids, people and channels match [`eval/catalog/`](../../../eval/catalog/).
The shapes follow the Slack documentation, as summarized in
[`docs/research/slack.md`](../../../docs/research/slack.md). The M1
definition of done adds two fixtures from a real capture of a dev
workspace.

## `events/`

One payload per file:

| Field | Meaning |
| --- | --- |
| `description` | What the payload is and why it matters |
| `kind` | `event` (Events API) or `interactive` (a button click) |
| `transport` | `http` (the body as Slack POSTs it) or `socket` (the Socket Mode envelope) |
| `headers` | HTTP headers that matter for the case, such as retry headers |
| `body` | The payload |
| `expected` | What ingest must do: `process`, `drop` with a `reason`, or `challenge` |

The expected results assume these settings: workspace `T0ACME001`, app
`A0ACME001`, channel allowlist `[C0ACME001]`, approvers `[U0LUIS0005]`,
and that the fixtures of one transport are fed in file name order, so a
retry arrives after its original.

Drop reasons: `bot`, `channel_not_allowed`, `wrong_workspace`, `wrong_app`,
`subtype_not_allowed`, `duplicate_event`, `not_authorized`.

## `signatures.json`

Test vectors for HTTP request signatures (`v0=` HMAC-SHA256 over
`v0:{timestamp}:{raw body}`, 5 minute window). Each case gives the local
time `now`, the headers, the raw body and whether verification must
succeed. The signing secret is synthetic.

## Changing a fixture

Edit the JSON by hand; it is the source of truth. `tests/test_slack_fixtures.py`
checks the structure and recomputes every signature with a reference
implementation.
