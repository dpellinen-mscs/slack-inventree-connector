# 0011: The model reads only what the rules decline, and only when configured

- Status: Accepted
- Date: 2026-09-30
- Decided by: agent (FYI to Dan)

## Context

There are two extractors (design section 9). The rule-based one needs no
network and reads the common one-line posts; it declines the rest. The LLM
extractor reads more, but every post it reads is sent to a provider, costs
a call, and can be slow or unavailable. The worker needs one rule for which
extractor reads a post, and for what happens when the provider gives no
answer.

## Decision

- **Off by default.** Without an `[llm]` section in the settings no post
  ever leaves the machine. Posts the rules decline then get no reply, as
  before.
- **Rules first.** The rule-based extractor reads every post. Only a post
  it *declines* goes to the model. A post the rules read, or read as "not a
  movement", is never sent. So the model cannot change what the rules read,
  text the rules can read stays on the machine, and the common posts cost
  no call and keep their reply time.
- **Same checks afterwards.** The model's candidates pass the same
  validator and matcher as the rules' candidates. The extractor's name
  (prompt version and model) is stored with every dry run.
- **No answer is an outage, not a reading.** The post waits and the
  provider is asked again with backoff (capped at one minute, and one call
  per interval for all waiting posts, counted from the end of the call
  that failed). After 15 minutes the post gets one last call of its own;
  if that fails too it is finished without a reply, and its record says
  `llm_unavailable`. It is never read as "not a movement".
- **A whole-call deadline.** `llm.timeout_seconds` (default 30, at most
  120) bounds the whole call, and the instance lock outlives it. One call
  runs at a time, and the queue is drained between two calls.
- One `[llm]` section: `base_url`, `model`, `timeout_seconds`. The key is
  the environment variable `LLM_API_KEY`, optional for a local server.

## Consequences

- A rule-based reading is never cross-checked by the model. The design's
  "the two extractors disagree" review reason would need both to read every
  post; it is left out until the eval scores show it is worth a call per
  post.
- A post that waited for the provider for more than 10 minutes is answered
  with the review reason `late_ingest`, like any late post.
- While declined posts get no reply (until M2's review replies), a long
  provider outage is silent in Slack. It is visible in the log and in the
  dry-run records.
- Whether a movement read by a model may ever apply without a click is a
  policy question for M3, not decided here.
