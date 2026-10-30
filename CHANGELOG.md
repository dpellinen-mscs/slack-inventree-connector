# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions will
follow [Semantic Versioning](https://semver.org/) from the first release.

## [Unreleased]

Nothing is released yet. With `writes.enabled = true`, the connector writes clear movements of the types in
`policy.auto_apply` with no click (by default take-outs to a vehicle or a site), and any other movement the gate
allows once an approver clicks Apply on its dry-run reply (by default returns, receipts, counts and the rest). A
reply too long to show in full carries no Apply button. An approved plan is never written once it has expired
(`slack.plan_expiry_hours`): it goes back to review; a plan whose first write began is finished.

### Changed

- InvenTree 1.6.3 (API 561) is tested, on SQLite and PostgreSQL, so stock writes are no longer turned off on it.
  The tested range is now API 511 to 561 (1.4.3 to 1.6.3).
- A configured `policy.default_source` no longer settles a source the text names in wording the check missed
  (T19): "from 3rd floor cage", "from 3 boxes in the cage", "from: shelf B", "from (shelf B)", "out the van",
  "outta", "frm", "src: shelf B", "@ shelf B", "at the yard", and the arrows "=>", "⇒", "⟶", a dash then ">" and a
  lone ">" between words now mark the source as left out, so the thread asks for it. Typed arrows are read as Slack
  sends them ("-&gt;"), which also makes a typed "->" count as a destination word again.
- A configured `policy.default_source` no longer settles a source the text names with no marker word in a
  movement's own clause when that place is another movement's destination (T19): in "took 2 ONTs to van 3, then 1
  ONT van 3 to the Smith job" the second take's source was main stock and the policy could apply it with no click;
  now its source is asked for. The first take keeps the default.
- A configured `policy.default_source` no longer settles a source the text names with no marker word: a post that
  names a stock location word for word (its name, full path ("Shelves/B"), an alias, or "my truck") that is neither a
  destination of the post's movements nor the default ("pulled 2 ONTs shelf B to van 3", "van 3 gave me 2") marks the
  source as left out (T19). No recorded corpus post changes. A name that holds punctuation itself ("Cage (north)",
  "Shelf B: top") is found across it, as "pulled 2 ONTs cage (north) to van 3"; other names still stop at a comma,
  colon, bracket or full stop.
- The typo "form" now counts as "from" where a source is looked for (T19): "2 ONTs form shelf B to van 3" marks the
  source as left out when the reading has none, and a source read after it is on the source side. "the form", "a
  form" and "small form factor" stay nouns. No recorded corpus post changes.
- A reading that draws its fields from several clauses now waits for a person when one of those clauses names
  another stock location by its name, full path or alias, with no vehicle, job or site word (T6): "took 2 ONTs to
  van 3, Riverside Apartments got 4 drops" read as 2 ONTs and 4 drops to van 3. Before, only a vehicle, job or site
  word, a place word or a spare number made such a clause a rival. "for Riverside Apartments" says why and is
  none, nor is the movement's own source or destination, or its default source when it states no other.
- A take to a site is written with no click only when its destination is under one of the new `locations.sites`
  paths (such as `["Sites"]`). Unset, the default, every take to a site waits for Apply, and `check-setup` warns with
  writes on. A return read as a take to a site no longer puts stock back on a shelf unasked (#199, #210, ADR 0022).
  The catalog now reads each location's parent.
- A movement put together from the fields of two stated movements needs a person: "took 2 Disc 40 from main stock
  to truck 1" and "took 4 Disc 40 from receiving to van 3" read as 4 from main stock to truck 1 is no longer written
  with no click. A movement told over several clauses with no other place or number among them (a heading over a
  list) is unchanged (#200). Another vehicle named in one of its clauses counts too, with no "to" or number:
  "Truck 1:" over 2 Disc 40 then "van3:" over 4 Disc 60, read as one take of both to truck 1, waits. So does a job or a site
  named in one of them ("the Smith job got 4 Disc 60"), unless it says why ("for the Smith job").
- A post that holds another person's message (a reply in someone else's thread, read with the post) waits for an
  approver: its "my truck" would be read as the poster's truck, so the policy no longer writes it with no click.
- A take-out whose text has a return word ("returned", "brought back", "back to") needs a person: a model that
  reads a return as a take-out no longer gets it written with no click (#199).
- Post text quoted in a reply shows `*`, `_`, `~` and backticks in their full-width forms, so it cannot fake the bold
  name of a matched record. More wordings that instruct the connector ("ignore your instructions", "bot:", "skip the
  review") flag the post, and "at shelf B" names a source that a default source no longer fills (#201).
- A receipt of a plan whose writes went through only in part says "Partly applied" (it said "Check this"), names
  the movements that changed inventory, and says to post the rest again. A movement the plan never sent says so,
  and no longer claims it waits for review: a plan is approved whole, so no click can approve the rest of it.
- A length in its part's own unit ("50 m of drop cable" for a part stocked in m, "50 ft" for one in ft) asks no
  quantity question and may apply, for a part matched surely enough to write. Units are never converted: cm, mm,
  inches, any other unit, containers and anything that may multiply the length ("3 runs of 50 m", "2x 50 m",
  "twice") still ask. The part's units are read from InvenTree again right before
  the write, and a part stocked in another unit since the dry run goes back to review unwritten.

### Fixed

- Message text is read as typed: Slack sends "&", "<" and ">" as "&amp;", "&lt;" and "&gt;", and the connector
  passed them on. A reply quoted "R&D kit" as "R&amp;D kit", the rules never saw a typed "->" as an arrow, and the
  model was given the escaped text. Links and mentions keep Slack's own form.
- Text sent to an LLM provider now also has Slack webhook URLs, Google API keys, `Bearer` values, labels after an
  underscore (`access_token=`, `DB_PASSWORD=`) and international or space-separated phone numbers masked.
- The rules now see another person as the actor in more shapes, whatever the model says: names with accents
  ("José took"), "removed", "transferred", "has taken", "taken by Mike", "Mike and I took" and "me & Mike took".
  Such a take used to apply with no click when the model left out `actor_not_poster`.
- HTTP mode: a request whose `X-Slack-Request-Timestamp` is not plain digits ("1e3", or hundreds of digits) is
  refused with 401. It used to get a 500 and an ERROR log line with a traceback quoting the header.
- An Apply click now approves a plan its post's second or later dry run made, as after an answered question.
  The click compared the plan's revision with the dry run's, which are counted apart, so such a plan was refused
  as "no plan" and could not be applied at all.
- `stockthread trail` prints every character of Slack text that is not printable (control characters, DEL, C1
  codes, bidi overrides) escaped as `\uXXXX` (a surrogate pair above U+FFFF), so a post or answer cannot move the
  cursor or reorder the terminal line.
- A consume of serialized stock no longer reads "could apply": InvenTree answers a remove on a serialized item with
  success and changes nothing, so the dry run shows the items and a review reason instead.
- A question and the revisions of its post now expire at the same time, fixed by the post's first dry run
  (migration 0015). Before, an answer given late to a thread whose root was older than its newest message was
  recorded but never acted on.
- An answer taken just before its plan expired, whose revision ran after it, was shown as answered and then
  nothing came of it. That revision now posts one notice that the answer came too late (issue #75).

### Added

- `check-setup` warns when `inventree.url` is plain http to a host that is not this machine: the token goes with
  every request.
- `docs/architecture.md`: the process, the life of a post module by module, a map of every module and the
  rules the layout keeps; a test checks the map against `src/` and the rules against the code.
- `docs/configuration.md`: every setting with its default, the secrets and the rules across settings; a test
  keeps it in step with the code.
- Eval corpus 0.3.0: learned aliases in `catalog/aliases.json` carry `rivals`, and the corpus covers them (one
  matched with no question, one asked again because the catalog grew a rival).

- The Apply button is taken out of its dry-run reply once the plan is approved (by a click or the policy), replaced
  by a newer reply, closed with nothing written, or expired. The reply ends with a line that says which, and who
  approved it. Replies posted before this version keep their buttons; a click on them is still checked against the
  database. Database migration 0022 adds the `apply_buttons` table.
- An integration test of alias learning end to end on the pinned InvenTree servers: the worker as `stockthread run`
  builds it, from a post's question and Apply click to the next post applied with no click, and asked again once a
  new part fits the words.
- Alias learning (ADR 0012): once every request of a plan an approver clicked Apply on is verified, each of the
  poster's own texts in it that a person confirmed or corrected in the thread becomes an alias of its part or
  location, and the receipt names it ("Learned from this Apply: “norvane 40” means Norvane Disc 40 Access Point.").
  The next post that says so is an alias match. Only a text whose words fit that one record is learned, so a pick
  among several candidates, and a text the catalog does not back, teach nothing. A plan the write policy approved, a dry run, a write that did not read
  back as planned, text the model or a photo supplied, and texts with words like "my", "this" or "ones" teach
  nothing. An undo forgets what its plan taught; `stockthread aliases remove` forgets one by hand.
- Retention (design section 6, threat model T10): `storage.retention_days` (30) after a post is done with,
  the worker blanks its stored message text, the raw Slack events and dry-run results, keeping the rows, ids,
  states and audit log. Text a write, a read-back or an undo may still need is kept. Each expiry is audited.
- Confirmed aliases are stored (migration 0021, `Store.learn_alias`, only from a verified request) and the run loop
  matches with the aliases stored now (`catalog.with_aliases`). A learned alias is asked again when a record its text
  did not fit when it was confirmed fits now ("Disc 40" once a "Disc 40 Pro" exists; ADR 0012). Nothing learns an
  alias yet.
- `stockthread check-setup` warns about each stored alias whose part is inactive, deleted, a template or virtual, or
  whose location is structural or gone, by id: the matcher ignores it, and `aliases remove <id>` forgets it.
  The run log names their records once each time that list changes.
- `stockthread aliases list | export | remove <id>`: the stored aliases with the post each was learned from, as text
  or JSON, and their removal (audited as the admin's; the matcher stops using it at its next catalog read).
- With writes on, the policy approves a clear movement of a type in `policy.auto_apply` (by default a
  take-out) right after its dry run (ADR 0004, 0019): it is
  written, read back, and gets a receipt with an Undo button. Each plan is judged once, and only the plan a
  post's first dry run made: one made after an edit or a joined message waits for Apply.
- `stockthread trail <message link>`: one post's dry runs with every review reason and who raised it, its
  questions with their answers and who gave them, and its audit log. It only reads the database, and prints
  ids and codes unless `--text` is given.
- Source selection rule (`core/resolve.py`): which stock items a line takes. It considers only items that are
  OK and unallocated, prefers one item that covers the quantity, otherwise splits oldest first, moves
  serialized stock by serial number, and gives a review reason when batches or supplier parts differ.
- The dry run reads the stock of each settled line (part, place and quantity matched or answered) and shows
  the stock item ids it would act on; stock that does not settle the pick (none, too few, a choice that
  matters) is a review reason. A failed stock read makes the post wait, like a failed catalog read.
  The lines of one post share the stock they read: a later line picks from what the earlier ones left (only a
  shortage they cause says so), and a counted item that another line acts on, or an add into an item that an
  earlier line takes in full, is a review reason.

- `stockthread check-setup`: reads InvenTree and reports the server version, what the token's user lacks
  or has beyond the minimum roles, each `[vehicles]` path that is not exactly one stock location, and
  people who have posted with no vehicle set. Exit code 1 when something must be fixed.
- A question whose plan expired is taken out of its reply with a line that says nothing from the post
  will be applied, and it takes no answer from then on (migration 0014).
- Questions in the thread: an answer by button is acted on at once. A revised dry run applies every
  answer about the same reading, validates again and asks the next question; a "no" to a doubt
  about the reading says it was rejected and that nothing of it will be applied (migration 0013).
- HTTP mode for `stockthread run` (`slack.transport = "http"`): waitress serves the signed Events API
  and button clicks at `[slack.http]` `host`, `port` and `path`, with `manifests/slack-http.json`.
- Design, roadmap and architecture decisions (`docs/`).
- A synthetic eval corpus with a schema, a validator and a reference scorer (`eval/`).
- The Python package scaffold with one-command checks (`make check`).
- Typed configuration from a TOML file and environment variables, with a `check-config` command.
  Secrets in the file are refused, and secrets are redacted everywhere.
- The core domain model and the request state machine of the apply protocol.
- Synthetic Slack payload fixtures and signature test vectors.
- A read-only InvenTree client (GET only), with an API version check.
- Slack ingest checks: workspace, app, channel allowlist, subtypes, bots, duplicates, and the
  approver check for Apply, each drop with a reason.
- A rule-based extractor that needs no LLM. It reads the common one-message shapes and declines
  everything else, and it has zero hard failures on the eval corpus.
- A validator that derives review reasons by rule: flags that follow from the fields, grounding of
  items and quantities in the text, the quantity limit, contradictions, and questions, plans,
  negations, hedges and instructions in the text.
- The dry-run reply renderer: what would change, and every review reason in words, with post
  text escaped so it can never become a Slack mention or link.
- The durable Slack event queue in the store (migration 0002): store before acknowledging,
  pick up unfinished events at startup, and stop retrying an event that keeps failing.
- Integration tests against real InvenTree 1.4.3 and 1.5.6 in Docker, including characterization tests
  of the server behavior the design depends on.
- Grouping of Slack messages into posts (migration 0003): siblings from the same user within the
  window, thread replies with their root, in any arrival order; a message never changes post.
- A threat model, first version (`docs/threat-model.md`).
- The Slack listener on Bolt for Python, for Socket Mode and HTTP: payloads are checked, stored in the
  durable queue and only then acknowledged. HTTP requests are always signature-checked.
- `stockthread run`: connects to Slack over Socket Mode and posts dry runs until stopped. It checks
  the bot token, InvenTree and the catalog before connecting, and refuses a second instance on the
  same database. HTTP serving is not built yet. A rate-limited reply is tried again after the time
  Slack names, without blocking, and the catalog read has a time limit, so the instance lock never
  goes stale under a working instance.
- The store refuses a change of a row id under any of its names (`rowid`, `oid`), not only `id`
  (migration 0005).
- A Slack app manifest for Socket Mode (`manifests/slack-socket.json`) with the minimum scopes, and a
  quickstart in the README.
- Slack connections verify against the system's certificate authorities plus certifi's, so a
  python.org build of Python on macOS works without its "Install Certificates" step.
- The worker (migration 0004): stored events become grouped posts, and once a post's sibling window
  has passed it gets one dry-run reply in its thread. Posts that are not read as a movement get no
  reply. Clicks are recorded and not acted on yet; edits and deletions are noted whichever event
  Slack delivers first, and read as described under revised dry runs below. Each dry run keeps a
  record of what was decided and why (candidates, matches, reasons).
- The validator reads a quantity expression as a whole ("2 of the 5", "half a dozen") and checks the
  unit against the text: a swapped, missing or made-up unit needs review.
- An optional `[llm]` settings section (`base_url`, `model`, `timeout_seconds`; key in `LLM_API_KEY`).
  With it, a post the rule-based extractor declines is read by the model, and gets a dry run like
  any other. Without it, no post leaves the machine. A provider outage makes the post wait; after
  15 minutes it is left unanswered and its record says so (ADR 0011). A post the model refuses or
  does not finish is left unanswered at once, with `llm_no_reading` in its record.
- A `[vehicles]` settings section (Slack user id to stock location path): "my truck" in a post is the
  poster's vehicle. A path that is not found in InvenTree is named in the log and never guessed.
- `python -m stockthread.llm.record`: records a model's raw answers to the eval corpus, so a model is
  scored in CI without a provider. A recording of `anthropic/claude-haiku-4.5` on prompt `llm-0.2`
  is in `eval/recorded/`; behind the rules it has no hard failure on corpus 0.2.0.
- The review reason `photo_not_read`: every movement of a post with a photo waits for a person,
  because no extractor sees photos. With the default policy, a clear take-out with a photo is
  confirmed rather than applied.
- Prompt `llm-0.2`: a movement that a later message takes back ("scratch that") is no movement.
- An LLM extractor behind a provider interface: a versioned prompt,
  a strict output schema (output that does not fit declines the post), masking of strings that
  look like credentials before text is sent, and an adapter for OpenAI-compatible endpoints.
- Catch-up after downtime: posts made while the connector was down or disconnected are read from
  the channel history (up to `slack.catch_up_hours`, and never from before the connector first
  watched the channel) and answered with the review reason `late_ingest` (migration 0006).
- Revised dry runs (migrations 0007 to 0009): a message that joins a post after its dry run, or an
  edit or deletion of one of its messages, makes the post read again. A different reading posts a
  *Revised dry run*, the same reading posts nothing, and no movement any more posts one notice
  that the reply above no longer stands. A post is read as its messages are now: each as its newest
  edit shows it, a deleted one left out, and a change that cannot be read withholds the post.
