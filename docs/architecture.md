# Architecture

A map of the code for a new contributor: how a Slack post becomes a stock change, which module does
each step, and the rules the layout keeps. The reasons behind each choice are in the
[design](design.md) and the [decisions](decisions/). This page only says where things are.

## One process

`stockthread run` is one Python process on one SQLite file. It has three parts:

```text
 Slack ──events, clicks──▶ listener ──store, then ack──▶ ┌──────────────┐
 (Socket Mode or HTTP)     (Bolt threads)                │ SQLite store │
                                                         │ queue, posts,│
 Slack history ──missed messages──▶ catch-up ──────────▶ │ plans, audit │
                                    (one thread)         └──────┬───────┘
                                                                │ claim
                                                                ▼
 Slack ◀──dry runs, questions, receipts── worker loop ──GET──▶ InvenTree
                                          (main thread) ──writes behind the gate──▶
```

- The **listener** (`slack/listener.py`) checks each payload (`slack/ingest.py`), stores the ones it
  accepts in the durable queue, and only then lets Bolt acknowledge it. It never processes a post.
- **Catch-up** (`slack/catchup.py`) reads the channel history after connecting, for messages posted
  while the connector was down, then every 15 minutes for any that Slack never delivered. It
  queues them the same way.
- The **worker** (`worker.py`) is a loop in the main thread (`run._loop`). Each step drains the queue,
  then does the work that is due: replies, approvals, writes, receipts, taking down old buttons, and
  blanking old message text (`Worker.step`). Everything after the listener happens here, one thing
  at a time.

The store holds an instance lock with a heartbeat, so a second process on the same file refuses to
start, and a process that lost the lock stops.

## The life of a post

Each step names the module that does it. Steps 1 to 6 make the dry run and never write.

1. **Ingest.** `slack/ingest.py` checks the workspace, the app, the channel allowlist, the message
   subtype, bots and duplicates, and drops what fails, with a reason; over HTTP, Bolt checks the
   signature first. `slack/listener.py` stores what is left. Chatter that names no movement is
   declined later, by the extractors.
2. **Group.** `core/grouping.py` decides which post a message belongs to: a photo and its caption,
   sibling messages, thread replies.
3. **Extract.** `core/extract_rules.py` reads the text by rule. Only a post the rules decline goes to
   the model (`llm/extract.py`, through `llm/openai_compatible.py`), and only output that fits the
   schema comes back ([0011](decisions/0011-llm-after-the-rules.md)).
4. **Validate.** `core/validate.py` turns contradictions, missing places, text the reading does not
   come from, and model flags into review reasons.
5. **Match.** `core/matching.py` maps item and place text to InvenTree parts and locations by rule:
   exact, normalized, alias, whole words, then fuzzy. `catalog.py` reads the catalog it matches
   against. The model never picks a record.
6. **Resolve and reply.** `core/resolve.py` picks the stock items each line moves.
   `core/questions.py` chooses the one question the post gets, and `slack/replies.py` builds the dry
   run in the thread. The store keeps that reply as the plan's record.
7. **Answer.** A button click or a typed reply in the thread (`core/answers.py`,
   `core/typed_answers.py`) changes the reading, and the worker replies again.
8. **Approve.** `applier.py` asks the gate (`gate.py`) to judge the plan: by the policy with no click
   ([0004](decisions/0004-default-write-policy.md)), or by an approver's Apply click. Writes off,
   an untested InvenTree version, or any open review reason blocks it. `plan.py` turns an approved
   plan into one request per movement, rebuilt from the dry-run record the thread showed
   ([0016](decisions/0016-apply-reads-the-dry-run-record.md)).
9. **Apply.** `apply.py` runs the apply protocol for each request under one writer lock: the gate
   again, the marker check (`core/marker_check.py`), the snapshot and precondition
   (`precondition.py`), one write (`writer.py`), the read-back (`readback.py`), and the record.
   `core/requests.py` is the request's state machine. A write whose outcome is unknown is never sent
   again; recovery looks for its marker (`core/marker.py`) in the stock history instead
   ([0015](decisions/0015-unknown-writes-are-never-retried.md)).
10. **Receipt and undo.** The worker posts the receipt in the thread (`slack/replies.py`). An Undo
    click runs `apply.undo_apply`, with the reverse request built by `inverse.py`.
11. **Learn.** Once every request of a confirmed plan is verified, `learning.py` stores the aliases it
    teaches ([0012](decisions/0012-first-confirmation-becomes-alias.md),
    [0021](decisions/0021-a-pick-teaches-nothing.md)).

`stockthread trail` (`trail.py`) prints what the store recorded for one post: what it saw, what it
decided and why, and who answered what.

## Module map

Every module under `src/stockthread/`:

| Module | What it does |
| --- | --- |
| `__init__.py` | The package, and its version. |
| `__main__.py` | The command line: `check-config`, `check-setup`, `run`, `trail`, `aliases`. |
| `config.py` | Settings: one TOML file for behavior, environment variables for secrets. |
| `run.py` | `stockthread run`: starts the listener, catch-up and the worker, and wires InvenTree into them. |
| `worker.py` | The worker: from the queue to dry runs, questions, approvals, writes and receipts. |
| `places.py` | A movement's source and destination: the default source, and when a named place keeps it out. |
| `dry_run.py` | The dry-run record a reply decided, and what the worker reads back from it. |
| `picking.py` | Which lines of a dry run are settled enough to read stock for, and the stock items they act on. |
| `failures.py` | How the worker sorts the errors it catches: transient, an outage, or a rate limit; their short codes. |
| `setup_check.py` | `stockthread check-setup`: what an admin should fix before the first dry run. |
| `alias_admin.py` | `stockthread aliases`: list, export, add, remove, history. |
| `trail.py` | `stockthread trail`: one post's audit trail, for an operator. |
| `printable.py` | Escapes text from Slack before it reaches a terminal. |
| `catalog.py` | Reads the parts and locations the matcher works on. |
| `inventree.py` | The read-only InvenTree client: it can only send GET. |
| `writer.py` | The only client that can change stock: one POST of a marked body, never retried. |
| `gate.py` | The write gate: switch, policy and validation, each able to block alone. |
| `applier.py` | The worker's approval and apply steps. |
| `plan.py` | An approved plan becomes its requests, one per movement. |
| `plan_record.py` | Rebuilds what a write acts on from the dry-run record the thread showed. |
| `apply.py` | The apply protocol, recovery of unknown outcomes, and undo. |
| `precondition.py` | Apply step 3: the snapshot and the precondition. |
| `readback.py` | Apply step 5: the read-back. |
| `inverse.py` | The undo of a write, built and checked before any network code sees it. |
| `learning.py` | The aliases a confirmed plan teaches. |
| `core/model.py` | The domain model: posts, candidates, matches, review reasons. |
| `core/grouping.py` | Which post a Slack message belongs to. |
| `core/signals.py` | Text patterns the rule-based extractor and the validator share. |
| `core/extract_rules.py` | The rule-based extractor. |
| `core/validate.py` | Review reasons derived by rule from the text and the reading. |
| `core/matching.py` | Deterministic matching of text to parts and locations. |
| `core/resolve.py` | Source selection: which stock items a line moves. |
| `core/questions.py` | The one focused question a post gets. |
| `core/answers.py` | What the answers in a thread change about a reading. |
| `core/typed_answers.py` | Whether a typed thread reply reads cleanly as an answer. |
| `core/write_request.py` | The InvenTree request body a decided line set becomes. |
| `core/marker.py` | The marker written into the notes of every stock change. |
| `core/marker_check.py` | Apply step 2: do the markers in the stock history agree with the store? |
| `core/requests.py` | The state machine of a request and its undo. |
| `llm/extract.py` | The LLM extractor: versioned prompt, strict output schema. |
| `llm/openai_compatible.py` | The provider adapter for OpenAI-compatible endpoints, hosted or local. |
| `llm/record.py` | Records a model's answers to the eval corpus, for scoring without a provider. |
| `slack/ingest.py` | What to do with one Slack payload, before anything else sees it. |
| `slack/listener.py` | Check, store, then acknowledge. |
| `slack/http_server.py` | Serving over HTTP ([0013](decisions/0013-http-serving-with-waitress.md)). |
| `slack/catchup.py` | Finds the messages Slack never delivered. |
| `slack/replies.py` | The thread replies: dry runs, questions and receipts. |
| `store/__init__.py` | The store's public names, re-exported from `store/db.py`. |
| `store/db.py` | Posts, plan revisions, requests, attempts and the append-only audit log. |

## Rules the layout keeps

- **`core/` is pure.** It imports only the standard library and other `core/` modules: no network,
  no database, no Slack. Most of the logic lives here and is tested without mocks
  (`tests/core/`).
- **One way to change stock.** `inventree.py` refuses every method but GET before a request leaves
  the process. `writer.py` refuses anything but a POST of a body built by
  `core/write_request.build_request` to one of the four stock adjustment paths. `run.py` opens it
  only when writes are on, and only `apply.py` sends through it, for the requests of a verdict the
  gate allowed.
- **Store first.** Every state change in `store/db.py` is one transaction that writes its audit row
  with it. The intent of a write is on disk before the write is sent, so a crash leaves an unknown
  outcome to recover, never a silent one.
- **No message text in logs.** Log lines name ids, states and error classes. Text from Slack and
  response bodies from InvenTree stay out, and so do tokens.

## Tests

`tests/` mirrors the modules: `tests/core/` for the pure core, `tests/test_<module>.py` for the
rest. `tests/integration/` runs against a real InvenTree in Docker (see [versions](versions.md)),
`tests/fixtures/` holds recorded Slack payloads, and `eval/` holds the synthetic message corpus the
extractors are scored on. `make check` runs lint, types, unit tests and the eval;
[CONTRIBUTING.md](../CONTRIBUTING.md) has the details.
