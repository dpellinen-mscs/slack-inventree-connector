# Design

Status: working design, 2026-09-29. Shape, stack and default policy are
decided ([0003](decisions/0003-standalone-python-service.md),
[0004](decisions/0004-default-write-policy.md)). Sections marked
**(decision pending)** still wait for an answer from Dan.

InvenTree and Slack behavior described here comes from reading
documentation and source code. It becomes a fact for this project only when
a test proves it (section 11).

Background research, with sources, is in [`research/`](research/):
[InvenTree](research/inventree.md), [Slack](research/slack.md),
[prior art and stack](research/prior-art-and-stack.md).

## 1. Goal

Turn informal Slack posts about stock ("2 Disc 40 on my truck") into correct,
auditable InvenTree stock movements, with a human in the loop exactly where a
human is needed.

The ranking of outcomes drives every tradeoff below:

1. A correct movement, applied once.
2. No movement, and a clear question in the thread.
3. No movement, silently. This is drift, the problem we set out to fix.
4. A wrong movement. **This is worse than all of the above.**

## 2. Pipeline

```text
Slack event
   │
   ▼
┌─────────┐   ┌─────────┐   ┌──────────┐   ┌───────┐   ┌─────────┐
│ Ingest  │──▶│ Extract │──▶│ Validate │──▶│ Match │──▶│ Resolve │
└─────────┘   └─────────┘   └──────────┘   └───────┘   └─────────┘
 verify        LLM or        contradiction   parts and    concrete
 allowlist     rules,        checks, text    locations,   stock items
 dedup         strict        review          no model     and serials
 group         schema        reasons
                                                              │
   ┌──────────────────────────────────────────────────────────┘
   ▼
┌────────┐   ┌───────┐   ┌─────────┐   ┌────────┐   ┌───────┐
│ Review │──▶│ Plan  │──▶│  Gate   │──▶│ Apply  │──▶│ Learn │
└────────┘   └───────┘   └─────────┘   └────────┘   └───────┘
 question     exact       switch AND    idempotent   confirmed
 in thread    API         policy AND    write,       corrections
              request,    validation    read-back,   become
              dry-run                   receipt,     aliases
                                        undo
```

Every stage is a function from typed input to typed output. Four stages
touch the outside world: Ingest and Review talk to Slack, Resolve reads
InvenTree, and Apply writes to InvenTree. Extract, Validate, Match, Plan
and Gate are the pure core and are tested without network access. Match
works on a catalog snapshot that Resolve refreshes. Every check that needs
live stock data belongs to Resolve.

| Stage | Input | Output | Can fail to |
| --- | --- | --- | --- |
| Ingest | Slack event | Post (grouped messages) | Ignored, with a reason |
| Extract | Post | Candidate movements | `unclear` candidate |
| Validate | Candidates | Candidates with review reasons | Review |
| Match | Item and location text | matched, ambiguous or unmatched | Review |
| Resolve | Matched candidate | Concrete stock items | Review |
| Review | Review reasons | One question in the thread | Waiting |
| Plan | Resolved candidate | Exact API request | |
| Gate | Plan, policy, switch | Allowed or a dry-run | Dry-run |
| Apply | Allowed plan | Verified movement | Failed, reported |
| Learn | Confirmed correction | Alias | |

## 3. Architecture options

### Option A: standalone service

A separate process next to InvenTree. It talks to Slack and to the InvenTree
REST API, and keeps its own small database.

- Good: independent of the InvenTree server process and version. A crash or
  a slow LLM call cannot hurt the inventory server. Socket Mode gives a
  deployment with no public endpoint. It uses the same permission checks and
  stock history as any other API user.
- Bad: one more container to run and configure. It is not visible in the
  InvenTree plugin list.

### Option B: InvenTree plugin

A plugin that runs inside the InvenTree server.

- Good: one install, settings in the InvenTree UI, direct access to models.
- Bad: tied to server internals, which change between releases. A long-lived
  Slack connection and slow LLM calls do not fit a web worker. Receiving
  Slack events over HTTP would mean exposing InvenTree to the internet.
  Plugin code runs with full database access, so a bug has a larger blast
  radius, and direct model access can bypass the API's checks. Integration
  tests are harder to write.

### Option C: core library with thin adapters

A pure core (Extract, Validate, Match, Plan and Gate) with adapters for
Slack, InvenTree and storage. Shipped as one service.

- Good: the core is tested without network access. Another chat platform, a
  CLI or a small plugin can reuse it later. This matches the shape of the
  pipeline.
- Bad: abstraction can grow beyond what one service needs. The answer is to
  keep it to one package, with adapter interfaces only where a second
  implementation exists or is planned.

### Decision

Option C, deployed as in option A: one Python package, one container, a pure
core and three adapters ([0003](decisions/0003-standalone-python-service.md)). A thin InvenTree plugin can come after v1.0 if
users ask for settings inside the InvenTree UI.

### Language

| | Python | TypeScript |
| --- | --- | --- |
| Slack SDK | Bolt for Python, maintained, async | Bolt for JS, maintained |
| InvenTree fit | Same language as InvenTree and its client | None |
| Contributors | InvenTree users already run Python | Extra runtime for most users |
| Fuzzy matching, data tools | Strong | Adequate |
| Typing | Type hints with a strict checker | Native |

Decision: Python. The users and likely contributors come from the
InvenTree community, and Bolt for Python covers everything the connector
needs.

## 4. Slack side

- **Transport.** Socket Mode is the default: an outbound WebSocket, no
  public URL and no TLS setup, which suits a small team behind NAT. HTTP
  with the Events API is supported for teams that prefer it. The listeners
  are the same, and only the entry point differs. The repository ships one
  app manifest per transport. `stockthread run` serves either. Over HTTP,
  waitress serves Bolt's WSGI adapter on a thread of the same process, at
  `[slack.http]` `host`, `port` and `path` (default
  `127.0.0.1:3000/slack/events`, behind a reverse proxy with TLS; ADR
  0013). It refuses bodies over 4 MiB and answers 404 to anything but a
  POST to the path. At startup it
  takes the instance lock (another instance on the same database refuses
  to start), puts back events an earlier instance left unfinished, checks
  the bot token (`auth.test`), the InvenTree version and a first catalog
  read, and only then connects or listens. A startup failure (a revoked token, no
  network, InvenTree down) exits with code 2 and names only the error
  code or class. The worker runs in the main thread. It beats the lock's
  heartbeat between startup phases and before each batch and each post,
  answers at most 20 posts per step, and the lock goes stale only after
  `4 × inventree.timeout_seconds + 60` seconds (at least a minute).
  Nothing blocks longer than that under the lock. A reply is posted on a
  helper thread with a 30-second deadline (a socket timeout does not bound
  a call: several addresses, a dripping body, DNS). A call that runs out
  of time is left running, its result is taken by the next try and never
  posted again, and nothing else is posted meanwhile. A rate-limited reply
  does not sleep: every reply waits until the time in Slack's
  `Retry-After` has passed. A catalog read is cut off 30 seconds before
  the lock could go stale (an outage like any other; a large catalog on a
  slow server needs a higher `inventree.timeout_seconds`). The worker
  checks the lock and beats it right before it posts, so an instance that
  lost it never posts a reply the new holder posts too. An
  outage makes it back off up to a minute, and a lost lock stops the
  process with code 1. The first SIGINT, SIGTERM or SIGHUP stops it
  cleanly, between posts; a second one stops it at once.
- **Signature verification** applies to HTTP, for events and for
  interactivity payloads. It is always on and has no switch. In HTTP mode
  the connector refuses to start without a signing secret. Tests cover a
  valid signature, a wrong secret, a changed body, a stale timestamp and
  missing headers.
- **Identity checks.** Every payload must carry the configured workspace id
  and app id. Anything else is dropped.
- **Acknowledge first.** Slack expects an answer within 3 seconds. The
  listener stores the raw event, acknowledges, and processes it later.
  `slack/listener.py` runs the ingest checks inside the Bolt handler, which
  runs before the acknowledgement (`process_before_response`). An accepted
  payload is stored first; if storing fails, the handler raises and Slack
  delivers the event again. A dropped payload is acknowledged and never
  stored. A click has no event id, so its queue key is built from the
  channel, the message, the user and the click's own `action_ts`.
- **Durable queue.** The queue is the `slack_events` table with a
  processing status. Rows that were not finished are picked up again at
  startup, so a restart loses nothing that was acknowledged. A row moves
  `pending` -> `processing` -> `done`, `dropped` or `failed`, and back to
  `pending` only at startup. An event claimed 3 times without finishing is
  `failed`, so one bad payload cannot crash the connector in a loop. Only
  the holder of the instance lock claims and finishes events, and startup
  requeues only events an earlier instance left behind. Delivery is **at
  least once**: after a lock takeover, an event the old instance was still
  processing runs again, so processing checks the lock before any write,
  and everything downstream is idempotent (post keys, request keys,
  markers). Triggers keep the key and the raw body fixed (retention may
  expire the body only of a finished event), keep attempts from going down,
  move the claim only with the status, freeze finished rows, and refuse
  deletes and illegal status changes. An outcome is a short code, never text.
- **Deduplication.** Slack retries deliveries. Events are deduplicated on
  `event_id`, messages on `(workspace, channel, ts)`, and writes on the
  request key (section 7).
- **Filtering.** The first check in every listener is the channel
  allowlist. An empty allowlist means the connector acts nowhere. Message
  subtypes are handled by allowlist: plain messages, file shares and thread
  broadcasts, plus edits and deletions, which become their own values and
  never write by themselves. Any message with a `bot_id`, or an edit of
  one, is dropped, which also stops the connector from reacting to its own
  posts. `slack/ingest.py` implements these checks as a pure function that
  gives every drop a reason. The deduplication there is in memory; the
  durable queue takes it over.
- **Shared channels.** A message or click from a user of another
  organization (Slack Connect) is dropped as `wrong_workspace`. Enterprise
  Grid org installs are not supported yet: `workspace_id` must be a `T` id.
- **Clicks at ingest.** Only the connector's own buttons on a message are
  accepted. Only an approver may click Apply, and ingest drops any other
  user's Apply click. Other buttons (answers, corrections, Undo)
  may come from the poster, so ingest passes them on with the user id, and
  review checks the user against the stored button row.
- **Catch-up after downtime.** Slack does not replay events that were
  missed while the connector was down or disconnected. `slack/catchup.py`
  reads the history of each allowlisted channel right after the connection
  is made and every 15 minutes after that, which also covers a lost
  connection, on a thread of its own. A message the connector does not
  have is stored in the durable queue as the event Slack would have sent,
  under the id `history:<channel>:<ts>`, and from there it is an event like
  any other: the same ingest checks, grouping and dry run. A post with such
  a message carries the review reason `late_ingest`, whatever its age, and
  is never applied automatically. Without this step, downtime would cause
  silent drift.
  - **How far back.** Up to `slack.catch_up_hours` (default 72; 0 turns
    catch-up off), and never behind the first sweep of the channel
    (`catch_up` table, migration 0006). A new installation, or a channel
    new to the allowlist, therefore does not answer the posts that were
    there before it. A channel taken off the allowlist and put back is
    caught up as after downtime. A post older than `plan_expiry_hours`
    (default 24) is stored and withheld as too old, without a reply.
  - **Threads.** `conversations.history` lists only top-level messages. A
    thread whose newest reply is later than the last completed sweep (less
    5 minutes) is read with `conversations.replies`. The listing of roots
    reaches behind the channel's first sweep, so a new reply under an older
    root is found, though that root is not stored. A reply under a root
    older than the window is not found.
  - **Not twice.** A message already in a post is skipped, and the queue
    key stores a history message once. Sweeps after the first leave the
    newest 2 minutes alone, since those events may still be on their way.
    If both the event and the history copy are stored, the message is
    placed once, from whichever is handled first.
  - **A channel at once.** What a sweep finds in a channel is stored in
    one transaction, together with the mark, after the history and the
    threads are read. The worker answers no post while events are
    pending, so a post from the history is never answered before its
    siblings and replies the sweep found are stored. A sweep that
    fails or is stopped halfway stores nothing for that channel. A
    thread Slack refuses to list is left out whole (root and replies);
    the rest of the channel is stored and the mark stays, so the next
    sweep reads that thread again.
  - **Edits and deletions.** A message found already edited is read as it
    is now. If the history shows another edit than the stored event (or a
    tombstone), the edit is stored as an event and noted like one Slack
    delivered. The worker does the same when a history copy that shows an
    edit arrives for a message already placed from its original event. A
    post read with an edit found this way carries `late_ingest`. A
    deletion made during downtime is not seen: the message is just absent
    from the history. Plan revisions (M2) re-read a post before a write.
  - **Known limits.** A message that joins a post after its dry run (a
    thread root found by catch-up after its reply was answered alone, or
    a reply posted within seconds of the point a sweep ends at, when its
    event is lost too) is answered by a revised dry run (see "Plan
    revisions"). A sibling found that way starts a post of its own. The
    first start after upgrading from a version without catch-up is a
    channel's first sweep, so that one downtime is not read. The mark is
    the host's time: a clock more than a few minutes ahead of Slack's can
    hide a reply from the next sweep.
  - **Failures.** A channel that cannot be read (the app is not in it, a
    rate limit, an answer not in Slack's form, more than 50,000 messages
    in the window) is logged by Slack's error code or the error's class,
    the other channels are still read, and it is not marked as swept, so the next sweep reads the same range again. Nothing here
    needs the instance lock: a sweep only stores what it read.
  - **Rate limits.** The two methods are Tier 3 for an app that a
    workspace creates for itself from the manifest, which is how this one
    is installed. A sweep costs one call per 200 messages in the window,
    plus one per thread that moved.
- **Grouping.** A photo with a caption arrives as one message. Sibling
  messages from the same user within a short window (default 5 seconds),
  and replies in the thread, join the same post. A message belongs to
  exactly one post. The post key is fixed when the post is created and is
  never recomputed, because Slack does not guarantee the order of events.
  Sibling grouping closes when the dry-run reply is posted. A thread
  reply after that creates a new plan revision. The rule is
  `core/grouping.py`; `Store.place_message` runs it inside one transaction
  under the instance lock. A reply that arrives before its thread root
  creates the post keyed on the root's timestamp, and the root joins it
  when it arrives. The window is compared exactly, to the microsecond.
  The window is measured from the nearest placed message, so siblings
  chain (0 s, 4 s, 8 s is one post). Which messages end up together can
  therefore depend on arrival order (0, 8, 4 gives two posts; a sibling
  whose reply arrived before it keys its own post), but a message never
  changes post once placed, and a reply always joins its root. Timestamps
  must be in Slack's exact form, because keys compare as strings.
- **Plan revisions.** Every plan has a revision number and a hash of the
  messages it was built from. An edit, a deletion or a new message in the
  thread makes the revision stale: its buttons are removed and a new plan
  is built. An approval names one revision and succeeds only if that
  revision is still current. Plans expire (default 24 hours).
  - **Revised dry runs (built).** Every dry run records the newest
    message it read (`read_through`; migration 0007). A message that joins
    the post later (a thread reply, or a root that arrives after its reply
    was answered alone) makes the post due again, as revision 2, 3, and so
    on (`dry_run_revisions`), under the same window and grace. The whole
    post is read again, and the result is compared with the reply that
    stands in the thread, if any:
    - the same movements, matches and reasons: `unchanged`, nothing is
      posted, so a "thanks" under a post the model reads the same way
      stays quiet (`late_ingest` is left out of the comparison: it says
      when a post was read, not what it says);
    - different: a *Revised dry run* reply is posted (`replied`);
    - no movement any more (declined, not a movement, withheld because a
      message was edited or deleted or cannot be read, or failed): one
      notice that the dry run above no longer stands (`retracted`). After
      that nothing stands, and later revisions without a movement post
      nothing.
    When the LLM provider gave no answer or no reading, the notice says
    that, not that the thread changed. The rule-based extractor declines
    every post of more than one message, so without an LLM extractor the first thread reply under an
    answered post takes its reply back. That is the safe reading: the
    reply may have changed the movement. A post whose plan is older than
    `plan_expiry_hours` is not revised at all, whatever the age of the new
    message: its reply has expired with its plan, so there is nothing to
    take back. A post's plan expires from one time, fixed by its first dry
    run (`posts.plan_from`, migration 0015): the newest message that dry
    run read, which is what its question counts from too. The message is kept in the post. This also
    bounds what each step reads. One exception: an answer taken just
    before the plan expired whose revision runs after it (it waited
    behind another reply, the model, or downtime). That revision posts a
    notice once, "The answer came as its plan expired: nothing of it
    will be applied", so the thread never shows an answer that nothing
    came of (issue #75). Such posts are found from the answer side: each
    answered question that the post's dry runs have not read yet, of a
    plan that expired within the last plan window. Later than that
    (downtime, or an upgrade over answers stranded before this fix), the
    thread has gone quiet and is left so.
  - **An edit or a deletion alone** after the dry run starts a revision
    too. Every dry run also records the newest noted edit or deletion of
    its post it was decided with (`changes_through`, a
    `revised_messages.id`, 0 for none; migration 0008), taken before the
    post is read. A change noted later makes the post due at once: an edit
    is no sibling, so there is no window to wait for. Every edit and
    deletion event is noted (migration 0009; before it, only the first of
    each message), so each later edit starts a revision too.
  - **Reading edits.** A post is read as its messages are now, as of the
    newest change it was decided with: each message as the edit with the
    newest edit timestamp shows it (Slack does not keep event order, so an
    older edit delivered late changes nothing), and a deleted message left
    out (it is still counted as read, so it starts nothing more). This
    holds for the first dry run as well as for revisions: an edit before
    the dry run is read in it. A post is never read from a message's
    original text once a change of it is noted. When a change cannot be
    read (a tombstone, an edit without a stamp or by another user, two
    edits with one stamp that disagree, a body retention expired, a note
    from before migration 0009, which may not be the newest edit, or an
    edit of a thread root the post does not hold), or every message was
    deleted, the post is withheld as `changed`: a standing reply is taken
    back with a notice, and without one nothing is posted. A deleted
    message is never read again, whatever edit comes after it. An edit
    that reads as the same movement is `unchanged` and posts nothing. An
    edit that arrives while a reply's Slack call is still running does
    not change what that reply recorded; it starts the next revision.
    Catch-up notes an edit it finds in the history for any post, not
    only one waiting for its first dry run, unless the stored event or a
    noted edit already shows that stamp. Posts answered before
    migration 0008 take every change noted until then, so an upgrade
    never answers an old edit.
    Known limits: posts waiting for their first dry run are answered
    before revisions, so a backlog delays revisions. A first dry run that
    ended `gave_up` may still have reached Slack (the call can fail after
    Slack took it); a revision treats it as "no reply stands".
- **After apply,** an edit, a deletion or a correction never writes by
  itself. It creates a review item that offers an undo. A revised plan can
  be approved only after the undo has been verified.
- **Worker.** `worker.py` runs under the instance lock, in two steps.
  `drain` claims stored events, runs the ingest checks again with the
  current settings, and places each new message in its post; the message
  keeps a link to its stored event, whose raw body is the only place its
  text is kept. An event that raises fails alone, with its error class as
  the outcome. Only once the queue is empty does `reply_due` run, so a
  backlog never splits siblings. A post is due when its newest message is
  older than the sibling window plus 10 seconds of grace, and, when it is
  keyed on a thread root that has not arrived, 6 minutes after the root
  (Slack retries a delivery after about 1 and 5 minutes). The worker then
  extracts, validates and matches, closes the post to siblings, posts the
  dry-run reply in the thread, and records the outcome once
  (`posts.dry_run_at`, `posts.reply_ts`, audit `dry_run_<outcome>`;
  migration 0004):
  - `replied`. A post answered more than 10 minutes after its newest
    message carries `late_ingest`, and so does a post with a message that
    catch-up found in the channel history.
  - `silent`: declined, or not a movement. No reply by design.
  - `withheld`: a message cannot be read again (its body expired), has a
    change that cannot be read or every message was deleted (`changed`,
    see "Reading edits"), or the post is older than `plan_expiry_hours`.
    A post is never read with a message left out. Every edit and deletion
    is noted by message key (`revised_messages`), so one that Slack
    delivers before the message it changes still counts when that message
    arrives. The note is read from the raw event, so it also covers a root
    that never arrives and the edits the strict checks cannot read in full
    (a deleted thread root becomes a tombstone; an edit without a stamp or
    a user). The listener stores those too, as `Revised`, after the same
    workspace, app, channel and bot checks.
    A link preview or a new thread reply changes neither text nor files
    and is not an edit.
  - `failed`: the pure pipeline raised on this post. It is logged by error
    class and line, never with a message or a traceback, and it cannot stop
    later posts. A catalog or database failure is an outage instead: it
    propagates, and the post waits.
  - `gave_up`: posting kept failing for 15 minutes, with backoff up to a
    minute between tries.

  A reply whose Slack call has not returned within its deadline is not a
  failure yet: the call may still send. Its post keeps the decision that
  was handed to Slack and stays open (not decided again, not given up on)
  until the call has ended, and nothing else is
  posted meanwhile. The record is then what the reply shows.

  The outcome is stored with what was decided and why (`dry_runs`, in the
  same transaction): the extractor's name and version, the candidate
  movements, every match with its rule and InvenTree ids, the validator's
  reasons and the reasons the reply showed. A withheld post records why,
  and a failed one the error class. The result quotes words of the post,
  so it is never logged, and retention can expire it.

  A crash between posting and recording posts the reply again: a
  duplicate dry run beats a silent drop. Clicks on a question's buttons
  record its answer (below); other clicks are recorded and not acted on
  yet. Edits, deletions and thread replies after the dry run make
  revisions (above).
- **Replies.** The dry-run reply (`slack/replies.py`) lists each movement
  with the matched part and location names next to the words as written,
  then every review reason in plain words. Text from the post and from the
  catalog is escaped, so neither can become a mention or a link. Candidate
  lists are capped at 5 in the display, and long sections are cut to fit
  Slack's limits; a reply shows at most 20 movements (Slack allows 50
  blocks), then says how many more there are. "Could apply" is printed only
  when `gate_reasons` finds nothing: the caller's reasons plus every reason
  the view itself shows (an unmatched part or place, a missing quantity).
- **Interactivity.** Questions and receipts use Block Kit in the thread.
  A button carries only a random id of at least 128 bits that points to a
  row in the connector's database. The handler checks that the channel and
  the message in the payload are the ones stored with that id. Buttons are
  removed once a decision is made.
- **Questions.** A post with review reasons gets one focused question at
  a time in its thread (`core/questions.py`, pure: wording and buttons
  belong to the adapter). The answer re-runs validation, and the next
  question follows. The question is the first in this order of tiers,
  across all movements of the post, earlier movements and lines first:
  1. doubts about the whole reading (instructions in the text, a
     contradiction, a correction, words not in the text, a hedge or a
     plan): "Is this right?", yes or no, since no detail answer can
     clear them, and details asked first would be wasted;
  2. an unclear movement type: one button per type;
  3. items: a missing item or an unmatched part is named in a reply; an
     ambiguous part with at most 5 candidates is a button choice, with
     more it is a reply; a part found by the words or fuzzy rule is
     confirmed, yes or no;
  4. quantities (missing, vague, a container or a pack unit): a reply;
  5. the source, then 6. the destination, the same way as parts (an
     implied place or an unmapped vehicle is named in a reply);
  7. serial numbers: a reply;
  8. stock (a choice that matters, none, or too few): a choice of
     stock items, or "read the stock again" (below);
  9. reasons only an approver clears (late, above the limit, someone
     else as the actor, stock changed): yes or no.

  Every reason code belongs to a tier, so every reason leads to a
  question, and a post with no reason gets none (checked on the corpus).
  A question of the last tier is marked for approvers only, and the
  stored button row keeps that mark. Its buttons are `answer-0`,
  `answer-1`, ... (Slack wants different action ids in one block), each
  carrying a stored button id; ingest reads them all as `answer`. Labels
  that would read the same get the record's key, and a long location path
  is cut from the left so the place itself stays visible.
  - **Asked in the reply (built).** The question goes below the dry-run
    reply, after a divider, in the same message: the reply takes at most
    42 blocks and the question 3, within Slack's 50. The notification text
    stays the reply's own. The question and one row per button (a random
    128-bit id and the answer it stands for) are stored in the same
    transaction as the reply's record (`review_items`, `interactions`;
    migration 0010), so no button a reply carries is without its row, and
    no question is posted apart from the reply it belongs to. Button ids
    are made once per decision: a reply still on its way to Slack is
    checked on, and stored, with the same ones. A reply posted again after
    an error or a restart gets new ones, so the buttons of a copy Slack may
    have posted before match no row (the click handler says so). A reply
    that was given up on stores none. A reply that Slack posted without a
    usable timestamp keeps a question that no click can fit. A question
    that cannot be put (a bug in its wording) is left out, and the reply
    is posted without it. The question is stored as codes, indexes and
    primary keys, never words of the post. A post has at most one open
    question: a revised reply, or a notice that the reply no longer stands,
    makes it stale. An unchanged revision leaves it open, also when only
    the lateness differs (`late_ingest` is not compared; the reply that
    stands, and its question, were read on time). Open does not mean
    current: a plan that has expired is not revised, so its question stays
    open, and a click is checked against expiry as well. Its buttons are
    taken down (below), and from then on it takes no answer. The stored
    `approver_only` column is the mark a click trusts.
  - **Taken down once closed (built).** A question that is answered,
    replaced by a newer reply, or whose plan expired while it was open, is
    taken out of the reply it was asked in.
    The divider above a question carries the block id `question`. A step
    of the worker, once the queue is empty, reads the reply back from
    Slack (`conversations.replies`, bounded to that one message), keeps
    every block before that divider, and edits the reply
    (`chat.update`) to end with one line: "answered by @user",
    "replaced by a newer reply", or "expired, so nothing from this post
    will be applied; to record it, post it again". Expiry is judged by the
    same step, from the same time and by the same comparison as a click
    (the plan's `expires_from` plus `plan_expiry_hours`; at that very
    instant it has not expired yet). The question is marked `expired_at`
    (once, only while open; migration 0014) with an audit row, and an
    expired question takes no answer even if `plan_expiry_hours` is raised
    later, because its buttons are gone. The text of the reply passes through
    memory only; nothing of it is stored or logged. One reply is edited
    every 2 seconds at most, fewest failed tries first, within the 50 a
    minute that each of the two Slack methods allows (`conversations.replies`
    is limited to 1 a minute only for apps distributed outside the Slack
    Marketplace, not for a team's own app). A reply that is gone, that
    this app did not post, or that has no marked divider is left as it
    is. A failed attempt counts one try for its question and pauses all
    take-downs for 1, then 2 minutes, or as long as Slack's Retry-After
    asks; a call that could not start because another is still on its way
    counts nothing. After 3 failures the connector gives up on that
    question. An edit whose answer was lost is seen on the next
    read (the reply already ends with its line) and counts as done. Where
    each question's buttons stand is stored (`review_items.buttons`:
    shown, removed or left, with a count of failed tries; migration
    0012), so a restart does not edit a reply twice or forget one.
    Questions closed before that migration are left. Slack sends the
    connector's own edit back as a `message_changed` event; an edit of a
    message that names this app (a bot id, and this app's id) is never a
    revision. Clicks are checked against the
    store whatever a reply shows, so a button that is still visible
    answers nothing.
  - **Answered by button (built).** The worker reads a click with the
    ingest checks again, then against the stored button: the click's
    channel and message must be the ones stored with it, its question
    open (not stale, not answered), and its plan not expired. Who may
    answer and when the plan expires are stored with the question when it
    is asked (migration 0011), because both could move later: the poster
    is the author of the oldest message the dry run read, and expiry
    counts from the post's one plan time (`plan_from`, fixed by the first
    dry run from the newest message it read; migration 0015), the same as
    its revisions. A click and a revision therefore count a plan's expiry
    from the same time.
    Questions asked in a revision before migration 0015 count from the
    post's own time, which is never later. The user must be an
    approver, or for a question that is not for approvers only, that
    poster. The answer (the button's position and choice) and who gave it
    are recorded once, as a compare-and-set on the open question, with an
    audit row by `human`. In the same transaction the post must not have
    moved on from the dry run that asked: no message joined it and none
    was edited or deleted since its newest dry run, and every dry run
    after the one that asked was `unchanged`. A click that comes while a
    revision is pending answers nothing. Every refusal finishes the click
    as dropped, with its reason (`unknown_button`, `other_message`,
    `stale`, `already_answered`, `expired`, `not_allowed`,
    `post_changed`). Expiry is judged when the click is handled, so a
    click that waited out an outage in the queue fails closed.
  - **Told to the clicker (built).** Once a click is finished, the clicker
    alone gets an ephemeral note in the thread (`chat.postEphemeral`,
    covered by `chat:write`): that the answer is recorded, or why it is
    not (out of date, replaced by a newer reply, already answered,
    expired, approvers only, poster or approver only, the post changed).
    The notes are fixed sentences that quote neither the post nor the
    catalog (`replies.CLICK_FEEDBACK`). A payload the ingest checks drop,
    and a button that does nothing yet, get no note. A note is best
    effort: it is sent after the event is finished, with its own 5 second
    deadline and one note in flight at most. One that fails is logged by
    error class and not tried again, and no note is sent for a minute
    after it, so a slow Slack never holds the queue up. A crash between
    the two loses the note, never the answer; a click handled again after
    its answer was recorded tells its own clicker that the answer stands.
  - **Acting on an answer (built).** An answer is a fact about one
    reading of the post, and it is applied to that reading only:
    - *Scope.* A click is recorded only while the post has not moved on
      from the dry run that asked. Every later reading is compared with
      that one (`_same_reading`, lateness left out). While they are the
      same, all answers given about it are replayed onto it, oldest first.
      A reading that differs (an edit, a new thread message read
      otherwise) starts over: earlier answers are kept in the audit trail
      but no longer applied, and its own first question is asked.
    - *Effects.* A part or location picked from a list, or a loose match
      confirmed with "yes", becomes a matched line (`source: answer`).
      "None of these" or "no" makes it unmatched, and the next question
      names it (a thread reply). A movement type picked replaces an
      unclear one, and validation runs again on the result, so a type
      that contradicts the places is a new question. "Yes" to a doubt
      about the whole reading, or to an approval-tier question from an
      approver, clears the one reason that question was asked for (its
      code and detail, on that movement and line); another doubt with the
      same code, or an unclear type, is still asked about. "No" to any
      yes-or-no question about the reading ends the plan: the revision says
      the reading was rejected and that nothing will be applied, and asks
      for a corrected post or thread reply.
    - *Then.* An answer makes the post due for a revision at once (no
      sibling window). The revision applies the answers, runs validation
      and the matcher's gate again, and posts the revised dry run with
      the next question, or with "could apply" when none is left. Its
      record lists the ids of the answers it applied. The answered
      question's buttons are taken down as above.
    - *How (built).* Each record keeps `reading`, a digest of what it
      read before any answer (lateness left out), and the ids of the
      answers it `applied`, those it `ignored` and the one that `rejected`
      the plan. The answers replayed are those to questions asked by the
      newest records in a row whose `reading` is the current one, up to
      the newest answer known when the post was read (`answers_through`,
      migration 0013; answers from before it are not acted on). A
      revision with only answers new keeps the reading that stands
      (`extracted` keeps its types before answers), so a model is not
      asked again and cannot read the same words otherwise. A part or
      location answer is a match by the `answer` rule. "Yes" clears a
      reason only for a doubt about the whole reading or an approval
      question, and only on the line the question named (a reason
      about another line is asked next); a "yes" to a stock question
      clears nothing until source selection asks its own. "No" to any
      yes-or-no question about the reading ends the plan. An answer that does not fit its question
      (a record not offered, a line the reading lacks) is ignored. A new
      answer always posts a reply, even one that changes nothing, so the
      next question is asked.
    - *Limits.* An answer never authorizes a write: the write gate of M3
      checks the resolved plan again, refuses a record with `rejected`,
      and an approval answer counts only
      from a user who is an approver when it is applied, not only when it
      was clicked. A confirmed match becomes an alias only after the plan
      is applied and verified ([0012](decisions/0012-first-confirmation-becomes-alias.md)).
      Answers given as thread replies follow the same rules once they are
      read (below).
  - **Answers by thread reply (designed; the reading rules are built, the
    worker does not use them yet).** Four questions have
    no buttons: which part (`name_part`), which place (`name_location`),
    how many (`quantity`) and which serials (`serials`). The person
    answers them by writing in the thread. Which questions take one and
    what reads cleanly (below) are `core/typed_answers.py`.
    - *Which questions take one.* Only a question that names one line
      (or, for `name_location`, one side of the movement). A question
      that names no line takes no typed answer: "How many of each item?"
      on a post with several lines, or "Which item was moved?" on a
      reading with no lines (a photo-only post). Every reply to it is a
      thread message and is read with the post, so the model reads
      "3 and 2" or "two of the patch cords" with the post's own words.
    - *What counts as an answer.* Messages in the thread are judged in
      `ts` order. A message counts as the answer to the open question
      only when all of these hold. It is in the post's thread and is not
      from a bot. It was posted after the reply that asked (its `ts` is
      greater than the question's `message_ts`). Its author may answer
      that question, the same rule as for a button. It is the first
      message that meets these conditions and reads cleanly: a message
      before it that does not read cleanly uses up nothing. A question
      gets one answer, and one message answers one question. And it
      *reads cleanly as an answer* to that ask:
      - `quantity`: the whole message is one number, as digits or as a
        word the rules already read ("3", "three"), greater than zero
        (zero only for a count). A piece word may follow ("3 pcs",
        "3 each"); so may the line's own unit when that unit is not a
        container ("40 ft" for a cable line in feet). A container word
        ("2 boxes", "a case of 12") does not read cleanly: v1.0 never
        converts packs (section 5). Nor do "3 or 4", "a few" and
        "3, and 2 to truck 2".
      - `name_part`: the matcher, run on the whole message as the line's
        item text, gives a part matched by a rule that may authorize
        (exact, normalized or alias), or a short list of candidates
        (at most `MAX_CHOICES`). A loose single match also counts; the
        next question then confirms it, as for any loose match.
      - `name_location`: the same, with the location matcher and the side
        the question named.
      - `serials`: the message holds only serial-shaped tokens separated
        by spaces, commas or new lines, as many as the line's quantity,
        no token twice. A token is serial-shaped when it is 4 to 40
        characters of letters, digits and `-`, `_`, `/`, `.` or `:`, and
        holds at least one digit, or when it is a MAC address (12 hex
        digits, bare or split by `:` or `-`). A token that the part or
        location matcher reads as a name (exact, normalized or alias)
        is not serial-shaped. So "ok", "thanks", "done", "3" and a part
        name such as "U7-Lite" do not read cleanly, whatever the line's
        quantity. The eval set holds these as cases that must not read
        as serials.
    - *Anything else is a thread message, as today.* A message that does
      not read cleanly ("ok thanks", "one sec, checking", "actually it was
      truck 2") is read with the whole post, so a correction is never lost
      to a parser that took it for an answer. A message that does read
      cleanly, but whose author may not answer, is a thread message too.
    - *Messages between the question and the answer.* A thread message
      that joins (or is edited) after the question and before the answer
      is read with the post first, as for any thread message. If the
      reading is still the one that asked, the question is still open and
      the later message can answer it: "one sec, checking" then "3" works.
      If the reading differs (an approver's "wait, it was 4"), answers
      start over and the old question is no longer open; the later
      message was posted before the new question, so it is a thread
      message too, and the new question is asked. The answer message
      itself never counts as a change of the post for this check, nor for
      the clicks' "the post changed" check.
    - *Effect.* An answer message is a fact about the reading that asked,
      like a click, so it is kept out of what the extractor reads: the
      reading that stands does not change, the answers given about it
      still replay, and a model never reads "the 48-port one" as a new
      item. Validation still sees its text (an item or a place named in
      the answer is in the post). On replay:
      - a `quantity` answer replaces the line's quantity. On a pack-unit
        question it also sets the line's unit to the piece and drops its
        pack size, and it clears every pack-unit reason on that line (the
        field rule and "container word in the text" alike), so the dry run
        shows "24", never "24 box". On a vague-quantity question a
        non-container unit stays as it was.
      - a `name_part` answer gives the matcher the typed text for that
        line, and a `name_location` answer gives the location matcher the
        typed text for that side. The line keeps the post's own words as
        its item text (or place text), and the dry run and the audit
        trail show both ("ubiquity psu", named in the thread as
        "Norvane PSU-48"). The match is recorded with the rule that found
        the typed text and with `source: answer`.
      - a `serials` answer replaces the line's serials. Typed by a person
        in the thread, they count as confirmed (the brief: serials are
        confirmed by barcode, OCR or a person).
      Validation and the matcher then run as for any reading. The
      question's own reason (code and detail) is cleared, as for a "yes",
      because some reasons are about the text and would come back.
    - *What the extractor reads.* One rule: a message is kept out of the
      extractor's input only while it is the counted answer to a question
      of the reading that stands. When answers start over (another
      message edited, a new message read otherwise), every earlier answer
      message is read with the post again, since the new reading may ask
      other questions or none, and a value given about an old reading is
      not carried over silently. A typed name that no longer reads
      cleanly because the catalog changed is not re-judged: whether a
      message counted is decided once, when it is first read, and
      stored. On replay its typed text is matched again against the
      catalog as it is then; if that finds nothing now, the line is
      unmatched and the next question says so, but the message stays an
      answer. Only an edit of that message judges it again (below). So a
      reading never changes without someone touching the thread.
    - *Stored.* The answer row keeps the message's `ts` and the kind of
      answer, never the text: on replay the message is read as it is now.
      An edit of an answer message is a change of the post like any other
      and makes a revision. If it still reads cleanly, the new value is
      the answer. If it no longer does, it is a thread message from then
      on: it is read with the post, the reading differs, and the answers
      start over, the safe side for an edit that may be a correction. A
      deleted answer message is no answer, and the question it answered
      is asked again.
    - *Then.* As for a click: the post is due at once, the revised dry run
      shows the result and asks the next question, and the question's
      reply loses nothing (it has no buttons to take down). The answer
      message gets no ephemeral note when it counts; a message that reads
      cleanly but comes from someone who may not answer gets the same note
      a click would ("poster or approver only").
    - *Limits.* As for buttons: an answer never authorizes a write. A
      typed name is a human correction of the post's own words, so once
      the plan is applied and verified, the post's item text becomes an
      alias for the part named in the thread, and the post's place text
      an alias for the location ("Sam's truck" means location Y), as ADR
      0012 does for a confirmed loose match. The typed text itself
      becomes no alias: the person typed a name, not a new way to say it.
      A typed name the matcher found only loosely is confirmed by the next
      question first, as above, and the alias is still the post's text.
  - **The stock question (designed, not built).** Tier 8 asks about the
    stock a line acts on, once its part, place and quantity are settled
    and its stock was read (section 5, "Stock items, not parts"). What
    the thread can settle depends on why the pick is not decided:
    - *A choice that matters, for a transfer or a remove* (`stock_choice`:
      the items differ in batch or supplier part). The question is about
      quantity coverage: buttons, one per stock item that alone covers
      the line with what the lines before it left, oldest first, at most 5, then "None of these". A label
      names the item by its InvenTree id, what is on hand, and its batch
      or supplier part (`#57 · 5 on hand · batch B-1`); these words come
      from InvenTree, never from the post. When no single item covers the
      line, the rule's split is offered as it stands, yes or no ("Take 1
      from #41 and 1 from #57?"). Both are computed from what the lines
      before it left, so a later line of a post that still has enough
      left gets this choice, never the shared-stock reason below (that
      reason is raised only for a shortage the other lines cause).
    - *A choice of target, for an add or a count* (`stock_choice`: several
      stock items of the part at the place). A receipt or a count acts on
      one item whatever it holds, so nothing here is about coverage and
      there is never a split: buttons, one per eligible item at the place
      (unserialized; status and allocation do not matter, as in the rule),
      oldest first, at most 5, then "None of these". The label is the
      same, with what is on hand for information only.
    - *Nothing the thread can choose* (no stock item, too few, serialized
      stock without serials, mixed serialized and unserialized stock, a
      serialized target, other lines of the post take the same stock so
      that too few are left, an add into an item an earlier line takes in
      full, a counted item another line acts on). Two buttons: "Read the stock
      again" and "Drop it". The question says what is wrong and that it
      is fixed in InvenTree or by editing the post (a missing serial
      number, or lines that ask for more than there is: by editing the
      post until answers by thread reply are built).
    - *Effects.* An answer names the stock items the line acts on: the
      one picked, or the items of the split said yes to. It is a fact
      about the reading, replayed like any other answer, and stored as
      primary keys only. Each revision reads stock again and replays it by
      the line's operation, without asking about the items' differences
      again (a person chose them). For a transfer or a remove, the line
      picks only from the named items, by the same rule and after the
      lines before it (the lines of a post share the stock); if they no
      longer cover the line (sold, moved, allocated since, or taken by an
      earlier line), the line gets the reason `stock_choice` with the
      detail "the chosen stock items no longer cover the quantity", and
      the question is asked again with the stock as it is now. For an add
      or a count, the line acts on the one named item if it is still at
      the place and still unserialized, whatever it holds; if it is not,
      the line gets `stock_choice` with a detail for the cause ("the
      chosen stock item is no longer at the location" or "the chosen
      stock item is serialized now"), and the question is asked again.
      "None of these" leaves the line without a pick, and the next
      question is "Read the stock again" or "Drop it": the stock in
      InvenTree is not what the person sees, and is fixed there.
      "Read the stock again" (`again`) makes the post due for a revision,
      which reads stock afresh, and it ends the line's earlier stock
      answers: replay skips every `choose_stock` and `confirm_stock`
      answer about that line that is older than its latest `again`
      (including a "None of these"). So a line whose stock was fixed in
      InvenTree then resolves by the rule, and one that was not asks the
      stock question once more, about the stock as it is now. Answers
      about the line's part, place or quantity are not touched. "Drop it", and "no" to a split,
      end the plan like any "no".
    - *Stored.* `choose_stock` (candidates: the stock item ids offered;
      the line's operation decides whether it is a source or a target),
      `confirm_stock` (candidates: the ids of the split; transfers and
      removes only) and
      `read_stock_again`, with the line they name. The choices are
      `record` (one stock item), `yes`, `no`, `none` and a new `again`.
      Nothing of a label is stored.
    - *Limits.* As for every answer, a choice never authorizes a write:
      the write gate of M3 reads the stock again before the write and
      refuses a pick that no longer holds. The poster or an approver may
      answer.
- **Authorization.** The Slack user id in the payload is checked against
  the configured approvers. An empty approver list means nobody can
  approve. Display names are never trusted. Only the poster or an approver
  can answer a question or correct a post, by button or by thread reply.
- **Undo** is a button on the receipt. A reaction is not enough, because
  anyone who can see the message can add one.
- **Scopes** are the minimum for v1.0: read messages in the channels the
  app is invited to, and write messages. File access is added only when
  photo extraction is built. The exact list is in the manifest
  (`manifests/slack-socket.json`: `channels:history`, `groups:history`,
  `chat:write`, and the events `message.channels` and `message.groups`),
  and `tests/test_manifest.py` fails when it grows.

## 5. InvenTree side

- **REST only.** The connector calls the public REST API with a token. It
  does not touch the database. It uses its own small async client for the
  handful of endpoints it needs. The official Python client is synchronous,
  has no retries, and discards the response of stock adjustments, which
  the audit trail needs.
- **Version check.** At startup the connector reads `apiVersion` from the
  API root and refuses to enable writes on a version outside the tested
  range. Dry-run still works, with a warning. The test matrix pins the
  current stable release and the previous minor, and also runs the floating
  `stable` tag so that a new release is noticed early.
- **The server is permissive, so the connector is strict.** According to
  the research, InvenTree does not reject a request for more than a stock
  item holds: a transfer moves the whole item, and a remove stops at zero.
  Both return success. The connector therefore checks quantities before
  the write and verifies them after it. It never relies on the server to
  refuse a bad request. Integration tests pin this behavior for every
  supported version.
- **Stock items, not parts.** A transfer moves stock items. The resolve
  step lists the stock items of the matched part in the source location
  itself, without sub-locations. It considers only items that are in
  stock, have status OK and are not allocated to an order. It picks by a
  documented rule: oldest first, and a single item that covers the quantity
  is preferred over a split. If the candidates differ in batch, status or
  supplier part, the choice is a review reason. Built as a pure step
  (`core/resolve.py`): "oldest" is the lowest
  stock item id, "allocated" is the list row's `allocated` quantity
  (sales order allocations included, measured on the pinned versions), and
  a row without it is an error, never "none allocated". A choice exists only
  when the pick leaves stock behind: a pick that empties every item asks
  nothing. Serialized stock moves by serial number; without serials only a
  pick of every serialized item is decided. Stock of one part that is
  partly serialized is for a human. An add or a count needs exactly one
  stock item of the part at its location. The lines of one post, across
  all its movements and in the post's order, share the stock that was
  read: a transfer or a remove picks from what the lines before it left,
  so "took 2 and 3" from an item holding 3 is a review reason ("other
  lines of the post take the same stock"), never two picks from one
  item. That reason is raised only for a shortage the other lines cause;
  a line that still has enough left gets the reasons of what is left
  (a choice that matters among what is left is a choice, not a
  shortage). An add does not add to what later lines may take (the stock
  read is the stock before the post), but an add into an item that the
  lines before it take in full is a review reason ("an earlier line of
  the post takes all of the stock item an add acts on"): InvenTree moves
  an item that is transferred whole, and may delete one removed to zero.
  A count sets an item as it is, so a counted item that another line also
  acts on is a review reason on each of those lines: the result would
  depend on the order.
  In the dry run (built), the worker reads stock last: after the answers
  are applied, so a part picked by button is the part whose stock is
  read, and outside the reading that answers are kept for, so a change
  in stock does not drop them. A line's stock is read once its part, the
  place its operation acts at (the source for a transfer or a remove,
  the destination for an add or a count) and its quantity are settled:
  matched by a rule that can authorize a write or by an answer, and with
  no quantity doubt on that line. The reply shows the stock item ids the
  line would act on; the record keeps them per line (`stock`, only once
  read, so older records digest as before). The pick's reasons name
  their line. A failed or malformed stock read is an outage: the post
  waits, as for the catalog. A rejected reading reads no stock.
- **Movement type to API operation.** Every dry-run shows the operation.

  | Movement type | Operation |
  | --- | --- |
  | `take_to_vehicle`, `take_to_site`, `return`, `transfer` | Transfer |
  | `consume` | Remove by default. Policy can make it a transfer to a site location |
  | `receive` | Add to an existing stock item |
  | `count` | Count, only when the part has exactly one stock item in the location |

  v1.0 never creates stock items or locations. When an add or a count has
  no single target stock item, that is a review reason, and a human creates
  the item in InvenTree.
- **Splits.** A partial transfer splits the stock item. The source keeps
  its id with a lower quantity, and a new stock item appears at the
  destination. For stock that is not serialized, verification and undo
  work on quantities per part and location. Serialized stock is verified
  and undone by stock item and serial number.
- **No merge in v1.0.** The server can merge transferred stock into
  matching stock at the destination. A merge deletes the moving stock item,
  writes the transfer's history entry on the destination item, and removes
  batch identity. That would weaken verification and undo, so v1.0 never
  merges. It sends `merge: false` on every transfer line. Measured
  (`test_char_transfer_merge_setting_on`): on 1.5.6 only an explicit
  `merge: true` merges; the server-wide setting "merge stock on transfer"
  (off by default) does not change a line that omits `merge` or sends
  `false`. 1.4.3 has neither the setting nor the field, and never merges.
  The cost is more small stock items on a vehicle. Merge is an option to
  consider after v1.0.
- **Depletion.** Removing the last unit can delete the stock item together
  with its history, depending on a server setting. Verification treats a
  missing source item as correct only when the plan expected it to reach
  zero. Measured on 1.4.3 and 1.5.6 (`test_char_deplete_deletes_item_keeps_history`):
  the item answers 404, but its history entries survive, so the research
  claim that history is deleted with the item was wrong. The marker of the
  removal that deleted the item is found by search on 1.5.6 and not on
  1.4.3. A history query filtered by the deleted item answers 400, not an
  empty list.
  The authority for "the item is gone" is `GET /api/stock/<pk>/`
  answering 404. The 400 only corroborates it, because a malformed id
  gets a 400 too. Markers are looked up by search, never through the
  item filter alone.
- **Serialized stock** always has quantity 1 per stock item, so "2 units"
  means two stock items. The research found that add and remove on a
  serialized item do nothing and still return success. The connector never
  sends a quantity change for a serialized item. It moves whole items,
  chosen by serial number. A serial read from a photo is unconfirmed until
  a human or a barcode confirms it.
- **Locations.** Vehicles and job sites must exist as stock locations
  before stock can move there. The connector never creates locations by
  itself. The docs guide the admin through modeling them, and a setup check
  lists the Slack users that have no vehicle mapped (built:
  `stockthread check-setup`. It names the users who have posted, read as
  ids from the local database, since listing a channel's members would
  need another Slack scope). Structural locations
  cannot hold stock and are never a match.
- **Least privilege.** InvenTree tokens carry the full permissions of
  their user, so the docs describe a dedicated user with the smallest set
  of roles that allows stock adjustments: view on Part and Stock Location,
  and view and add on Stock Item (`test_char_restricted_user_can_adjust`).
  InvenTree stores "change" together with "add" on Stock Item, so that
  user can also edit stock items; it cannot delete them. The setup check
  reads the roles of the token's user (`/api/user/roles/`) and reports
  what is missing, as an error that says what it breaks, and what is more
  than needed, as a warning. "Change" on Stock Item is not reported, since
  it comes with "add". A superuser gets one warning. According
  to the research, tokens expire after one year by default. Where the API
  exposes the expiry, the connector reports it at startup.
- **Pack units.** A container unit or a stated pack size is always a
  review reason. v1.0 never converts packs. A human gives the quantity in
  the unit the part is stocked in.
- **Characterization tests.** Every server behavior this document relies
  on has a named test that runs against each pinned InvenTree version, on
  PostgreSQL as well as SQLite. If one fails on a version, the connector
  refuses writes on that version. Section 11 lists the tests.
- **Rate limits.** According to the research, the server throttles API
  calls by default in recent versions. The client honors 429 responses
  with backoff for reads. A write is never retried automatically
  (section 7).

## 6. Safety model

The threats these controls answer, and the status of each control, are in
[`threat-model.md`](threat-model.md).

### The write gate

A write happens only if all three are true. Each one is evaluated
independently, and each one alone can block.

1. **Switch.** `writes.enabled` is true. The default is false. Turning it
   on is an explicit configuration step, and the connector logs it at
   startup.
2. **Policy.** The policy allows this movement
   ([0004](decisions/0004-default-write-policy.md)). By default a
   `take_to_vehicle` or `take_to_site` movement with no review reason
   applies automatically, with a receipt and an Undo button. Every other
   movement type waits for a human to click "Apply". `policy.auto_apply`
   lists the movement types that may apply automatically. An empty list
   makes every movement review-first.
3. **Validation.** The plan has no open review reason, every part and
   location is matched to exactly one record, and every stock item is
   resolved.

The gate is evaluated twice: when a plan is approved, and again
immediately before the write. The switch, the approver list and the server
version can change in between.

### The model cannot authorize a write

Leaving a confidence score out of the gate is not enough, because a model
can also steer the gate by what it omits. These rules close that path:

- **Review reasons are computed by rule.** The validator derives every
  text-level reason from the raw text and the structured candidate: implied
  source, missing or vague quantity, container units, hedges, negations,
  questions, instructions aimed at the connector. Flags from the model can
  add a reason. They can never remove one, and the pipeline never depends
  on the model to raise one.
- **Grounding.** Each item text must appear in the message, and each
  quantity must correspond to a number or number word in the message.
  A quantity expression is read as a whole: in "2 of the 5" only 2 is
  stated, and "half a dozen" states 6. An expression the rules cannot
  read ("one and a half", "half of the 10", "both", "all the") states
  nothing, so any quantity for it needs review. The unit must be the one the text
  gives right before the item, in any spelling: a swapped, missing or
  made-up unit changes the amount. Anything else is a review reason.
- **Limits.** A quantity above `policy.max_quantity` is a review reason.
  So is a quantity above the stock on hand, which the resolve step checks.
- **Where.** `core/validate.py` implements the rules above that need no
  catalog, and `tests/core/test_validate.py` checks, on every corpus
  movement with all extractor flags removed, that the rules alone still
  raise every flag that follows from the fields.
- **What the validator checks against the text.** An item must appear as
  whole words, and it must not be a location. Its quantity must be stated
  in the few words before it ("2 breakers", "a box of", "half a dozen"),
  so quantities cannot move between items. Each location must appear on
  its side: after "from" or "off" for the source; after "to", "on", "at"
  or "for", or as a heading ("truck 2:"), for the destination. Serials
  must appear in the text, one per unit. Receive, count, consume, return
  and transfer each need one of their words in the text. Container words,
  vague quantities, corrections, a third person as the actor, negations,
  plans, questions and instructions raise their reasons from the text
  alone. On the corpus, the extra reasons fall on photos, threads,
  multi-movement posts and one Spanish post. The limit applies to every
  unit, so "300 ft" of cable needs a click at the default of 100.
- **Matching is deterministic** and takes only the item text, the location
  text and the poster as input (section 10). The extraction prompt never
  contains catalog or alias data, so the model cannot learn part names to
  aim at.
- **Auto-apply is stricter than approval.** A movement applies without a
  click only when every match comes from an exact, normalized or alias
  rule, the vehicle resolves through a mapped Slack user or an exact
  location name, and the model raised no flag. If the rule-based extractor
  produced a candidate that disagrees with the LLM extractor, that is a
  review reason.
- **Implied source.** `policy.default_source` can name a default source
  location per movement type. It is unset by default, and an implied
  source is then a review reason.

A pipeline-level test fixes a post and a catalog, varies every score the
model returns, strips every flag the model returns, and checks that the
gate decision never moves from "blocked" to "allowed".

### Dry-run

Dry-run builds the exact request and shows it, and stops there. It shares
all code with apply up to the HTTP call. The InvenTree client has two
classes: a read-only client, and a writing client that can only be
constructed by the gate. The read-only client rejects every method except
GET at the transport level. With writes off, the writing client does not
exist in the process, and the full test suite with writes off records zero
requests other than GET.

### Untrusted input

Message text and photos are data. They are passed to the model in a
delimited field, and the model's output is parsed against a strict schema.
The model cannot call tools, choose parts, or reach InvenTree. Whatever it
returns, the worst case is a wrong candidate, which then faces validation,
matching against the real catalog, and the gate. The corpus has
prompt-injection cases, and any deviation on them is a hard failure.

### Privacy

- Raw message text is stored in the audit database and nowhere else. Logs
  and the append-only audit log carry ids, not text.
- Message text never goes into InvenTree. The notes of a stock adjustment
  hold the marker, a link to the Slack message and the Slack user id.
- Retention covers every table that repeats message text: raw events,
  extractions, candidates, review answers and stored API calls.
- Photos are not downloaded in v1.0. No extractor sees a photo, so every
  movement of a post that has one carries the review reason
  `photo_not_read`: the photo may show other items, another quantity or
  serials, and a person checks the reading against it. A "yes" to that
  question confirms the reading only: once answers clear reasons, it must
  not clear the questions a reader of the photo would have raised
  (serials, a quantity read from it). Those are still asked. Two
  limits: a photo posted after the sibling window starts its own post,
  and an image that arrives as a link preview is not a file.
- Tokens and secrets are redacted in logs, errors and stored API calls, by
  type (a secret value type that prints as `***`) and by pattern as a
  second layer.
- Text sent to an LLM provider leaves the machine. The docs say so plainly.
  A rule-based filter masks strings that look like secrets before text is
  sent to a remote provider. A local model and the rule-based extractor are
  supported.

## 7. Idempotent apply, verification and undo

This is the highest-risk part of the system. Nothing in this section is
built before the characterization tests of section 11 pass.

### Units of work

| Unit | Key | Rule |
| --- | --- | --- |
| Post | `(workspace id, channel id, ts of the first message)` | Has at most one applied plan revision at a time |
| Plan revision | `(post key, revision)` | Holds 1 to n requests. A new revision can be applied only after the previous one is verified as undone |
| Request | `(post key, revision, sequence number)` | Applied at most once. Has its own marker and its own state |

One Slack post can describe several movements ("2 ONTs to my truck, and
returned 1 switch to receiving"), and movements with different
destinations need separate API requests. InvenTree cannot run them as one
transaction. The requests of a plan therefore run in order. At the first
failure or unknown outcome, the remaining requests are not sent, the plan
is marked `partially_applied`, and the operator is offered an undo of the
requests that completed. Nothing is rolled back automatically.

The database enforces the keys with unique constraints, not with
application logic alone. The local database is not the only guard: the
marker check below also protects against a lost or restored database.

One Slack app has one connector instance. A startup lock with a heartbeat
refuses a second instance on the same database. Two instances on different
databases cannot be prevented by the connector. The read-back detects
their writes after the fact, and the docs warn against that setup.

A plan has its own state: `current`, `stale`, `expired`, `applied`,
`partially_applied`, `undone` or `accepted`.

### States of a request

| State | Meaning | Next |
| --- | --- | --- |
| `planned` | Part of a current plan | `approved`, `rejected`, `cancelled` (plan became stale), `expired` |
| `approved` | A human or the policy approved it | `applying`, `planned` (precondition failed, back to review), `verify_failed` (marker already present), `cancelled` (plan became stale) |
| `applying` | Intent stored, write about to be sent or sent | `applied`, `failed`, `unknown` |
| `failed` | InvenTree refused the request. Nothing was written | `approved`, by a human, as a new attempt |
| `unknown` | The outcome is not known | `applied`, `failed` (no marker and nothing changed) or `verify_failed`, by recovery |
| `applied` | InvenTree accepted the request | `verified`, `verify_failed` |
| `verified` | Read-back agrees with the plan | `undoing` |
| `verify_failed` | Read-back or recovery found something unexpected | `accepted` or `undoing`, by the operator |
| `accepted` | The operator accepted the observed state. Final | |
| `undoing` | Undo intent stored | `undone`, `undo_failed`, `undo_unknown` |
| `undo_failed` | InvenTree refused the undo. Nothing was written | `undoing`, by a human, as a new attempt |
| `undo_unknown` | The outcome of the undo is not known | `undone`, `verified` or `verify_failed`, by recovery |
| `undone` | The undo is verified. Final | |
| `rejected`, `cancelled`, `expired` | Never applied. Final | |

A new plan revision for the post can be approved only when every request
of the earlier revision is in a final state that left no stock change
behind: `undone`, `rejected`, `cancelled`, `expired`, or `failed` with no
later attempt. After `accepted`, the post is closed for further plans.

Transitions are compare-and-set updates in a transaction. Two workers that
race for the same request cannot both move it to `applying`, and a second
click on a button changes nothing.

### The marker

`[sic:<22 random base32 characters>:<a or u><two-digit attempt number>]`
(Crockford's alphabet in lower case: digits and letters without i, l, o, u),
for example `[sic:k7m2q9x4t1b8w3n6p5r0zd:a01]`. `a` marks an apply and `u`
an undo. The random id belongs to one request, and every attempt of that
request shares it.

- The marker has a fixed length and comes first in the notes. The notes
  hold nothing else except a link to the Slack message and the Slack user
  id, and they stay under 200 characters. The server stores notes in a
  `varchar(512)`, which PostgreSQL enforces (`test_char_notes_length`).
- The search for a marker reads every page of results and compares the
  whole token, because the server search matches substrings.
- **A marker that is found proves the write happened. A marker that is not
  found proves nothing.** On 1.4.3 the marker of a removal that deleted
  its stock item cannot be found (section 5, Depletion), an admin can
  delete history. History was readable as soon as the server answered
  in the measured setup (one server process with SQLite,
  `test_char_marker_visible_after_response`), but that is not assumed
  for every deployment.

### Apply protocol

All steps from 2 to 6 run under one writer lock in the process, so the
connector never has two InvenTree writes in flight and cannot race itself.

1. **Gate.** Evaluate the write gate again.
2. **Marker check.** Search the stock history for the marker id of this
   request, from any attempt, apply or undo. A hit that the recorded state
   does not explain stops the apply and moves the request to
   `verify_failed` for the operator. Markers of the other requests of the
   post are compared with their recorded states in the same way: a
   verified request must have its marker, and a request that never ran
   must not.
3. **Snapshot and precondition.** Read every stock item of each part in
   the plan, at the source and at the destination location, without
   sub-locations. Compare with what the plan was built on. If a source
   holds less than the plan takes from it, or anything relevant changed,
   stop: the request returns to `planned` and the post to review.
   Also record the ids of the history entries that already exist on each
   source stock item the request takes from. Otherwise store the snapshot,
   the exact request body and the move to `applying` in one transaction.
4. **Write.** Send the request once. A request that InvenTree refuses
   with a 4xx changes nothing: all lines succeed or fail together
   (`test_char_multi_item_request_is_atomic`). A 5xx is not that safe. On
   1.5.6 with PostgreSQL, a request whose history entry fails to save
   answers 500 after the stock change is committed, and no entry carries
   the marker (`test_char_overlong_notes_write_without_history`). That is
   why a 5xx is an unknown outcome and recovery compares quantities.
5. **Read back.** Three checks, and any failure means `verify_failed`:
   - the history entries that carry the marker have the deltas of the
     plan;
   - the quantities per part and location match the snapshot plus the
     plan;
   - the source stock items and the items the request created have no
     history entry that is missing from the snapshot and lacks this
     request's marker. Such an entry means another writer touched the
     same stock during the apply.
6. **Record.** Move to `verified` and post the receipt.

Another InvenTree user can still change the stock between steps 3 and 4.
The connector cannot lock InvenTree, so that window cannot be closed, only
detected by step 5.
A concurrent write can also make the connector's own request move less
than it asked for (a transfer of more than an item holds moves the whole
item), and on 1.4.3 with PostgreSQL two concurrent splits of one item can
even create stock (`test_char_concurrent_transfers_same_item`). The first
two checks of step 5 catch the short move (a delta of 1 where the plan says
4), but not the 1.4.3 lost update: there the connector's own deltas and the
source and destination quantities all match the plan, and the extra stock
appears at a third location outside the snapshot. The third check catches
it, because the other writer's split leaves an entry without the marker on
the source item. The price is that any concurrent write to the same stock
items ends in `verify_failed` and goes to the operator, even a harmless
one. A concurrent writer that touches none of these items cannot be
detected, and it also cannot corrupt the connector's movement.

### Unknown outcomes and recovery

An outcome is **known** only when InvenTree itself answered: a 2xx status,
or a 4xx status with a JSON body in InvenTree's error format. A known 4xx
moves the request to `failed`. Everything else is **unknown**: timeouts,
connection errors, every 5xx, the statuses 408, 429 and 499, and any answer
whose body does not come from InvenTree, such as a proxy error page. A
crash while a request is in `applying` is unknown as well.

A request that timed out on the client may still be running on the
server, so recovery waits before it concludes anything:

1. The request is quarantined for `apply.settle_seconds` (default 120,
   and it must exceed the request timeout of the server).
2. After that, recovery searches for the marker and reads the quantities.

| Marker | Quantities compared with the snapshot | Result |
| --- | --- | --- |
| Found | Match the plan | Continue with read-back, then `verified` |
| Found | Anything else | `verify_failed` |
| Not found | Unchanged | `failed`: the operator may approve a new attempt |
| Not found | Changed | `verify_failed` |

A new attempt, after a failure or after recovery, gets a new attempt
number in its marker. It runs the whole protocol again, and step 2
searches for the markers of **all** earlier attempts, so a late write from
an earlier attempt blocks it. A retry never happens without a human.

`verify_failed` blocks further writes for the post. The operator sees the
snapshot, the plan and the current state side by side, and chooses: accept
the state as it is, or reverse the change that was observed.

### Undo

Undo reverses **what was observed** in the stock history at apply time, not
what the message or the plan said. It goes through the same protocol, with
its own marker, read-back and recovery.

Undo is allowed only if the stock the apply touched has no later history
entries except the connector's own, and the condition in the table holds.
Otherwise undo is refused with an explanation and becomes a review item.
Undo expires after `undo.max_age` (default 7 days).

| Operation | Undo | Condition |
| --- | --- | --- |
| Transfer | Transfer the same stock back to the source location | The destination stock items exist and hold at least the moved quantity |
| Remove | Add the quantity back | The stock item still exists. If it was deleted on depletion, a human recreates it |
| Add | Remove the same quantity | The stock item holds at least the added quantity |
| Count | Count back to the recorded earlier value | The current value is still the one the connector set |

For stock that is not serialized, undo restores quantities per part and
location. It does not promise to restore stock item ids, because a split
changes them.

## 8. Data model and audit trail

The store is SQLite in a single file, which suits a single-node deployment
and needs no extra service.

| Table | Content |
| --- | --- |
| `slack_events` | Raw events with a processing status, unique on `event_id` |
| `posts` | One row per post, unique on post key, and the time its plan expires from, fixed by its first dry run (migration 0015) |
| `post_messages` | Which post each message belongs to, unique on `(workspace, channel, ts)`: ids, author and thread, never text (migration 0003) |
| `revised_messages` | Message keys that were edited or deleted, in any arrival order: ids only (migration 0004) |
| `dry_run_revisions` | Revisions 2, 3, ... of a post's dry run: what each read through, its outcome, record and posted reply (migration 0007) |
| `catch_up` | One row per channel: where its catch-up started, and how far its history has been read (migration 0006) |
| `dry_runs` | A post's first dry run: the outcome, the extractor, and the result as JSON (candidates, matches, reasons) (migration 0004), and the newest message it read (migration 0007) |
| `plans` | Plan revisions with the hash of their input messages |
| `extractions` | Extractor name and version, prompt version, raw output |
| `candidates` | Candidate movements and line items |
| `matches` | Match results with candidates and the rule that matched |
| `review_items` | One question per dry run that asked one: the question as ids and codes, approvers only or not, the message it was asked in, open, answered or stale, the answer and who answered (migration 0010); the poster and when its plan expires (0011); where its buttons stand (0012); when it expired unanswered (0014) |
| `requests` | The state machine of section 7, unique on the request key, with the marker id |
| `request_attempts` | One row per apply or undo attempt: marker, snapshot, request body, outcome |
| `interactions` | One row per button: its random id, its question and the answer it stands for (migration 0010) |
| `instance_lock` | The single-instance lock with its heartbeat |
| `api_calls` | Every InvenTree request and response, redacted |
| `aliases` | Alias, target, who confirmed it, from which review item |
| `audit_log` | Append-only log of every state change, ids only |

`audit_log` rows are never updated or deleted by the application. Database
triggers reject both. Triggers also keep every key column fixed (the row
id under any of its names: `id`, `rowid`, `oid`; migration 0005), keep
the recorded intent of an attempt (marker, snapshot, request body) fixed,
and keep rows that carry a key from being deleted. The first migration
holds the tables of section 7; the others arrive with their features
([ADR 0009](decisions/0009-audit-store.md)). The log holds ids and never message text, so it can
stay complete when retention removes text elsewhere. Retention of text is
configurable, and expiry replaces the text while keeping the row.

An operator can answer three questions from the trail: what did the
connector see, why did it decide what it decided, and what exactly did it
send to InvenTree.

## 9. Extraction

- **Interface.** `extract(post) -> list[candidate]`, with two
  implementations: rule-based and LLM.
- **Rule-based extractor** (`core/extract_rules.py`). Needs no network
  and no key. It handles the common patterns (quantity, item, preposition,
  location), spelled-out numbers and units. It is the fallback, the test
  oracle for the simple cases, and the default in the quickstart.
  - It is precision-first. It reads one message of a known shape and
    **declines** the rest: photos, threads, sibling messages, lists,
    several movements, a third person as the actor, or a post without a
    verb that does not open with a number. `Extraction.declined` means "could
    not read", which is not the same as "not a movement". Questions,
    requests, plans, conditions and negations are "not a movement".
  - Post flags are raised on every post, even a declined one. A sentence
    that holds instructions to the connector is dropped before parsing.
  - Flags that follow from the fields are computed by rule, never guessed.
  - Corpus 0.2.0 (`tests/core/test_extract_rules_corpus.py`, matching not
    scored): 0 hard failures; movement detection precision 100%, recall
    63.4%; flag precision 100%, recall 53.1%; case exact match 70.3%. The
    30 missed movements are all declined posts: photos, threads and
    sibling messages (11), lists, headings and multi-movement posts (7),
    and wording it does not know (12: "gave", "swapped", a third person,
    no quantity, Slack chatter around the movement, Spanish, text after a
    comma that ends the location).
  - Nothing is left unread: text after a comma that ends a location, or a
    number word after a quantity ("two dozen", "twenty five", "a
    hundred"), declines the post. "1,500" is one quantity.
- **LLM extractor** (`llm/extract.py`). A provider interface with a strict
  JSON schema for the output. v1.0 ships one adapter for OpenAI-compatible
  endpoints (`llm/openai_compatible.py`), which covers hosted providers
  and local servers. More adapters can follow. The provider is optional at
  install time: without an `[llm]` section no post leaves the machine.
  - The worker asks the model only for a post the rule-based extractor
    declines (ADR 0011). Its candidates pass the same validator and
    matcher. A provider that gives no answer makes the post wait; it is
    asked again with backoff, and after 15 minutes the post is finished
    without a reply and its record says `llm_unavailable`. While several
    posts wait, each call goes to the one asked least recently. A worker
    step makes at most one call, after every reply that needs none, so
    posts the rules read never wait behind the model. A call has
    a whole-call deadline (`llm.timeout_seconds`), and the instance lock
    outlives it. After a call the queue is drained before the reply is
    posted, so an edit that arrived during the call withholds the post.
  - The model sees the message texts and how many files each has. It sees
    no user ids (a mention is sent as "@someone"), no timestamps, no
    catalog and no aliases.
  - Strings that look like credentials are masked before the text is sent:
    known token shapes, long runs of letters and digits, a password in a
    URL, phone numbers, and the word after a label such as "password:" or
    "gate code is". Part numbers with dashes, MAC addresses and links are
    sent as written. This is a second layer: no pattern finds every
    secret, only the first word of a passphrase is replaced, and the docs
    say that text leaves the machine.
  - The answer is parsed strictly. Output that does not fit the schema in
    any field (an extra field, a quantity as text, an unknown flag, a name
    over two lines, a key given twice, a number out of bounds) **declines** the post. Nothing
    is repaired or half-read.
  - The model can add a post flag and never remove one: the flags found by
    rule in the text are raised whatever the model answers.
  - A provider that gives no answer (unreachable, an error status, a
    body that is no chat completion) is an outage, not a reading:
    `ProviderError`. An answer that is no reading of the post (a refusal,
    an answer cut off by a limit or filtered, or one that is too long) is
    `NoReading`: the provider is well and would answer the same again, so
    that post alone is finished without a reply, its record says
    `llm_no_reading`, and no other post waits. A 200 with nothing in it,
    or with an ending nobody asked for, stays an outage. Errors never quote the
    request, the response or the key. An API key is sent only over https, or to a server on the same
    machine. Proxy settings from the environment are not used, and
    redirects are not followed.
  - Scored from recordings (`eval/recorded/`, `tests/llm/test_recorded_corpus.py`).
    Prompt `llm-0.2`, `anthropic/claude-haiku-4.5` through an
    OpenAI-compatible endpoint, corpus 0.2.0, matching not scored, one
    recording:
    - The model reading every post: movement detection precision 100%,
      recall 92.7%; flag precision 91.9%, recall 70.8%; case exact match
      74.3%. Its 16 missed flags show why flags that follow from the
      fields are computed by rule (pack units, an implied place) and why a
      photo is never left to the model.
    - As the worker runs it (the rules first, the model for the posts they
      decline, the reasons the validator derives by rule added): movement
      detection precision 100%, recall 93.9%; flag precision 89.0%, recall
      84.4%; case exact match 82.2%. The scorer still lists 4 missed
      flags, all of them flags only a reader of the photo can raise
      (group-004, -009, -010, -012); `photo_not_read` asks a person on each
      of those movements, so the bar counts them as raised.
    - Prompt `llm-0.1` read a post whose thread reply took the movement
      back ("scratch that") as a take-out and a return. `llm-0.2` says that
      a movement taken back is no movement.
- **Disagreement.** When both extractors run and disagree on a quantity, a
  movement type or a location, that is a review reason.
- **Versioning.** Prompt and schema are versioned, and the version is
  stored with every extraction.

## 10. Matching

Matching works on a catalog snapshot (`catalog.py`): every active part
and every stock location, read from InvenTree at startup and again every
5 minutes. A record with a missing or mistyped field fails the whole
read, because a partial catalog would make a part look unknown. When a
refresh fails, dry runs keep the last snapshot for up to an hour; after
that, posts wait. Aliases are not read yet. Vehicle assignments come from
the `[vehicles]` settings (Slack user id to location path) and are resolved
against every snapshot: a path counts only when it is one non-structural
location's whole path, letter for letter. Otherwise that person has no
vehicle, the log names them, and their "my truck" is unmatched.

Deterministic. The rules run in this order:

1. **exact**: the text equals an IPN or a part name, or a location path
   or name.
2. **normalized**: the same after folding case, punctuation, plurals,
   possessives and spacing.
3. **alias**: a confirmed alias, or "my truck" for the poster's vehicle.
4. **words**: every word of the text is one of the record's words (name,
   keywords and aliases; path and description for locations). "Disc 40"
   finds "Norvane Disc 40 Access Point". A word such as "3ft" or "van3"
   may be split into letters and digits.
   A match by this rule or by rule 5, for a part or a location, never
   applies without a click. Once a person confirms it and the plan was
   applied and verified, the text becomes an alias
   ([0012](decisions/0012-first-confirmation-becomes-alias.md)). A
   confirmed dry run teaches nothing.
5. **fuzzy**: as rule 4, but a word of 4 letters or more may be misspelled
   (`difflib` similarity 0.8, so "ondrl" finds "Ondrel"). Words with digits
   never get this allowance, so "truck 7" never becomes "Truck 1".

- The first rule with any hit decides. One hit is `matched`, more than one
  is `ambiguous` with every hit as a candidate. Matching does not fall
  through to a later rule to find a single winner. The candidate list is
  capped where it is shown, not in the result.
- One exception, planned with alias learning (ADR 0012, M3): an alias
  learned from a confirmation stores the records that fitted its text at
  rules 4 and 5 then. When it hits, those rules run again, and a record
  that fits now and is not in the stored set makes the result `ambiguous`
  (the alias's record and the new ones). An alias typed by an admin is
  not rechecked.
- A single hit at rule 4 or 5 is `matched`, because the corpus clerk would
  pick that one record. It still cannot authorize a write: it raises the
  `fuzzy_match` review reason. Only rules 1 to 3 authorize, and the code
  enforces that with an allowlist (`Match.can_authorize_a_write`). The
  allowlist also holds `answer`: a record a person picked or confirmed
  by button in the thread (section 4, "Acting on an answer"). The
  matcher never produces it.
- Corpus 0.2.0, with the expected text as input (`tests/core/test_matching_corpus.py`):
  part match 100% (109 results) and location match 100% (92 results).
  Of the 99 single part matches, 80 came from rule 4 and 1 from rule 5
  ("Disc 40", "ONTs"), and 20 of the 88 single location matches came from
  rule 4. Under the
  current policy all of those need a confirmation.
- Plural endings are folded only on words without digits at rules 2 and
  3 ("40S" may be another model), and "-es" only after a hissing sound
  ("boxes", "switches", but "tapes" is never "tap"). Rules 4 and 5 fold
  both kinds ("d40s", "tapes"), and those rules never authorize. Where
  folding still makes two records look the same ("ups": "UP" or "UPS"),
  the result is ambiguous.
- A trailing "+" is part of the word at rules 1 to 3 ("SFP+" and "SFP",
  "PoE+" and "PoE" are different products). Rules 4 and 5 ignore it, so
  "poe injector" still finds "PoE+ Injector", for confirmation.
- Template, inactive and virtual parts are never a match. Structural
  locations are never a match.
- "My truck" resolves through the mapping from Slack user to vehicle,
  only when the word ("truck", "van") names that vehicle in its path,
  description or aliases. "My trailer" from a user with a truck goes
  through the other rules instead. Aliases that contain such words are stored per user.
- An alias is checked against the catalog snapshot when it is used. An
  alias that points to an inactive part or a deleted location is ignored
  and reported.

## 11. Testing strategy

| Layer | How | Where |
| --- | --- | --- |
| Pure core | Unit and property tests | Every PR |
| Safety invariants | Named tests, one per invariant, listed in the docs | Every PR |
| Slack | Recorded event fixtures, signature tests, no network | Every PR |
| Extraction | Corpus with recorded model outputs | Every PR |
| Extraction, live | Corpus against a real provider | When a key is set |
| InvenTree behavior | Characterization tests, real server in Docker, pinned versions | When relevant paths change |
| Apply and undo | Parallel, restart and fault tests against the real server | When relevant paths change |
| Docs | Quickstart followed from scratch | Every milestone |

CI stays lean: integration jobs run only when their paths change, and
dependencies are cached.

### Characterization tests

The research read InvenTree's behavior from documentation and source code.
It did not run it. These tests turn each claim the design depends on into
a fact, per pinned version. They use a raw HTTP helper that exists only in
the test suite.

| Test | Claim |
| --- | --- |
| `test_char_over_quantity_transfer` | A transfer of more than an item holds is not rejected |
| `test_char_over_quantity_remove` | A remove of more than an item holds stops at zero |
| `test_char_serialized_quantity_noop` | Add and remove on a serialized item change nothing, return success, and whether they still write a history entry |
| `test_char_deplete_deletes_item_keeps_history` | Depletion can delete the stock item. The earlier history survives. The removal's marker is found by search on 1.5.6 but not on 1.4.3 |
| `test_char_partial_transfer_split` | A partial transfer keeps the source id and creates a new item |
| `test_char_marker_on_every_entry_type` | The notes reach every history entry a request writes |
| `test_char_marker_visible_after_response` | Whether history is readable as soon as the server has answered |
| `test_char_marker_search_is_substring` | The history search matches substrings, and `limit` with `offset` pages through it without overlap |
| `test_char_notes_length` | Notes up to 512 characters are stored whole. Longer ones: SQLite stores them, PostgreSQL answers 500 |
| `test_char_overlong_notes_write_without_history` | PostgreSQL: on 1.5.6 that 500 still commits the stock change, with no history entry. On 1.4.3 it changes nothing |
| `test_char_multi_item_request_is_atomic` | One bad line fails the whole request |
| `test_char_restricted_user_can_adjust` | View on Part and Stock Location plus view and add on Stock Item allow transfer, add, remove and count. The server adds "change" to "add"; delete stays refused |
| `test_char_user_without_stock_add_cannot_adjust` | Without "add" on Stock Item every adjustment is a 403 that changes nothing, even with "change" |
| `test_char_concurrent_transfers_same_item` | Two simultaneous transfers of 4 from an item of 5. SQLite: colliding requests give one 201 and one 5xx that wrote nothing; requests handled one after the other behave like PostgreSQL 1.5.6. PostgreSQL 1.5.6: both 201, the second moves only the 1 left, so a 201 can move less than asked. PostgreSQL 1.4.3: both 201 and stock grows to 9 (a lost update) |
| `test_char_transfer_does_not_merge` | A transfer to a location that holds the same part leaves separate items (server-wide merge setting off) |
| `test_char_transfer_merge_setting_on` | The server-wide merge setting does not merge a line that omits `merge` or sends `false`; only `merge: true` merges (1.5.6) |
| `test_char_stock_list_cascade_false` | A stock list with `cascade=false` leaves out sub-locations |
| `test_char_error_body_format` | The shape of a 4xx answer from InvenTree, which recovery uses to tell it from a proxy |

Two Slack claims need a capture from a real workspace before the fixtures
are frozen: a photo with a caption arrives as one message, and the app
receives events only from channels it is a member of.

A fault proxy between the connector and InvenTree delays, drops and
corrupts responses (`tests/support/faultproxy.py`). Each fault happens
either before the request reaches the server or after the server answered.
`test_char_failure_seen_by_client_hides_committed_transfer` shows that a
client timeout, a lost connection, a proxy error page and a cut body can
each hide a transfer that committed. The proxy drives the M3 tests for
unknown outcomes, including
`test_late_commit_after_client_timeout_never_double_applies`.

## 12. Configuration

One TOML file plus environment variables for secrets. Settings are
validated at startup, and the connector refuses to start on an unknown key,
a value of the wrong type ("yes" is not a boolean) or a secret in the file.
Errors name the setting and never repeat its value.
[`stockthread.example.toml`](../stockthread.example.toml) lists every
setting with its default, and `stockthread check-config` validates a file. The startup
log prints the effective settings with secrets redacted, and states in one
line whether writes are on, and in another whether post text is sent to a
model and to which host.

## 13. Proposed v1.0 scope **(decision pending)**

In:

- One Slack workspace and one InvenTree instance
- Socket Mode and HTTP
- Text posts, and photo posts through their caption
- Transfer, remove, add, and count for the single stock item case
- Review and clarification in the thread
- Apply with read-back, receipts and undo
- Alias learning from confirmed corrections
- Rule-based extractor, and LLM extractor with one provider adapter
- Catch-up after downtime
- Docker image built locally, and docker compose file

Out:

- Reading quantities, serials or items from photo pixels (experimental
  after v1.0)
- Merge on transfer, pack conversion, creating stock items or locations
- Reminders for unanswered questions (plans expire instead)
- A web dashboard
- Purchase orders, build orders and sales orders
- Multiple workspaces or instances in one deployment
- Other chat platforms
- A hosted service

## 14. Open questions

Tracked as issues labeled `needs-dan`. See the decision log in
[`decisions/`](decisions/).
