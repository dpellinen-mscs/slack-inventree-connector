# stockthread

Slack to InvenTree stock movements.

Turn informal Slack stock posts ("2 APs on Sam's truck") into verified, auditable
[InvenTree](https://inventree.org) stock movements, with a human in the loop where it matters.

**Status:** early development. This version reads Slack and InvenTree and posts **dry runs** only:
it never changes stock.

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

4. **Create an InvenTree token.** In InvenTree, open your user settings and create an API token
   for a user who can view parts and stock. This version only reads.

5. **Fill in `stockthread.toml`:** `workspace_id`, `app_id` and `channels` under `[slack]`, and `url`
   under `[inventree]`. Leave the rest as it is. Secrets go in the environment, never in the file:

   ```sh
   export SLACK_BOT_TOKEN=xoxb-…
   export SLACK_APP_TOKEN=xapp-…
   export INVENTREE_TOKEN=…
   .venv/bin/stockthread check-config
   ```

   `check-config` prints the settings it will use, with every secret hidden, or says what is wrong.

6. **Run it,** and stop it with Ctrl-C:

   ```sh
   .venv/bin/stockthread run
   ```

7. **Post in the channel,** with a part and two stock locations that exist in your InvenTree, for
   example `took 2 Disc 40 from main stock to truck 1`. About 15 seconds later the app replies in
   the thread with a dry run: what it would move, which part and locations it matched, and every
   reason a person would have to look first. Chatter that is not a stock movement gets no reply.

What to expect in this version:

- Nothing is written to InvenTree, whatever `writes.enabled` says.
- The built-in extractor reads simple one-line posts. What it cannot read, it leaves unanswered.
- Optionally, a model reads the posts the built-in extractor cannot: uncomment the `[llm]` section
  in `stockthread.toml` and point it at any OpenAI-compatible endpoint (a hosted provider with
  `LLM_API_KEY` set, or a local server such as Ollama). Only those posts are sent, their text then
  leaves this machine unless the endpoint is local, and strings that look like credentials are
  masked first. No model has been scored on the eval corpus yet: treat it as experimental.
- A vehicle is a stock location: create one per vehicle in InvenTree ("Truck 1") and name it in the
  post. For "my truck" to work, say whose vehicle it is in the `[vehicles]` section of
  `stockthread.toml`: `U0SAM0001 = "Vehicles/Truck 1"` (the person's Slack member id, and the
  location's path exactly as InvenTree shows it). The log names every path it cannot find.
- Posts made while the connector was stopped are read from the channel history when it starts
  again, up to `catch_up_hours` back (a day at most gets a reply), and their dry run says it was
  read late. Posts from before the very first start are left alone.
- The database is one file, `stockthread.db`, next to the settings. It holds the raw Slack events.

## Project documents

- [Design](docs/design.md): pipeline, architecture options, safety model
- [Threat model](docs/threat-model.md): what can go wrong, and which control answers it
- [Roadmap](ROADMAP.md): milestones and their definitions of done
- [Decisions](docs/decisions/): the decision log
- [Research notes](docs/research/): InvenTree, Slack, prior art
- [Eval corpus](eval/): synthetic Slack posts with expected results
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md): set up the project and run the checks

## License

[MIT](LICENSE).
