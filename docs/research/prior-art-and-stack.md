# Prior art and stack research

Research date: 2026-09-28. Method: public web only (web search, GitHub REST API, PyPI JSON API, npm registry, vendor docs).
Star counts and "last activity" values are snapshots from that date. Items that could not be confirmed are listed under "Unverified" at the end.

## Key takeaways

1. **Confirmed gap.** No open-source project was found that turns free-text Slack messages into InvenTree stock movements. A GitHub repository search for `inventree slack` returned zero results; the only Slack-related InvenTree code is the built-in, one-way (InvenTree to Slack) notification plugin.
2. **Closest prior art is small and recent.** `guyiyu/inventree-telegram-bot` (Telegram + Gemini, 0 stars, April 2026) and several InvenTree MCP servers (including the official `inventree/inventree-mcp`, created July 2026, read-only by default) show demand for conversational access but none offer a review queue, a rule-based fallback, or an eval set.
3. **The pattern worth copying is "propose, then confirm".** Independent hobby projects (`Sambridda/InventoryBot`, `debugsito/homebox-mcp`, `RicterZ/AL1S-WMS`) all converge on: nothing is written until a human confirms, with idempotency keys for writes. The official InvenTree MCP plugin ships with `MCP_READ_ONLY` defaulting to `True`.
4. **Naming: avoid leading with "Slack".** Slack's Marketplace guidance gives the explicit example "'Task notifications for Slack' rather than 'Slack task notifications'", and Slack's brand terms forbid domains and trademarks containing "slack". `slack-inventree-connector` and `slackstock` conflict with that guidance; `tallybot` is already taken on PyPI by a Slack/email chat assistant.
5. **`stockthread` and `stockpost` are the cleanest candidate names** (free on PyPI and npm, only trivial GitHub name collisions). `partline` is free on registries but collides with existing companies and an active MIT project of the same name.
6. **All four LLM provider families now support JSON-schema-constrained output**, and all accept a schema generated from a Pydantic model. Anthropic, OpenAI and llama.cpp document constrained decoding; schema-valid output is still not guaranteed on refusal or token-limit truncation, so local Pydantic validation remains mandatory.
7. **Recommendation: hand-rolled `Extractor` protocol with thin per-provider adapters**, each provider SDK as an optional extra. This keeps the rule-based parser dependency-free and avoids litellm's heavy install (boto3, tokenizers, tiktoken, aiohttp are required dependencies) and its March 2026 PyPI supply-chain incident exposure.
8. **Plain pytest + JSONL is sufficient for the eval set.** Dedicated tools (promptfoo, inspect-ai, deepeval) add value mainly for LLM-as-judge and dashboards, which deterministic field extraction does not need.
9. **httpx has been forked.** Pydantic now maintains `httpx2` (2.13.1, Sept 2026); the `anthropic` and `openai` SDKs depend on `httpx2`, while `google-genai` and `ollama` still depend on `httpx` (last release 0.28.1, Dec 2024). This affects HTTP-mocking and cassette tooling choices.
10. **No copyleft in the proposed core stack.** Everything in Part C is MIT, BSD-3-Clause, Apache-2.0, or dual MIT/Apache. Copyleft appears only in prior art that must not be copied from (Ki-nTree GPL-3.0, grocy-telegram-bot AGPL-3.0, AL1S-WMS GPL-3.0).

---

## Part A - Prior art

### A1. Slack and InvenTree integrations

| Name | URL | What it does | Last activity | Stars | License | Learn / reuse (ideas only) |
|---|---|---|---|---|---|---|
| InvenTree built-in Slack Notification plugin | https://docs.inventree.org/en/stable/plugins/builtin/slack_notification/ | Sends InvenTree event notifications to a Slack channel. Implements `NotificationMixin`. One setting: a Slack API key. Optional; disabled by default. One-way only (InvenTree to Slack). | Ships with InvenTree core (latest release 1.5.6, 2026-09-26) | n/a (core repo ~7.6k) | MIT | Confirms Slack is a recognised channel for InvenTree users. Our tool is the inverse direction, so there is no functional overlap, but the name `inventree-slack` would be easily confused with it. |
| inventree-apprise | https://github.com/invenhost/inventree-apprise (fork: https://github.com/matmair/inventree-apprise) | Notification plugin that sends via Apprise URLs (Slack, Discord, Teams, Matrix and others). One-way. | 2026-08-18 (invenhost) | 4 | MIT | Compatibility table in README mapping plugin versions to InvenTree versions is a good convention. |
| inventree-zapier | listed at https://inventree.org/plugins | Exposes InvenTree events to Zapier. One-way event export. | not checked | not checked | not checked | Shows the "event out" side is covered by existing plugins. |

Finding: **no two-way or inbound Slack integration exists.** GitHub repository search `inventree slack` and `inventree-slack OR slack-inventree` both returned 0 results. `inventree discord` returned 0. Nothing relevant on the official plugin list (24 plugins reviewed) or the `inventree` GitHub topic. No Reddit or GitHub Discussions thread requesting this was surfaced by search.

### A2. Chat-to-inventory tools (any backend)

| Name | URL | Backend / chat | What it does | Last activity | Stars | License | Learn / reuse (ideas only) |
|---|---|---|---|---|---|---|---|
| inventree-telegram-bot | https://github.com/guyiyu/inventree-telegram-bot | InvenTree / Telegram | Natural-language queries, photo recognition, create/update parts, stock items, locations via Gemini function calling. Python, `python-telegram-bot`, `httpx`, Pydantic settings, Docker Compose. 25 commits. | 2026-04-11 | 0 | README says MIT; GitHub API reports no detected license | Closest functional prior art. Gemini-only, home-inventory oriented, no review queue, no rule-based fallback, no eval set. Model fallback chain for rate limits is a useful idea. |
| mcelroy-inventory | https://github.com/echo313unfolding/mcelroy-inventory | InvenTree + PWA + MCP | Shop/field parts system on top of InvenTree; MCP server with 10 tools; writes gated behind an environment flag. | 2026-07-02 | 1 | Internal/proprietary statement in README | Evidence of a real field-service use case on InvenTree. Write-enable flag is a sensible safety default. Do not reuse code. |
| ha-inventree | https://github.com/benoit505/ha-inventree | InvenTree / Home Assistant | Sensors plus services that adjust stock from Home Assistant automations. | 2025-11-17 | 11 | none detected | Shows stock adjustment via REST from an external service is practical. |
| slack-snipeit-bot | https://github.com/mmeyer724/slack-snipeit-bot | Snipe-IT / Slack | Read-only asset lookups from Slack (RTM API). 3 commits. | 2019-08-08 | 0 | none | Dormant. Uses the deprecated RTM approach; nothing to reuse. |
| Snipe-IT native Slack integration | https://github.com/snipe/snipe-it/issues/5270 | Snipe-IT / Slack | Native integration is webhook notifications on checkout/checkin. A 2018 request for two-way Slack actions was closed (stale). | 2018 (issue) | n/a | AGPL-3.0 (Snipe-IT core) | Same gap exists in the Snipe-IT ecosystem: notifications out, nothing in. |
| grocy-telegram-bot | https://github.com/markusressel/grocy-telegram-bot | Grocy / Telegram | Fixed slash-style commands (add, consume, chores, shopping list). No NLP. On PyPI and Docker Hub. | Archived 2020-09-10 | 5 | AGPL-3.0 | Command vocabulary (add/consume) is a reasonable baseline for a rule-based grammar. Copyleft: ideas only. |
| Grocy MCP servers | https://github.com/saya6k/mcp-grocy-api , https://github.com/attmous/grocy-mcp | Grocy / MCP | Expose Grocy stock operations to AI agents. About 12 repos found. | 2026-04 to 2026-09 | 29 / 11 | MIT | MCP is the dominant 2026 pattern for LLM-to-inventory; chat platforms are secondary. |
| homebox-mcp | https://github.com/debugsito/homebox-mcp | Homebox / Telegram + MCP | TypeScript. Natural-language tool calling; photo produces a proposal with confirm buttons, writes nothing until confirmed. Groq or Gemini with failover. | 2026-09-14 | 0 | MIT | Propose-then-confirm with buttons maps directly to Slack Block Kit actions. README notes providers accept different JSON Schema subsets. |
| InventoryBot | https://github.com/Sambridda/InventoryBot | SQLite / Discord | Draft, review, confirm workflow; drafts expire after 24 h with a reminder at 20 h; Gemini for PDF ingestion. | 2026-09-14 | 0 | none (README: "use as you see fit") | Draft expiry and reminder timing is a good idea for a review queue. |
| AL1S-WMS | https://github.com/RicterZ/AL1S-WMS | Own SQLite / MCP | Household WMS with MCP endpoint; requires idempotency keys on inventory writes and explicit confirmation. | 2026-09-28 | 32 | GPL-3.0 | Idempotency keys for stock writes. Copyleft: ideas only. |
| frappe_telegram | https://github.com/leam-tech/frappe_telegram | ERPNext / Telegram | Generic Telegram bot framework for Frappe; not stock-specific. | 2024-01-15 | 44 | MIT per README; GitHub reports NOASSERTION | Generic plumbing only. |
| stockbot | https://github.com/HerinPatel98/stockbot | Own SQLite / Streamlit | LLM tool calling over a SQL ledger; shows fuzzy-match resolution trace before executing. | 2026-09-28 | 0 | none | Showing the resolution trace to the reviewer is a good UX idea. |
| slack-mcp-client | https://github.com/tuannvm/slack-mcp-client | any MCP / Slack | Go bridge from Slack to MCP servers (OpenAI, Anthropic, Ollama). | Archived 2026-04-12 | 171 | MIT | A generic Slack-to-MCP bridge plus `inventree-mcp` is the nearest "assemble it yourself" alternative; it is archived and agent-style (no deterministic parser, no review queue). |

Nothing found for: PartKeepr, Part-DB (no bots or MCP servers surfaced), Odoo or ERPNext open-source chat-to-stock-entry bots (only commercial services and n8n templates), Teams or WhatsApp bots for any of the listed systems.

### A3. InvenTree ecosystem projects as structural models

| Name | URL | What it is | Last activity | Stars | License | Conventions observed |
|---|---|---|---|---|---|---|
| inventree-python | https://github.com/inventree/inventree-python | Official Python REST client. PyPI name `inventree` (0.23.2, 2026-05-13). Depends on `requests`, `urllib3` (synchronous). | 2026-08-04 | 50 | MIT | Integration tests via `invoke test`, which starts an InvenTree server in Docker (docker-compose) and resets a test database. Pre-commit, CI, coverage badge. |
| inventree-mcp (official) | https://github.com/inventree/inventree-mcp | MCP server packaged as an InvenTree plugin; tools wrap InvenTree REST views and run as the authenticated caller. | 2026-09-22 (created 2026-07-26) | 3 | MIT | `MCP_READ_ONLY` defaults to `True`. Token auth. PyPI `inventree-mcp`. README order: design, setup, configuration, auth. |
| inventree-mcp-plugin (community) | https://github.com/eljefedelrodeodeljefe/inventree-mcp-plugin | Community MCP plugin with write tools (adjust stock, transfer). | 2026-02-26 | 5 | MIT | Recommends a dedicated service account with a role matrix; Docker Compose integration tests; semver releases. |
| Other InvenTree MCP servers | syntaxerr66, munin92, davidvanlaatum, yoo2001818, hvkshetry | 9 repos total in search | 2026-02 to 2026-09 | 0 to 4 | mostly none | Fragmented; several have no license. |
| Ki-nTree | https://github.com/sparkmicro/Ki-nTree | GUI tool creating parts in KiCad and InvenTree from supplier APIs. PyPI `kintree`. Poetry. | 2026-07-09 | 251 | **GPL-3.0** | A standalone tool with its own brand name, not the `inventree-` prefix. Copyleft: ideas only. |
| inventree-part-import | https://github.com/30350n/inventree-part-import | CLI importing supplier parts into InvenTree. | 2026-06-17 | 89 | MIT | Standalone tool using the `inventree-` prefix; YAML config files; pipx install recommended. |
| InvenTree mobile app | https://github.com/inventree/inventree-app | Official Flutter app for Android/iOS. | 2026-09-28 | 145 | MIT | Separate repo, CONTRIBUTING.md and BUILDING.md, user docs hosted in main InvenTree docs. |
| Official plugin list | https://inventree.org/plugins | 24 plugins, mostly named `inventree-<thing>` or `inventree-<thing>-plugin`. | n/a | n/a | n/a | Disclaimer: listing "does not mean that the plugin is officially supported by the InvenTree core team". |
| plugin-creator | https://github.com/inventree/plugin-creator | Cookiecutter-based scaffolding tool for plugins. | n/a | n/a | MIT default for generated plugins | Generates `pyproject.toml`, optional CI publishing to PyPI. |

Conventions summary:
- **License:** MIT is the overwhelming norm (InvenTree core, client, app, official plugins, plugin-creator default).
- **Naming:** `inventree-<thing>` is used for both plugins and standalone tools; branded names (Ki-nTree) also exist. PyPI package name usually equals the repo name.
- **Packaging:** `pyproject.toml`; PyPI publishing from CI; README includes an InvenTree version compatibility note.
- **Integration testing:** a real InvenTree server in Docker Compose, not mocks.
- **Safety defaults:** read-only by default, dedicated service account and token, writes behind an explicit setting.

### A4. Name availability

Checked via `https://pypi.org/pypi/<name>/json`, `https://registry.npmjs.org/<name>` (HTTP 404 means no such package) and the GitHub repository search API (`<name> in:name`).

| Candidate | PyPI | npm | GitHub conflicts | Brand / trademark notes |
|---|---|---|---|---|
| `slack-inventree-connector` | free (404) | free (404) | none found | Leads with "Slack": contrary to Slack Marketplace naming guidance. Descriptive but risky as a public brand. |
| `inventree-slack` | free (404) | free (404) | none found | Reads as an InvenTree plugin and is easily confused with the built-in Slack notification plugin. Still contains "slack" (not in a "for Slack" form). |
| `stockthread` | free (404) | free (404) | 2 student C# repos (`thanhhle/StockThread1`, `StockThread2`, 2019, 0 stars) | No product of this name found by web search. Cleanest candidate. |
| `slackstock` | free (404) | free (404) | `savala/slackStocks` (15 stars, 2016) and other share-price bots | Contains "slack" as part of a coined word, which Slack's brand terms discourage; also reads as a share-price bot. |
| `partline` | free (404) | free (404) | 6 repos, including `Zachshotamartin/partline` (MIT, active Sept 2026, 3D model review) | Partline Ltd (UK engineering group, founded 1955) and a US "Partline, Inc." exist. Moderate conflict risk. |
| `tallybot` | **taken**: `tallybot` 0.0.2 (2025-09-28), MIT, "Chat assistant to keep track of your accounting. Able to answer on slack and email messages." | free (404) | 22 repos named tallybot | Direct conflict in a neighbouring domain (Slack chat assistant). Not recommended. |
| `stockpost` | free (404) | free (404) | 3 trivial repos (0 stars) | No product of this name found by web search. Clean candidate. |

Caveat: a 404 from the registry APIs shows the name is not currently published; it does not prove the name is not reserved, previously deleted, or blocked by a registry similarity rule.

**Slack guidance (cited):**
- Slack Marketplace guidelines, "Name" section: "If you do not own the service, make sure to not infringe on any trademarks or copyright (e.g., 'Task notifications for Slack' rather than 'Slack task notifications')."
- Slack Brand Terms of Service: "Don't use 'Slack' as a noun, verb, plural or possessive. An appropriate generic term must appear after the Slack trademark"; "Don't register a domain containing the word 'slack' or any variation thereof. Deliberate misspellings and transliterations are also not permitted"; "Do not apply for a trademark that includes the word 'slack,' our logo, or any other words or marks similar to our own"; "You are entitled to say that your application is integrated with Slack...".
- Practical reading: use a neutral project name and describe it as "for Slack and InvenTree" in the tagline. This matters most if the app is ever submitted to the Slack Marketplace; a self-hosted app's display name inside a workspace is chosen by the installer.

**InvenTree stance:** no published trademark or naming policy was found on inventree.org, docs.inventree.org or the GitHub organisation. De facto practice is permissive: many third-party projects use the `inventree-` prefix, and the plugin list carries only a "not officially supported" disclaimer. Recommended: neutral name plus "for InvenTree" in the description, state "not affiliated with the InvenTree project" in the README, and ask the maintainers before using the InvenTree logo.

### Sources - Part A

- https://docs.inventree.org/en/stable/plugins/builtin/slack_notification/
- https://inventree.org/plugins
- https://github.com/topics/inventree
- https://docs.inventree.org/en/latest/plugins/creator/
- https://github.com/inventree/InvenTree and https://api.github.com/repos/inventree/InvenTree/releases/latest
- https://github.com/inventree/inventree-python and https://github.com/inventree/inventree-python/blob/master/TESTING.md
- https://github.com/inventree/inventree-mcp
- https://github.com/eljefedelrodeodeljefe/inventree-mcp-plugin
- https://github.com/inventree/inventree-app
- https://github.com/sparkmicro/Ki-nTree
- https://github.com/30350n/inventree-part-import
- https://github.com/invenhost/inventree-apprise and https://github.com/matmair/inventree-apprise
- https://github.com/guyiyu/inventree-telegram-bot
- https://github.com/echo313unfolding/mcelroy-inventory
- https://github.com/benoit505/ha-inventree
- https://github.com/mmeyer724/slack-snipeit-bot
- https://github.com/snipe/snipe-it/issues/5270
- https://github.com/markusressel/grocy-telegram-bot
- https://github.com/saya6k/mcp-grocy-api and https://github.com/attmous/grocy-mcp
- https://github.com/debugsito/homebox-mcp
- https://github.com/Sambridda/InventoryBot
- https://github.com/RicterZ/AL1S-WMS
- https://github.com/leam-tech/frappe_telegram
- https://github.com/HerinPatel98/stockbot
- https://github.com/tuannvm/slack-mcp-client
- https://pypi.org/project/tallybot/ and https://github.com/Kolumbs/tallybot
- https://docs.slack.dev/slack-marketplace/slack-marketplace-app-guidelines-and-requirements/
- https://slack.com/terms-of-service/slack-brand
- GitHub search API queries: `inventree slack`, `inventree telegram`, `inventree discord`, `inventree bot`, `inventree mcp`, and each candidate name with `in:name`

---

## Part B - Structured LLM extraction with pluggable providers

### B5. Strict-schema structured output by provider

| Provider | Mechanism | Strength of guarantee | Image input | Python SDK (version, license) |
|---|---|---|---|---|
| Anthropic Claude | `output_config.format` with `type: "json_schema"` and `schema`; or tools with `strict: true`. Generally available. The older `output_format` parameter is deprecated. | Constrained decoding. Docs: "Always valid", "Guaranteed field types and required fields". Exceptions: refusals and `max_tokens` truncation. Schema limits: `additionalProperties: false` required on objects; no recursive schemas; no `minimum`/`maximum`/`minLength`/`maxLength`; `minItems` only 0 or 1. Compiled grammars are cached for 24 h; first request is slower. | Yes; docs show extraction from images. | `anthropic` 1.9.0 (2026-09-28), MIT. `client.messages.parse()` accepts a Pydantic model; SDK strips unsupported constraints and validates against the original model. |
| OpenAI | Responses API: `text.format` with `type: "json_schema"`, `strict: true`, `schema`. Chat Completions: `response_format` with `json_schema`. Function tools with `strict: true`. | Constrained decoding. Docs: model will "always generate responses that adhere to your supplied JSON Schema". Refusals arrive in a separate `refusal` field. Schema limits: all fields must be `required`; `additionalProperties: false` mandatory; optional fields expressed as nullable unions. | Yes per OpenAI's launch announcement ("compatible with vision inputs"). | `openai` 3.20.0 (2026-09-28), Apache-2.0. `client.responses.parse(text_format=Model)`. |
| Google Gemini | Current docs use the Interactions API: `client.interactions.create(..., response_format={"type": "text", "mime_type": "application/json", "schema": ...})`. The legacy Generate Content API uses `response_mime_type` plus `response_schema` or `response_json_schema`. | Docs promise syntactically valid JSON matching the schema but advise "always validate values in your application". Supports a JSON Schema subset; "very large or deeply nested schemas may be rejected". | Models are multimodal; the structured-output page does not show an image example (see Unverified). | `google-genai` 2.25.0 (2026-09-22), Apache-2.0. Pass `Model.model_json_schema()`. |
| Ollama (local) | `format` parameter on chat/generate: either `"json"` or a full JSON Schema object. Also via the OpenAI-compatible endpoint's `response_format`. | Grammar-constrained sampling locally; no formal guarantee stated. Docs recommend temperature 0 and restating the schema in the prompt. Ollama Cloud "currently does not support structured outputs". Quality depends on the chosen model. | Yes: "Vision models accept the same `format` parameter". | `ollama` 0.6.3 (2026-09-29 UTC), MIT. Depends only on `httpx` and `pydantic`. |
| llama.cpp | GBNF grammars; `llama-server` converts JSON Schema to a grammar when given `json_schema` or `response_format: {type: "json_schema", json_schema: {schema: ...}}`. | Hard constraint at sampling time. The schema is not injected into the prompt, so the prompt must describe the fields. Unsupported: `uniqueItems`, `contains`, `if/then/else`, remote `$ref`; `prefixItems` is broken; `pattern` needs `^...$`. | Depends on the multimodal model and server build (see Unverified). | `llama-cpp-python` 0.3.35 (2026-08-17), MIT, source distribution only (compiles on install). Talking to `llama-server` over HTTP avoids this dependency. |

Portable schema rules (lowest common denominator across all five):
- Flat or shallow objects; every property listed in `required`; `additionalProperties: false`.
- Optional values as `anyOf: [<type>, {"type": "null"}]`, not by omission.
- `enum` for action types (for example `take`, `return`, `transfer`, `count`); no numeric range or string length keywords; enforce ranges in Pydantic after parsing.
- No recursion. Keep `line_items` as an array of one simple object type.
- Always re-validate with the original Pydantic model and treat refusal, truncation and validation failure as "route to human review".

### B6. Provider abstraction options

| Option | Status (2026-09) | Required dependencies | License | Fit |
|---|---|---|---|---|
| Hand-rolled `Protocol` + adapters | n/a | None in core. Each adapter imports its SDK lazily; SDKs installed as extras (for example `[anthropic]`, `[openai]`, `[gemini]`, `[ollama]`). | project's own | Best fit. Roughly 40 to 80 lines per adapter. The rule-based parser implements the same protocol, so "no LLM" is simply the default adapter. |
| `litellm` | 1.103.0 (2026-09-27); very active; about 59.8k stars, about 5.4k open issues | Heavy: `openai`, `tiktoken`, `tokenizers`, `aiohttp`, `boto3`, `jsonschema`, `jinja2`, `click`, `pydantic-settings`, `httpx[http2]` and more are all required | MIT, except the `enterprise/` directory which has its own licence (GitHub reports NOASSERTION) | Poor fit for a small codebase. Versions 1.82.7 and 1.82.8 were malicious uploads on 2026-03-24 (credential stealer via a `.pth` file, live about 40 minutes). Pin with hashes if ever used. |
| `instructor` | 1.17.0 (2026-09-09); active; about 14k stars | Medium: `openai` is a required dependency even for other providers; also `aiohttp`, `requests`, `rich`, `typer`, `tenacity`, `jinja2` | MIT | Good ergonomics (Pydantic in, validated object out, retry on validation error) but forces the OpenAI SDK and a CLI/UI stack into every install. |
| `pydantic-ai` (`pydantic-ai-slim`) | 2.51.0 (2026-09-25); very active; about 20k stars | Slim core is moderate: `pydantic`, `pydantic-graph`, `httpx2`, `anyio`, `opentelemetry-api`, `genai-prices`, `griffelib`; providers via extras | MIT | Best of the three libraries if a library is wanted: provider extras, typed outputs, test models. Still an agent framework with a fast release cadence (weekly minor versions), more surface than single-call extraction needs. |

Recommendation:
1. Define one protocol in core, for example `class Extractor(Protocol): async def extract(self, message: MessageInput) -> ExtractionResult`. `ExtractionResult` is a Pydantic model carrying line items, per-field confidence or provenance, and the extractor identity.
2. Ship `RuleBasedExtractor` in core with zero third-party LLM dependencies. It must pass a defined subset of the eval set by itself.
3. Add one adapter module per provider, each using that provider's native strict-schema feature from B5 and its official SDK, imported inside the adapter so that a missing SDK raises a clear configuration error only when that provider is selected.
4. For Ollama and llama.cpp, a plain HTTP call is enough; no SDK is needed.
5. Re-evaluate `pydantic-ai-slim` only if multi-step tool use (for example, the model querying the catalogue) becomes a requirement.

### B7. Versioned eval sets for extraction

| Tool / example | URL | What it offers | Licence, status | Relevance |
|---|---|---|---|---|
| promptfoo | https://github.com/promptfoo/promptfoo | Declarative YAML tests; assertions `equals`, `is-json` (with schema), `contains-json`, custom `python`/`javascript`; per-assertion `weight`, test `threshold`, named `metric` aggregation; CSV test files with `__expected` columns. | MIT; TypeScript; about 25.5k stars; active | Strong for prompt comparison across providers. Requires Node.js in a Python project. |
| inspect-ai | https://inspect.aisi.org.uk/ | Dataset loaders (JSON/JSONL/CSV), built-in scorers (`match`, `exact`, `includes`, `f1`, model-graded), custom scorers returning multiple metrics, persistent eval logs with a viewer and re-scoring. | MIT; 0.3.272 (2026-09-28); UK AI Security Institute | Good model of per-sample logs and custom scorers. Heavier than needed for deterministic scoring. |
| deepeval | https://pypi.org/project/deepeval/ | pytest-style LLM tests, many LLM-judged metrics, datasets of "goldens". | Apache-2.0; 4.2.6 (2026-09-24) | 28 required dependencies including `openai`, `posthog`, `grpcio`, OpenTelemetry. Oriented to LLM-as-judge; not needed here. |
| vcrpy + pytest-recording | https://github.com/kevin1024/vcrpy , https://github.com/kiwicom/pytest-recording | Record HTTP once, replay in CI. pytest-recording defaults to record mode `none`, adds `--block-network`, and a `vcr_config` fixture with `filter_headers` for secret redaction. | MIT; vcrpy 8.3.0 (2026-07-04); pytest-recording 0.13.4 (2025-05-08) | The standard way to keep CI key-free. See the httpx2 caveat under Unverified. |
| llm-eval-harness | https://github.com/jt-mchorse/llm-eval-harness | `fixtures/*.jsonl` rows with `id`, `input`, `expected_outputs`, `tags`, `dataset_version`, `provenance`; SQLite run history; non-zero exit on regression beyond a threshold; stub backends so tests run without keys. | MIT; 0 stars; small personal project | A concrete example of the JSONL row shape and regression gating; judge-based scoring is not needed for our case. |

Is plain pytest + JSONL sufficient? **Yes.** Extraction output is structured and has a single correct answer per case, so scoring is deterministic and needs no judge model. Suggested shape (ideas only):
- **Cases:** `eval/cases/*.jsonl`, one JSON object per line: `id`, `text`, optional `image` path, `catalog_fixture` (which synthetic catalogue snapshot applies), `expected` (list of line items with part reference, quantity, action, source and destination), `tags` (for example `typo`, `multi-item`, `ambiguous`, `no-op`), `added_in` (dataset version). Synthetic data only, for example "2 U7-Lite on Sam's truck for the Acme Fiber job".
- **Scoring:** per field, exact match for `quantity` and `action`; normalised exact match for part identity after resolution; line items compared as a set, giving precision, recall and F1 per case (partial credit). Also report strict case-level exact match. Report "abstained / sent to review" separately from "wrong": a confident wrong answer is worse than an abstention.
- **Slices:** aggregate by tag and by extractor (rule-based, each provider and model).
- **Tracking over time:** write a JSON summary per run (dataset version, extractor, model id, prompt version, scores), upload it as a CI artefact, and commit a baseline file; fail CI when a score drops more than a tolerance below baseline.
- **No API key in CI:** replay recorded provider responses. Either HTTP cassettes (pytest-recording with default mode `none` and `--block-network`, headers such as `authorization` and `x-api-key` filtered), or a simpler response cache keyed by a hash of provider, model, prompt version and input, stored as JSON fixtures. Re-record locally with a key when prompts or models change. The rule-based extractor needs no fixtures and runs live on every CI run.

### Sources - Part B

- https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- https://developers.openai.com/api/docs/guides/structured-outputs
- https://openai.com/index/introducing-structured-outputs-in-the-api/
- https://ai.google.dev/gemini-api/docs/structured-output
- https://ai.google.dev/gemini-api/docs/generate-content/structured-output
- https://docs.ollama.com/capabilities/structured-outputs
- https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md
- https://pypi.org/project/anthropic/ , https://pypi.org/project/openai/ , https://pypi.org/project/google-genai/ , https://pypi.org/project/ollama/ , https://pypi.org/project/llama-cpp-python/
- https://pypi.org/project/litellm/ , https://github.com/BerriAI/litellm/blob/main/LICENSE , https://docs.litellm.ai/blog/security-update-march-2026
- https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
- https://pypi.org/project/instructor/ , https://github.com/567-labs/instructor
- https://pypi.org/project/pydantic-ai-slim/ , https://github.com/pydantic/pydantic-ai
- https://www.promptfoo.dev/docs/configuration/expected-outputs/
- https://inspect.aisi.org.uk/scorers.html , https://pypi.org/project/inspect-ai/
- https://pypi.org/project/deepeval/
- https://github.com/kevin1024/vcrpy/releases , https://pypi.org/project/vcrpy/
- https://github.com/kiwicom/pytest-recording , https://pypi.org/project/pytest-recording/
- https://github.com/jt-mchorse/llm-eval-harness

---

## Part C - Python stack sanity check

Versions are the latest on PyPI on 2026-09-28; dates are PyPI upload dates.

| Component | Latest version (date) | Licence | Status and note |
|---|---|---|---|
| FastAPI | 0.141.1 (2026-07-29) | MIT | Active. Requires Python >= 3.10 and `starlette>=0.46.0`. Still 0.x versioning; pin a minor range. |
| Starlette | 1.7.0 (2026-09-23) | BSD-3-Clause | Active, now 1.x. Sufficient by itself for a handful of webhook routes if FastAPI's extras are not needed. |
| httpx | 0.28.1 (2024-12-06) | BSD-3-Clause | No release for about 21 months; repository last pushed 2026-03-29. Still a dependency of `google-genai` and `ollama`. |
| httpx2 | 2.13.1 (2026-09-23) | BSD-3-Clause | Continuation of httpx maintained by Pydantic (repo created 2026-05-11, about 1.5k stars). Import name `httpx2`; forked from httpx 0.28.1; depends on `httpcore2`. Used by `anthropic`, `openai`, `pydantic-ai-slim`. Prefer for new code, subject to the test-tooling caveat in Unverified. |
| pydantic v2 | 2.13.5 (2026-08-28) | MIT | Active. Supports Python 3.9 to 3.14. Single source of truth for the extraction schema. |
| SQLAlchemy | 2.1.1 (2026-09-25) | MIT | Active. 2.1 requires Python >= 3.11. Worthwhile if the schema will grow or Postgres is a future option (with Alembic migrations). |
| `sqlite3` / aiosqlite | stdlib / 0.22.1 (2025-12-23) | PSF / MIT | For a few tables (messages, proposals, audit log) plain SQL through `aiosqlite` is smaller and easier to read. Reasonable default; migrate to SQLAlchemy only if needed. |
| rapidfuzz | 3.14.6 (2026-08-30) | MIT | Active. Requires Python >= 3.11. C++ extension with prebuilt wheels. Permissive, unlike the older GPL-licensed `fuzzywuzzy`/`python-Levenshtein` pairing. |
| structlog | 26.1.0 (2026-06-06) | MIT OR Apache-2.0 | Active. Requires Python >= 3.10. |
| pytest | 9.1.1 (2026-06-19) | MIT | Active. Requires Python >= 3.10. |
| ruff | 0.16.9 (2026-09-24) | MIT | Active; linter and formatter. Still 0.x; pin the version in CI and pre-commit. |
| mypy | 2.3.1 (2026-08-15) | MIT | Active, now 2.x. Pure Python install; has a Pydantic plugin. |
| pyright | 1.1.414 (2026-09-10) PyPI wrapper | MIT | The PyPI package is a community wrapper ("not affiliated with Microsoft") that downloads the Node-based checker. Astral's `ty` (0.0.84, MIT) is still classified Beta. Recommendation: pick one; mypy is the simpler contributor story (no Node runtime). |
| uv | 0.12.20 (2026-09-28) | MIT OR Apache-2.0 | Very active. Recommend `uv` with a committed `uv.lock` as the documented path, while keeping a standards-based `pyproject.toml` so `python -m venv` plus `pip install -e ".[dev]"` still works. |
| testcontainers (Python) | 4.15.0 (2026-07-24) | Apache-2.0 | Active. Good for a single throwaway service. InvenTree needs several containers (server, background worker, database, optional cache) and setup steps, so a Docker Compose file, as used by `inventree-python` and `inventree-mcp-plugin`, is the more conventional choice. Testcontainers can drive a compose file if pytest-managed lifecycle is wanted. |

Licence flags: nothing copyleft or unusual in this table. Notes: `litellm` has an `enterprise/` directory under a separate licence (Part B); Python floor should be 3.11 because SQLAlchemy 2.1 and rapidfuzz 3.14 require it.

### Sources - Part C

- https://pypi.org/project/fastapi/ , https://pypi.org/project/starlette/
- https://pypi.org/project/httpx/ , https://github.com/encode/httpx
- https://pypi.org/project/httpx2/ , https://github.com/pydantic/httpx2 , https://github.com/pydantic/httpx2/blob/main/src/httpx2/CHANGELOG.md
- https://pypi.org/project/pydantic/ , https://github.com/pydantic/pydantic
- https://pypi.org/project/SQLAlchemy/ , https://pypi.org/project/aiosqlite/
- https://pypi.org/project/rapidfuzz/ , https://pypi.org/project/structlog/
- https://pypi.org/project/pytest/ , https://pypi.org/project/ruff/ , https://pypi.org/project/mypy/ , https://pypi.org/project/pyright/ , https://pypi.org/project/ty/
- https://pypi.org/project/uv/ , https://pypi.org/project/testcontainers/
- https://github.com/inventree/inventree-python/blob/master/TESTING.md

---

## Unverified

1. **Trademark registers were not searched.** No USPTO, EUIPO or UKIPO lookup was done for any candidate name. "No product found" rests on web and GitHub search only. Partline company details come from search-result snippets (ZoomInfo, Bloomberg, LinkedIn listings), not primary sources.
2. **InvenTree naming policy.** No published policy was found; absence was inferred from searches, not confirmed with the maintainers.
3. **Slack Marketplace wording.** A search snippet attributed this sentence to Slack's guidelines: "Your app should not use 'Slack' in its name or domain name other than to note itself as a third-party integration ... 'My Service for Slack' is okay, but 'My Slack Service' is not." It could not be found on the current guidelines page, which now carries the "Task notifications for Slack" example instead. Treat the snippet as older or secondary wording.
4. **Registry 404 results** show the names are unpublished today; reserved, deleted or similarity-blocked names were not tested. Scoped npm names and PyPI normalised variants (underscores, dots) were not checked.
5. **GitHub search coverage.** Unauthenticated API searches were rate-limited and some combined queries returned noise (the Snipe-IT and Part-DB/PartKeepr queries in particular). Absence of results for Part-DB, PartKeepr, Odoo and ERPNext bots is "not found", not "proven not to exist". Reddit was searched only through a general web search.
6. **inventree-telegram-bot licence.** README states MIT but the GitHub API reports no detected licence file. Whether it adjusts stock quantities (rather than only creating and updating records) is not stated in its README.
7. **inventree-zapier** details (licence, stars, last activity) were not retrieved.
8. **Gemini structured output with image input** is not demonstrated on the structured-output docs page; support was inferred from secondary sources. The relationship between the newer Interactions API and the legacy Generate Content API (which one to target) should be confirmed before writing the adapter.
9. **OpenAI vision plus structured output** is confirmed by the 2024 launch announcement, not by the current guide page.
10. **llama.cpp image input with grammar constraints** was not verified.
11. **Cassette tooling with httpx2.** vcrpy release notes for 8.2.0 say "Add support for httpx 2.x" and "Patch httpx transports instead of httpcore"; it is unclear whether this covers the separate `httpx2` package (which uses `httpcore2`). pytest-recording's last release (0.13.4, May 2025) predates httpx2. Release dates shown on the vcrpy GitHub releases page were read inconsistently by the fetch tool; the PyPI date for 8.3.0 (2026-07-04) is used here. Verify recording against the `anthropic` and `openai` SDKs before relying on cassettes; the hash-keyed response cache is the fallback.
12. **Star counts and open-issue counts** are point-in-time values from the GitHub API and web pages on 2026-09-28.
13. **Model names and versions shown in vendor documentation examples** were not checked and are deliberately not recommended here; choose models at implementation time.
