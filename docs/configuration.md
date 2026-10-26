# Configuration reference

stockthread reads one TOML file for behavior, and environment variables for
secrets. [`stockthread.example.toml`](../stockthread.example.toml) is a
commented starting point. Every setting is listed here, with its default.
`tests/test_docs_configuration.py` fails when this page and the code
disagree.

## The settings file

`stockthread` reads `stockthread.toml` in the directory it is started in.
`--config PATH` or the environment variable `STOCKTHREAD_CONFIG` names
another file. `stockthread check-config` validates the file and prints the
settings it will use, with every secret hidden.

Settings are checked once, at startup:

- An unknown key, or a value of the wrong type, stops the connector with
  an error that names the setting. Types are strict: `"10"` is not a
  number and `"true"` is not a boolean.
- The error never repeats the value, because it may be a secret.
- A `secrets` section is refused: secrets come from the environment only
  (below).

## Secrets (environment variables)

| Variable | Needed | Value |
| --- | --- | --- |
| `SLACK_BOT_TOKEN` | always | the bot token, `xoxb-...` |
| `SLACK_APP_TOKEN` | with `slack.transport = "socket"` | the app-level token, `xapp-...` |
| `SLACK_SIGNING_SECRET` | with `slack.transport = "http"` | the app's signing secret |
| `INVENTREE_TOKEN` | always | an InvenTree API token (README step 4 says which roles) |
| `LLM_API_KEY` | only with an `[llm]` section whose endpoint needs a key | the provider's key; a local server on this machine needs none |

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `slack.transport` | `"socket"` | `"socket"` (Socket Mode, no public URL needed) or `"http"` (the Events API behind a reverse proxy). |
| `slack.workspace_id` | required | The workspace id (`T...`). Events from any other workspace are dropped. Enterprise Grid org installs are not supported yet. |
| `slack.app_id` | required | The app id (`A...`). Events for any other app are dropped. |
| `slack.channels` | `[]` | Channel ids (`C...` or `G...`) the connector acts in. Empty means it acts nowhere. |
| `slack.approvers` | `[]` | Slack user ids (`U...` or `W...`) that may click Apply, click Undo on any post, and answer questions only an approver may answer. The poster of a post may always Undo it. Empty means nobody can click Apply, while `policy.auto_apply` still applies clear movements with writes on (`check-setup` warns). |
| `slack.catch_up_hours` | `72` | After downtime, read posts missed this far back (at most 720). `0` turns catch-up off. A caught-up post is marked late and needs an approver. |
| `slack.group_window_seconds` | `5` | Messages from one person this close together (at most 300) are read as one post, such as a photo and its caption. `0` reads each message alone. |
| `slack.plan_expiry_hours` | `24` | A plan, its questions and its Apply button expire after this long (1 to 720). A post older than this, found by catch-up, is stored and gets no reply. |
| `slack.http.host` | `"127.0.0.1"` | HTTP mode: the address to listen on. The default takes requests from this machine only, that is, from the reverse proxy. In a container, `"0.0.0.0"`. |
| `slack.http.port` | `3000` | HTTP mode: the port. |
| `slack.http.path` | `"/slack/events"` | HTTP mode: the path that events and button clicks both arrive on. It is matched exactly, so it has no trailing slash. Give Slack `https://<public host><path>` as the request URL. |
| `inventree.url` | required | The InvenTree server's base URL, `http://` or `https://`. A user name, password or query in it is refused, because it would be logged. Use https unless InvenTree is on this host or a trusted network: the token goes with every request. |
| `inventree.timeout_seconds` | `10` | Timeout of one request to InvenTree (1 to 300). |
| `writes.enabled` | `false` | The switch of the write gate. Off: every movement is a dry run, and the connector never opens its writing client. Writes also stay off on an InvenTree version outside the tested range. |
| `policy.auto_apply` | `["take_to_vehicle", "take_to_site"]` | Movement types that apply with no click when nothing is in doubt ([ADR 0004](decisions/0004-default-write-policy.md)). Every other type waits for Apply. `[]` makes every movement review-first. A type beyond the two shipped take-outs makes `check-setup` warn. Allowed: `take_to_vehicle`, `take_to_site`, `return`, `transfer`, `receive`, `consume`, `count`. |
| `policy.max_quantity` | `100` | A larger quantity on a line is a review reason. |
| `policy.default_source` | `{}` | A default source location per movement type. Unset, an implied source ("2 Disc 40 on my truck") is a review reason. |
| `apply.settle_seconds` | `120` | How long a write whose outcome is unknown waits before recovery looks for it. It must be longer than `inventree.timeout_seconds`. |
| `undo.max_age_hours` | `168` | An Undo is refused after this long from when the write was made (7 days). |
| `storage.database` | `"stockthread.db"` | The SQLite database file, created readable by its owner only. A relative path is taken from the directory the connector is started in ([operations](operations.md)). |
| `storage.retention_days` | `30` | Message text (raw Slack events, dry-run results) is blanked this many days after a post no longer needs it (1 to 3650). Ids, states and the audit log are kept. It must be longer than `slack.plan_expiry_hours`, `slack.catch_up_hours` and `undo.max_age_hours`. |
| `llm.base_url` | required in `[llm]` | An OpenAI-compatible endpoint, such as `"http://localhost:11434/v1"` for a local server. With no `[llm]` section, no post ever leaves this machine, and the posts the built-in rules cannot read get no reply. |
| `llm.model` | required in `[llm]` | The model name the endpoint expects. |
| `llm.timeout_seconds` | `30` | Timeout of one whole call (1 to 120). A post whose call fails is tried again, and left unanswered after 15 minutes. |
| `vehicles` | `{}` | Whose vehicle "my truck" is: a table from Slack user id to the stock location's path in InvenTree, exactly as InvenTree shows it (`U0SAM0001 = "Vehicles/Truck 1"`). [Locations](locations.md) explains how to model vehicles. |

## Rules across settings

- Socket Mode needs `SLACK_APP_TOKEN`, and HTTP mode needs
  `SLACK_SIGNING_SECRET`.
- `apply.settle_seconds` must be longer than `inventree.timeout_seconds`.
- `storage.retention_days`, in hours, must be longer than
  `slack.plan_expiry_hours`, `slack.catch_up_hours` and
  `undo.max_age_hours`.
- `policy.auto_apply` may not list a movement type twice.
- With an `[llm]` section, `llm.base_url` is the base URL, without
  `/chat/completions`, credentials or a query, and `LLM_API_KEY` is sent
  only over https or to a server on this machine.
