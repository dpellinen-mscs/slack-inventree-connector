# stockthread

Slack to InvenTree stock movements.

Turn informal Slack stock posts ("2 APs on Sam's truck") into verified, auditable
[InvenTree](https://inventree.org) stock movements, with a human in the loop where it matters.

**Status:** early development. Out of the box it only reads Slack and InvenTree and posts **dry
runs**: it changes no stock until you set `writes.enabled = true` (see "What to expect" below).

## Quickstart: the first dry run

You need Python 3.11 or later, an InvenTree server (1.4 or 1.5), and a Slack workspace where you may
install an app. About 15 minutes.

1. **Install.** From a checkout of this repository:

   ```sh
   python3 -m venv .venv
   .venv/bin/pip install .
   cp stockthread.example.toml stockthread.toml
   ```

2. **Create the Slack app.** Open <https://api.slack.com/apps>, choose *Create New App*, then *From a
   manifest*. Pick your workspace, switch to the JSON tab, and paste
   [`manifests/slack-socket.json`](manifests/slack-socket.json). The app can read messages in the
   channels it is invited to and reply; it uses Socket Mode, so it needs no public URL.
   - *Basic Information* → *App-Level Tokens* → generate a token with the scope `connections:write`.
     This is `SLACK_APP_TOKEN` (`xapp-…`).
   - *Install App* → install to the workspace. The *Bot User OAuth Token* is `SLACK_BOT_TOKEN`
     (`xoxb-…`).
   - *Basic Information* shows the *App ID* (`A…`).

3. **Pick a channel.** In Slack, invite the app to the channel where stock is reported:
   `/invite @stockthread`. Open Slack in a browser: the address of the channel is
   `https://app.slack.com/client/T…/C…`. The `T…` part is the workspace id and the `C…` part is the
   channel id.

4. **Create an InvenTree token.** A token carries all the permissions of its user, so create a
   user just for the connector. In InvenTree's admin, give it a group with exactly these roles:

   | Role | Permissions |
   | --- | --- |
   | Part | view |
   | Stock Location | view |
   | Stock Item | view |

   Log in as that user, open its user settings and create an API token. These roles are enough for
   dry runs. Writing stock (`writes.enabled`) also needs *add* on *Stock Item*; `check-setup` (step 5)
   says so with a note.

5. **Fill in `stockthread.toml`:** `workspace_id`, `app_id` and `channels` under `[slack]`, and `url`
   under `[inventree]`. Leave the rest as it is. Secrets go in the environment, never in the file:

   ```sh
   export SLACK_BOT_TOKEN=xoxb-…
   export SLACK_APP_TOKEN=xapp-…
   export INVENTREE_TOKEN=…
   .venv/bin/stockthread check-config
   ```

   `check-config` prints the settings it will use, with every secret hidden, or says what is wrong.
   Then `.venv/bin/stockthread check-setup` reads InvenTree. With writes on, it first warns when
   `slack.approvers` is empty (nobody can click Apply and only the poster can Undo, yet the policy
   still applies clear take-outs) or `policy.auto_apply` goes beyond the take-outs. It reports the
   server version, the token's expiry (a warning 30 days before), the roles of the token's user
   (what is missing, and what is more than needed), any `[vehicles]` path that is not a stock
   location, and any learned alias whose part or location can no longer be matched (the matcher
   ignores it). It exits with 1 when something must be fixed, and with 2 when the check could not
   run at all (the settings do not load, or InvenTree cannot be reached). It only reads.

6. **Run it,** and stop it with Ctrl-C:

   ```sh
   .venv/bin/stockthread run
   ```

7. **Post in the channel,** with a part and two stock locations that exist in your InvenTree, for
   example `took 2 Disc 40 from main stock to truck 1`. About 15 seconds later the app replies in
   the thread with a dry run: what it would move, which part and locations it matched, and every
   reason a person would have to look first. Chatter that is not a stock movement gets no reply.

What to expect in this version:

- Nothing is written to InvenTree while `writes.enabled` is false (the default). With it true, and
  an InvenTree version CI tests, a clear movement of a type in `policy.auto_apply` (by default a
  take-out to a vehicle or a site) is written right after its dry run, read back, and reported in
  the thread with an Undo button. Every other movement that could apply waits for an approver
  (`slack.approvers`) to click Apply on its dry-run reply; Slack asks them to confirm first.
- The built-in extractor reads simple one-line posts. What it cannot read, it leaves unanswered.
- Optionally, a model reads the posts the built-in extractor cannot: uncomment the `[llm]` section
  in `stockthread.toml` and point it at any OpenAI-compatible endpoint (a hosted provider with
  `LLM_API_KEY` set, or a local server such as Ollama). Only those posts are sent, their text then
  leaves this machine unless the endpoint is local, and strings that look like credentials are
  masked first. On the synthetic eval corpus, one small hosted model behind the built-in extractor
  had no hard failure (scores in `docs/design.md`); other models may score differently, so record
  and score yours (`eval/README.md`). A post with a photo always waits for a person: no extractor
  sees photos.
- A vehicle is a stock location: create one per vehicle in InvenTree ("Truck 1") and name it in the
  post. For "my truck" to work, say whose vehicle it is in the `[vehicles]` section of
  `stockthread.toml`: `U0SAM0001 = "Vehicles/Truck 1"` (the person's Slack member id, and the
  location's path exactly as InvenTree shows it). The log names every path it cannot find.
  [Modeling vehicles and sites](docs/locations.md) shows a layout that works and how place names
  are matched.
- Posts made while the connector was stopped are read from the channel history when it starts
  again, up to `catch_up_hours` back (a day at most gets a reply), and their dry run says it was
  read late. Posts from before the very first start are left alone.
- The database is one file, `stockthread.db` (`storage.database`), readable by its owner only. A
  relative path is taken from the directory the connector is started in, not from where the settings
  file is: start it from the same directory every time, or give an absolute path. It holds the raw
  Slack events. Message text is blanked `storage.retention_days` (30) days after a post
  is done with; ids, states and the audit log stay, so `trail` still shows what happened.
  [Operations](docs/operations.md) covers backing it up, restoring it and upgrading over it.
- To see why a post got the reply it did, copy the link of any of its messages (*Copy link* in
  Slack) and run `.venv/bin/stockthread trail '<link>'`. It prints each dry run with every reason it
  needs a person, the question asked, who answered and with what, and the audit log. It only reads
  the database, and prints ids and codes rather than the words of the post, unless you add `--text`.
- `.venv/bin/stockthread aliases list` prints the confirmed aliases ("disc 40" means part 12), each
  with its id and the post it was learned from; `aliases export` prints them as JSON, for a backup.
  `aliases remove <id>` forgets one, also while the connector runs: posts read after that are
  matched as if it had never been learned. `aliases history` prints every time an alias was learned,
  added, replaced, forgotten or removed, with the part or location it stood for, also after it is gone.
- A pick among several parts teaches nothing, so a word your team always means one way ("ubiquity
  psu" for one power supply) asks every time until you add it by hand:
  `.venv/bin/stockthread aliases add part 'ubiquity psu' 57`, where 57 is the id in the part's
  InvenTree address (`/part/57/`); `aliases add location 'back room' 12` for a stock location. It
  reads InvenTree and shows the part's name or the location's path, any other record the text
  also fits now (posts saying it will no longer be asked which one), and any learned alias it
  would replace; add `--yes` to store it. It refuses a record that cannot hold stock, a text with a word
  like "my" or "this", a vehicle word alone ("the van"), and a text that is already some record's
  name. It applies to everyone's posts.

### Serving over HTTP instead of Socket Mode

Socket Mode needs no public URL, and suits most teams. To receive Slack's Events API over HTTP
instead, you need a public https URL that reaches the connector, usually a reverse proxy with TLS.

1. Create the app from [`manifests/slack-http.json`](manifests/slack-http.json) instead, after
   replacing both `https://stockthread.example.com/slack/events` with your URL. Slack checks the URL
   at once and says it could not verify it: that is expected, the connector is not running yet.
   Skip the app-level token, and install the app as above. *Basic Information* → *Signing Secret* is
   `SLACK_SIGNING_SECRET`.
2. In `stockthread.toml`, set `transport = "http"` under `[slack]`, and check `[slack.http]`: the
   connector listens on `127.0.0.1:3000` at `/slack/events` by default. Point the proxy there; in a
   container, set `host = "0.0.0.0"` and publish the port only to the proxy.
3. Export `SLACK_SIGNING_SECRET` instead of `SLACK_APP_TOKEN`, then run `check-config` and `run`
   as above.
4. With the connector running, open *Event Subscriptions* and *Interactivity & Shortcuts* in the
   app's settings and click *Retry* (or *Verify*) next to the request URL, then save.

Keep the host's clock in sync (NTP): a request signed more than five minutes away from the
connector's time is refused. If deliveries keep failing, Slack may pause the app's events until you
turn them on again in *Event Subscriptions*; catch-up reads what was missed, within `catch_up_hours`.

## Project documents

- [Architecture](docs/architecture.md): how a post becomes a stock change, and where each step lives
- [Design](docs/design.md): pipeline, architecture options, safety model
- [Threat model](docs/threat-model.md): what can go wrong, and which control answers it
- [Security policy](SECURITY.md): how to report a vulnerability privately
- [Configuration reference](docs/configuration.md): every setting, its default, and the secrets
- [Modeling vehicles and sites](docs/locations.md): stock locations for vehicles, sites and stock rooms
- [Operations](docs/operations.md): the database, backups, restores and upgrades
- [Troubleshooting](docs/troubleshooting.md): no reply, no match, no write, and what the log says
- [Supported versions](docs/versions.md): the InvenTree, Python and Slack versions CI tests
- [Roadmap](ROADMAP.md): milestones and their definitions of done
- [Decisions](docs/decisions/): the decision log
- [Research notes](docs/research/): InvenTree, Slack, prior art
- [Eval corpus](eval/): synthetic Slack posts with expected results
- [Changelog](CHANGELOG.md)
- [Releasing](docs/releasing.md): versions, and the steps of a release (none published yet)
- [Contributing](CONTRIBUTING.md): set up the project and run the checks

## License

[MIT](LICENSE).
