# Troubleshooting

Start with the two checks and the log. They answer most questions.

- `stockthread check-config` says whether the settings file and the environment are valid, and prints the settings
  it will use with every secret hidden.
- `stockthread check-setup` reads InvenTree and says what must be fixed: the server version, the token's roles,
  `[vehicles]` paths, learned aliases, and, with writes on, who can write.
- `stockthread run` logs to the terminal (standard error), one line per decision. Lines name posts by their Slack
  timestamp, as in post 1729900000.000100, and never quote the words of a post.
- `stockthread trail '<link to a message>'` prints what happened to one post: each dry run with every reason it
  needs a person, the question asked, who answered, and the audit log. It only reads the database.

Run every command from the directory you start the connector in: a relative `storage.database` is taken from there.

## A post gets no reply

Look for the post's line in the log.

| Log line | Why | What to do |
| --- | --- | --- |
| no line at all | Slack did not deliver the post. The app is not in the channel, or (HTTP) Slack paused the app's events. | `/invite @stockthread` in the channel. For HTTP, see [the HTTP section](#over-http-slack-says-it-cannot-reach-the-url). |
| `slack event dropped: channel_not_allowed` | The channel id is not in `slack.channels`. An empty list means the connector acts nowhere. | Add the `C...` id from the channel's address. |
| `slack event dropped: wrong_workspace` or `wrong_app` | `slack.workspace_id` or `slack.app_id` is not this workspace's or app's. | Copy them again: the `T...` from the channel's address, the `A...` from *Basic Information*. |
| `slack event dropped: bot` | A bot or app posted it, including this app. | Nothing: bot posts are never read. |
| `slack event dropped: subtype_not_allowed` | A join, a topic change, or another message that is not a person's post. | Nothing. |
| `post ...: no reply (not a movement)` | The post reads as chatter. | Nothing, or say what moved: a quantity, a part, and a place. |
| `post ...: no reply (declined)` | The built-in extractor could not read it, and no model is set. | Post it more plainly ("took 2 Disc 40 from main stock to truck 1"), or set up `[llm]` (README). |
| `post ...: withheld (too_old)` | The post is older than `slack.plan_expiry_hours` (24 by default), for example read by catch-up after a long stop. | Post it again if it still needs doing. |
| `post ...: no answer from the LLM provider (...), attempt ...` | The model endpoint did not answer; the post is tried again. After `post ...: gave up on the LLM provider`, the post is decided without the model. | Check `[llm]` `base_url`, the model name and `LLM_API_KEY`; `check-config` prints the model line. |
| `catch-up: channel ...: Slack answered not_in_channel: invite the app to the channel` | The app is not a member of a channel in `slack.channels`. | `/invite @stockthread` in that channel. |

A post made before the connector's very first start is never read. A post made while it was stopped is read when it
starts again, up to `slack.catch_up_hours` back.

## "My truck" is not matched

The poster has no vehicle in `[vehicles]`, or its path is not a stock location.

- `check-setup` lists each `[vehicles]` path that is not the whole path of exactly one stock location that can hold
  stock, and each person who has posted with no vehicle set.
- The log says `vehicles: no stock location at the path set for ...; their "my truck" will not be matched` when the catalog is read.
- The path must match InvenTree letter for letter, from the top: `"Vehicles/Truck 1"`, not `"Truck 1"`. A structural
  location cannot hold stock. [Modeling vehicles and sites](locations.md) shows a layout that works.

## Every reply asks which part or location

The text fits more than one record, or none closely enough. The dry run lists the candidates.

- A text is learned as an alias when an approver confirms a movement whose text fits one record closely, and
  InvenTree reads the write back. A pick among several records teaches nothing (ADR 0021): add the alias by hand,
  `stockthread aliases add part 'ubiquity psu' 57`, and `--yes` once the dry run of the command looks right.
- `stockthread aliases list` shows what is learned, and `aliases history` when each alias came and went.
- `check-setup` reports learned aliases whose part or location can no longer be matched; the matcher ignores them.

## Writes are on, but nothing is written

`run` logs one of these when it starts:

| Log line | What to do |
| --- | --- |
| `stock writes are off (writes.enabled = false): only dry runs are posted` | Set `writes.enabled = true` under `[writes]`, and restart. |
| `stock writes are off (this InvenTree is not tested for writes): only dry runs are posted` | The server's API version is outside the tested range; the line before it names both. Dry runs still work. Use a tested InvenTree version. |
| `stock writes are ON: movements the policy approves (...) are written, read back and can be undone` | Writes are on. Read on. |

With writes on, a movement is written with no click only when the policy approves it: its type is in
`policy.auto_apply` (by default a take to a vehicle or to a site), and the dry run has no reason that needs a person.
A take to a site also needs its destination under one of the `locations.sites` paths; with none set, it waits.
An implied source ("2 Disc 40 on my truck") is such a reason unless `policy.default_source` sets one for that type.
Everything else waits for an approver to click Apply. The log line `post ...: plan ... (...)` says what the policy
decided and why.

If the token lacks *add* on *Stock Item*, `check-setup` says so, and InvenTree refuses every write.

`check-setup` warns about two policies that write with nobody able to step in, and one that writes less than it says:

- `approvers: none in slack.approvers, so nobody can click Apply or answer for an approver, and only the poster can Undo; the policy still applies with no click: ...`:
  add the user ids of the people who may approve to `slack.approvers`, or set `policy.auto_apply = []`.
- `policy: auto_apply has ..., beyond the shipped take-outs: those apply with no click`: a return, receive, count or
  other type in `policy.auto_apply` is written with no click. Remove it unless that is what you want.
- `policy: auto_apply has take_to_site, but locations.sites is unset, so every take to a site waits for Apply`: set
  `locations.sites` to your sites folder, such as `["Sites"]` ([locations](locations.md)).
- The log says `locations.sites: no stock location at ...; a take to a site there waits for Apply` when a path is not
  the whole path of exactly one location. `check-setup` says why.

## Clicking Apply or Undo does nothing, or says no

- A click from someone who is not in `slack.approvers` is dropped: the log says
  `slack interactive dropped: not_authorized`. Apply is for approvers only.
- Undo is for the poster of the post and for approvers. It is refused when the movement was written more than
  `undo.max_age_hours` ago (undo it in InvenTree), when the movement was not written and read back, or when an
  earlier undo may have landed. A note to the clicker alone says which.
- Apply answers the clicker alone. "Not applied: writes are turned off" means `writes.enabled` is false.
  "Not applied: the post changed or is being worked on" clears in a moment; click again. A refusal by the policy adds
  its reason.

## The connector will not start

| Log line | What to do |
| --- | --- |
| `another instance is running on ...` | Another `stockthread run` uses the same database. Stop it first. |
| `cannot start: Slack answered invalid_auth` (or another code) | A Slack token is wrong or revoked. Copy `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` again. The app-level token needs the scope `connections:write`. |
| `cannot start: ... reaching Slack` | The machine cannot reach Slack: check the network or proxy. |

`check-config` refuses unknown keys, wrong types and any secret in the file, and says which key is wrong. Secrets
belong in the environment: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` (Socket Mode) or `SLACK_SIGNING_SECRET` (HTTP),
`INVENTREE_TOKEN`, and `LLM_API_KEY` for a hosted model.

## Over HTTP, Slack says it cannot reach the URL

- Slack checks the request URL when the app is created, before the connector runs; that first failure is expected.
  Start the connector, then click *Retry* next to the URL in *Event Subscriptions* and in
  *Interactivity & Shortcuts*.
- The connector listens on `127.0.0.1:3000` by default. In a container, set `[slack.http] host = "0.0.0.0"`, and
  publish the port only to the reverse proxy.
- A request with a bad signature, or signed more than five minutes from the connector's clock, is refused and not
  logged. Keep the clock in sync (NTP), and check `SLACK_SIGNING_SECRET`.
- After repeated failures Slack may pause the app's events. Turn them on again in *Event Subscriptions*; catch-up
  reads what was missed, within `slack.catch_up_hours`.

## Where the data went

The database is one file (`storage.database`, `stockthread.db` by default). Message text is blanked
`storage.retention_days` after a post is done with; `trail` then shows ids and states but no words.
[Operations](operations.md) covers backups, restores and upgrades.
