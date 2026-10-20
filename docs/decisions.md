# Design decisions and internal contracts

These decisions are shared by every part of the code. Code, SOP files and eval
scenarios must agree with them. When one changes, this file changes in the same commit.

## Clients and load types

| Client in the assignment | `broker.id` | Name | Notes |
|---|---|---|---|
| Client A | `northline` | Northline Logistics | The fixture `evals/fixtures/load_481207.json` |
| Client B | `keystone` | Keystone Freight | Scenarios override `broker` in `load_overrides` |
| Client B, refrigerated | `keystone` + load type `reefer` | | `load_overrides: {"load_types": ["reefer"]}` |

The SOP client id is the load's `broker_id`.

## SOP files

```
sops/
  standard/<topic>.md                          layer: standard
  clients/<client_id>/<topic>.md               layer: client
  clients/<client_id>/types/<load_type>/<topic>.md   layer: load_type
```

Each file is one section: YAML front matter between `---` lines, then a plain-English
Markdown body.

```yaml
---
id: delay_risk_high            # required, snake_case, the merge key across layers
title: Delay risk changes to high
description: One line. The model sees it in the catalog of sections.
intents: [delay_risk_high, eta_follow_up_due]   # empty or missing: every intent
stages: [to_pickup, to_delivery]                # empty or missing: every stage
mode: replace                  # replace (default) or extend; ignored in standard/
---
```

Merge, per `id`, least to most specific (standard, client, load type): `replace` swaps the
body and the metadata; `extend` appends its body after the inherited body and keeps the
inherited title, description, intents and stages. An `extend` with nothing to extend is a
lint error. A section applies to an event when its intents include the event's intent (or
are empty) and its stages include the load's stage (or are empty).

Canonical section ids (scenarios tag them as `sop:<id>`):

| id | Layer(s) | Intents | What it says |
|---|---|---|---|
| `communication_rules` | standard | all | Tone, only the load's contacts, never share rates or prices, short SMS, no promises the agent cannot keep |
| `escalation` | standard | all | When and how to create a Hero task, when it is urgent, what to write in it |
| `delay_risk_high` | standard, `northline`, `keystone` | `delay_risk_high`, `eta_follow_up_due` | The high-risk procedure (see below) |
| `delay_risk_cleared` | standard | `delay_risk_low` | Risk is back to normal: close the open ETA request as cancelled, no messages |
| `eta_received` | standard | `eta_provided` | Close the ETA request as satisfied and write the ETA, as a local clock time, in the TMS. A relative ETA ("in 30 min") is converted from the message time. A vague answer ("soon", "on my way") is not an ETA: send nothing, keep the request open, the follow-up continues |
| `eta_deferred` | standard | `eta_deferred` | The contact promises an answer later: accept one deferral of up to 15 minutes, send nothing |
| `driver_questions` | standard | `question` | Answer from the load metadata when the answer is there; otherwise a Hero task; never guess |
| `acknowledgements` | standard | `acknowledgement` | "ok", "thanks": do nothing; an acknowledgement is not an ETA |
| `status_updates` | standard | `status_update` | Write a TMS note when the update matters; otherwise nothing |
| `driver_departed` | standard, `keystone/types/reefer` (extend) | `driver_departed` | Standard: update the stage and write a TMS note. Reefer: also ask the driver for the trailer temperature and the seal number |
| `documents` | standard | `document`, `document_processed` | Document handling (stub: create a Hero task to review the document; card W3-DOC writes the `document_processed` procedure) |

High-risk procedures (the scenarios depend on these exact rules):

- **standard:** SMS the driver for the ETA. No ETA after 30 minutes: SMS again. No ETA 30
  minutes after the second SMS: create a Hero task to call the driver.
- **`northline` (Client A), replace:** SMS the driver for the ETA. No ETA after 30 minutes:
  SMS again. No ETA after the two SMS: email the dispatcher in the main thread, then
  create a Hero task.
- **`keystone` (Client B), replace:** one SMS to the driver. No ETA after 30 minutes: alert
  the broker's Slack channel. Never contact the dispatcher.

When no section applies to the event, the run escalates to a Hero ("no SOP for this
situation") and records the gap. The agent does not improvise. General sections (empty
`intents`, such as `communication_rules` and `escalation`) apply to every event, so they do
not count for this check: the run escalates when no section with an explicit intent matches.

The decide prompt also shows, as context only, the sections of procedures still in progress
(the follow-up intent of each open obligation), so that an answer to a driver's question at
07:13 knows the ETA request continues. The model is told not to run those steps now.

## Intents and the gate

| Event | Intent | Model call |
|---|---|---|
| `risk_changed` to `high` | `delay_risk_high` | none |
| `risk_changed` from `high` to lower | `delay_risk_low` | none |
| `timer_due` for an `eta_request` obligation | `eta_follow_up_due` | none |
| `attachment_received` | `document` | none |
| `document_processed` (the document pipeline read an attachment) | `document_processed` | none |
| `inbound_message` with attachments and no text | `document` | none |
| `inbound_message` | from the gate model | `gate` role, `max_tool_rounds = 0` (direct structured output, no tools) |
| `hero_update` | `other`, no action | none |

The run continues to decide when `decision.act` is true or the intent's `must_act` is set
in the load's broker catalog (`IntentDef.must_act`; the standard catalog keeps the old
`MUST_ACT` set). Otherwise the run ends as `noop` with one model call (the gate). A message
can only have a message intent of the catalog (`Broker.message_intents`); any other answer
of the gate model becomes `other`, which still acts.

Only a confirmed risk transition into or out of `high` becomes an event. `medium` is
tracked but does not wake the agent. On every confirmed transition the runtime saves the
load with `delay_risk` set to the new band, in the same unit of work as the tracker.

Pings are delivered at least once. The tracker keeps the ids of the last 64 pings it
counted (`RiskTracker.recent_ping_ids`), stored with it in the same unit of work: a ping
whose id it holds is a redelivery and changes nothing, after a restart or on another
process too. The simulation API does not move its clock for a redelivered message or ping.

## Run context

`application.context.load_run_context` reads the load's memory once per run. `recent_events`
are the last N events of the load in log order, **without the run triggers logged after the
event being processed** (`domain.events.RUN_TRIGGERS`): messages, pings and timers queued
behind it are already in the log but are not history for this run (they get their own runs).
Every `agent_action` is kept, wherever it falls: a message queued ahead of a timer event can
commit its reply after the timer event was logged, and the timer's run must see that reply,
or it may send it again.
`recent_outbound` is built from `agent_action` events in the rate window, however many
events that takes; a broker Slack post counts against the first broker contact on Slack.

## Prefilter (code, before any model call)

| Condition | Result |
|---|---|
| A run already exists for the event | `skipped`, `already_processed` |
| `timer_due` and the timer is stale (`domain.timers.is_stale`) | `skipped`, `stale_timer` |
| `inbound_message` with an empty body and no attachments | `skipped`, `empty_message` |
| `inbound_message` whose sender is not a contact of the load | `escalated`, `unknown_sender`: one Hero task, no model call |

## Run pipeline

`load_context` → `prefilter` → `gate` → `resolve_sop` → `decide` → `validate` →
`commit_execute`, with exits `noop`, `skipped` and `escalated`. One run per event. The
whole run's state changes (ledger, timers, outbox, load, run record) are committed in one
unit of work. Delivery happens afterwards from the outbox: the dispatcher checks each
item's payload first, and a malformed one fails at once (no retry can fix it); anything
the gateway call raises keeps the item pending, with `attempts` and `last_error`, until
`max_delivery_attempts` makes it `failed`, and the rest of the batch is still sent.

Run status: `skipped` (prefilter), `noop` (gate does not act, or the plan is empty, or
every action was dropped, or the executor skipped every approved action), `acted` (at
least one approved action applied), `escalated` (the run created a Hero task because of a
failure path: unknown sender, no SOP section, model failure or timeout, a high-severity
validator violation), `failed` (unexpected error; the event goes back to the queue and
becomes a dead letter after too many attempts).

A model failure (`LlmError`, after fallbacks) or a run over its deadline creates one
urgent Hero task and completes the event, so the event is never lost and never retried
into a duplicate.

**The claim lease (watchdog).** A worker that dies between claim and commit (a crash, a
cancelled task, a failure while releasing) leaves its event in flight, and the load's
later events would wait forever. The queue records the claim time from the runtime's
clock (never the database's: evals and the simulation run on a virtual clock), and every
`process_all` (so every `tick`) first calls `EventQueue.reclaim_expired(now, lease_s)`:
events in flight for more than `RuntimeConfig.claim_lease_s` (default `run_deadline_s`
plus 60 s) go back to their place in the queue, each counted as a failed attempt, so an
event whose runs always die still becomes a dead letter. A run that was only slow and
commits after its event was reclaimed changes nothing in the queue (`complete` and
`release` leave an event that is not in flight alone); its stored run makes the event's
next run a skip (`already_processed`).

## Executor semantics

The executor applies approved actions in plan order, inside the run's unit of work.
`now` is the clock's time when the run executes.

| Action | Effect |
|---|---|
| `send_sms` with `requests: <kind>` (`purpose: eta_request` means `requests: eta_request`) | If no open obligation of that kind exists, open one (`attempts = 1`); otherwise `record_attempt`. Outbox `sms`. Only an SMS counts; an email's `requests` is not applied yet (card W3-AGT decides). |
| other `send_sms` | Outbox `sms` only. |
| `send_email` | Outbox `email` (main thread). |
| `post_broker_slack` | Outbox `broker_slack`. |
| `write_tms_note` | Outbox `tms_note`. |
| `create_hero_task` | Outbox `hero_task`. |
| `post_internal_slack` | Outbox `internal_slack`. |
| `update_stage` | Save the load with the new stage. Outbox `stage_update`. |
| `schedule_timer` | Needs the open obligation of that kind. `due = follow_up_due_at(requested=now + due_in_minutes, deferred_until=obligation.deferred_until, grace)`. Every pending timer of the obligation becomes `superseded`; the new timer becomes the obligation's `active_timer_id`. |
| `defer_obligation` | `until = event.occurred_at + minutes` (the message time, not the processing time); `obligation.defer(until, now=event.occurred_at, policy)`. If the obligation has an active timer, the executor replaces it with one due at `follow_up_due_at(requested=<old due_at>, deferred_until=until, grace)`; the old one becomes `superseded`. |
| `close_obligation` | Close the open obligation of that kind with the status; every pending timer of it becomes `cancelled`. |
| `open_obligation` | Open an obligation of that kind with `attempts = 0` (a document the load owes); skipped when one is already open. No outbox item. |

An action that would break a domain invariant (for example `schedule_timer` with no open
obligation) is skipped and reported in `ExecutionResult.skipped`; the rest of the plan runs.
The run records the skips in `RunRecord.skip_reason` (`executor skipped action <index>
(<type>): <reason>; ...`); when the executor skipped every approved action the run is
`noop`, and with some applied it stays `acted`. The validator normally drops such actions
first. Validator rules evaluate each action against the state left by the actions approved
before it in the same plan. Outbound text must not
contain an email address or a phone number (a high-severity violation that escalates the
run); note that a bare 10-digit number reads as a phone number.

Outbox `idempotency_key` = `{event_id}:{index}`, with `index` the action's position in the
approved list.

**The agent's own actions are memory.** For every outbox item, the executor also appends
an `agent_action` event to the event log, in the same unit of work:
`event_id = act:{idempotency_key}`, `direction = outbound`, `occurred_at = now`,
`body` = the text (SMS, Slack, TMS note), the email body, or the Hero task title and
description, and `data = {"kind", "payload", "run_id", "outbox_id"}`. Channel by kind:
`sms` → `sms`, `email` → `email`, `broker_slack` → `slack`, `hero_task` and
`internal_slack` → `hero`, `tms_note` and `stage_update` → `system`. These events are never
queued (`domain.events.RUN_TRIGGERS`). The context loader reads them back: they are part of
the recent events the model sees, and `recent_outbound` (for rate limits) is built from the
`sms`, `email` and `slack` ones inside the window. Outbox `payload` keys, used by the dispatcher and by the eval graders:

| Kind | Payload |
|---|---|
| `sms` | `contact_id`, `text`, `purpose` |
| `email` | `to_contact_ids` (list), `subject`, `body`, `purpose` |
| `broker_slack` | `broker_id`, `text` |
| `tms_note` | `text` |
| `hero_task` | `title`, `description`, `urgent` |
| `internal_slack` | `text` |
| `stage_update` | `stage` |

## Event ids

| Event | `event_id` |
|---|---|
| Message | the integration layer's `message_id` |
| Risk change | `risk:{ping_id}` |
| Timer due | `timer:{timer_id}`; `occurred_at` is the firing time and `data.due_at` the due time |
| Document processed | `doc:{document_id}`; `data = {document_id, type_id, status, attachment_id}` |

Ids for runs, obligations, timers, outbox items and documents come from `IdGenerator`
with the prefixes `run`, `obl`, `timer`, `out`, `doc`.

## Eval timing

A step that injects an event at `at` runs `run_latency_seconds` (default 60) later, so the
first SMS of the main scenario leaves at 07:01 and its timer is due at 07:31. Timers fire
with zero latency: the 07:31 follow-up leaves at 07:31. Step indexes are 0-based; default
message and ping ids are `{scenario_id}:{step_index}`. A timer that comes due inside a
step's latency window fires on the tick after the latency, after the injected event's run.

## Layer and file ownership

The layer rules in `pyproject.toml` (import-linter) are part of the design and are checked
by `make arch` and by a unit test. The LLM stack (LangChain, OpenRouter) is only imported
in `adapters/llm`. `application` may import LangGraph.

# Wave 3: brokers onboarded without code

The contracts below were written first (card W3-C0, branch `wave3`); seven cards build on
them in parallel, each touching only its own files ("Wave 3 file ownership"). Until a card
lands, its seam behaves as before wave 3: the 22 core scenarios pass in scripted mode with
no scenario edits.

## Brokers as data

A broker is a row, not code (`domain/broker.py`, `ports/brokers.BrokerRepository` on the
unit of work):

| Part | What it holds |
|---|---|
| `IntentDef` | `intent_id`, `title`, `description`, `examples`, `priority` (order in the gate prompt, lower first; `other` is 1000), `must_act`, `noop_eligible`, `system` (only from code) |
| `ObligationDef` | `kind`, `follow_up_minutes` (30), `max_attempts` (2), `follow_up_intent` (empty: `<kind>_follow_up_due`), `on_exhausted` (`dispatcher_email`, `hero_task`, `broker_slack`) |
| `DocumentTypeDef` | `type_id`, `fields` (`FieldDef`: `name`, `kind`, `check`) |
| `DocumentRequirement` | `type_id`, `at_stage`, `within_minutes`, `opens_obligation` |
| `BrokerLimits` | `validation` (`ValidationConfig`), `max_steps` (8; 0 = one plan, no action tools), `max_cost_usd` (0.05), `max_rejections` (3) |
| `Rollout` | `percent` of loads on the current version, `previous_version` |

`standard_broker()` is the catalog every broker starts from (`new_broker` copies it) and
the one a load runs on while its broker was never saved: the 8 message intents with the
gate prompt's descriptions word for word, the system intents `delay_risk_high`,
`delay_risk_low`, `eta_follow_up_due` and `document_processed`, the `eta_request`
obligation (30 min, 2 attempts, its follow-up intent `eta_follow_up_due`, exhausted to the
dispatcher email then a Hero task) and the document types `bol`, `pod`,
`rate_confirmation`, `receipt` (`docs/answers.md` 1.2). It has no document requirement.
`Intent` and `ObligationKind` stay as the standard ids; everywhere else an intent or a kind
is a `str` of the catalog.

**Versions and canary.** Every save bumps `config_version` and the repository keeps the
whole broker per version (`broker_versions`, jsonb). `load_run_context` reads the load's
broker, and while `rollout.percent < 100` also `get_version(previous_version)`;
`choose_version(current, previous, load_id)` sends a load to the current version when the
sha256 of its id falls in the first `percent` buckets of 100, so a load keeps its version
across runs. The run records `broker_id` and `broker_config_version`. Publishing an SOP
saves the broker with `sop_version`, `config_version + 1` and `rollout = {percent: 100,
previous_version: <old>}`; a canary publishes with a lower percent; rollback is
`percent: 0`. `Broker.validation_config()` is the broker's `ValidationConfig` with each
kind's attempt cap (`max_attempts`, per kind, `attempt_cap(kind)`).

## Knowledge base

Two ports (`ports/knowledge.py`), because they live in different phases of a run:

- `SopStore`, on the unit of work: uploaded `SopDocument`s, the broker's one `SopDraft`
  (sections under review, `proposed_intents`, `proposed_requirements`,
  `proposed_obligations`) and the published `SopVersion`s (immutable, `version` = content
  hash). `broker_id` None is the standard layer.
- `SopIndex`, in `RuntimeDeps.knowledge`, read while the run thinks (outside any
  transaction): `by_applies` (= `domain.sop.resolve`, today's deterministic selection),
  `visible`, `search` (pgvector cosine plus `ts_rank`, fused by reciprocal rank, in
  Postgres; word overlap in memory), `read`, `version`. Another broker's sections are
  never returned.

`MemorySopIndex.from_files(sops/)` reads the tree on every call, exactly as the file
repository did: an edited file applies to the next run, a broken file fails only the runs
of the brokers that see it, and `version` is the tree's hash. `agent sop import` (card
W3-KNW) turns `sops/` into published versions (standard, northline, keystone).

Embeddings: `openai/text-embedding-3-small`, 1536 dimensions (`config/models.yaml`
`embedding`), only in live and record mode. Scripted, replay and CI use `FakeEmbedder`:
tools never run offline, so no real query is embedded and there is no embedding cassette.
The `vector` extension lives in `public` (scratch schemas set `search_path` to their own
schema): columns are `public.vector(1536)` and the operator `OPERATOR(public.<=>)`.

Onboarding: upload (`POST /api/brokers/{id}/sops`) → `PageReader` → `compile` role
(structured output: sections with title, description, verbatim body, proposed `applies`,
proposed intents, requirements and obligations) → the editor reviews the draft → publish
(embeddings, hash, lint: every section's `applies` valid, every intent with a section) →
the broker's new config version.

## Agent loop

Target graph (card W3-AGT): `prefilter → triage → knowledge → agent → validate`. Until it
lands the graph is the pre-wave-3 one, with decide on the `agent` role and the
`AgentOutcome` schema.

- **triage** is the gate on the broker's catalog: system intents from code, message
  intents and `must_act` from the catalog; `Broker.triage = False` skips the model.
- **knowledge** gives the sections of `by_applies` and the catalog. Today it still
  escalates a gap (`SOP_GAP_TITLE`, reason `sop_gap:`); with AGT the gap moves to the
  agent: `finish(no_procedure=true)` with nothing accepted escalates the same way.
- **agent** is one `generate` call on the `agent` role with tools, `max_tool_rounds =
  max_steps` and `max_cost_usd`; the adapter's loop runs the rounds (so `llm_calls` stays
  1 per agent node) and checks the running cost after each round. Read tools:
  `search_sop`, `read_sop_section`, `get_history`, `get_ledger`, `get_load`,
  `read_document`, `list_document_requirements`. One action tool per `Action` type: it
  builds the action, runs `domain.validation.check_action` against the actions staged so
  far and answers accepted or refused by a rule. A tool never executes anything.
- **The staged plan is a pure fold over the transcript** (`stage_from_transcript`): the
  node recomputes accepted and refused actions from the transcript's tool calls in every
  mode, because scripted and replay models never run tools. The live staging only answers
  the model. The trajectory (`RunRecord.trajectory`, `TrajectoryStep`) is the transcript;
  each assistant entry carries its round's `usage`.
- `AgentOutcome(Plan)` keeps `actions`, so a plan given in the answer (every core scenario's
  `script.decide`) is validated as before.
- **validate** runs the rules over the staged actions plus the answer's actions: the merge
  point, and defense in depth (a violation on a staged action is logged and recorded).
- Over `max_rejections` refused calls: escalate with `blocked_actions`, the accepted
  actions still run.

Model roles: `gate`, `agent` (was `decide`), `judge`, `driver_sim`, `extract` (documents,
images included), `compile` (SOP drafts, scenario drafts). `LlmRequest` carries `images`
and `max_cost_usd`.

## Obligations as data

An obligation's kind is a catalog `ObligationDef.kind`. An SMS that asks for something
names it in `requests` (`purpose: eta_request` is the older spelling of `requests:
eta_request`, and a contradiction is refused): the executor opens that obligation with
the first one and counts every later one, and the rules project it the same way.
`open_obligation` opens one with no attempt (a document the load owes). The attempt cap
is per kind (`ValidationConfig.max_attempts`, filled from the catalog). With AGT,
`update_stage` into a stage with a `DocumentRequirement` opens its `opens_obligation`
(attempts 0), with a timer after `within_minutes` when set; its follow-up timer produces
the kind's follow-up intent; when the attempts are used up, `on_exhausted` says who gets
the case.

## Documents

`ports/documents.py`: `BlobStore` (`put` returns `sha256:<hex>`, the same bytes the same
reference; the filesystem under `BLOBS_DIR` now, an object store later), `PageReader`
(pypdfium2: each page's text, and the page as a PNG when its text is too thin),
`DocumentRepository` (on the unit of work), `DocumentProcessor` (`process_pending`, owns
its units of work).

- `ingest_message` saves one `Document.pending` per attachment of a new message, in the
  message's unit of work.
- `tick()` = `fire_due_timers → documents.process_pending → process_all →
  dispatch_outbox`, so a document is read before the queue runs and its event queues
  behind the message.
- The pipeline (card W3-DOC): blob → pages → one `extract` call that classifies and
  extracts with the broker's `DocumentTypeDef` schemas (a page with little text goes as
  an image) → checks in code per `FieldDef.check` → the document saved `processed` (every
  check ok), `needs_review` (a check failed, low confidence or a missing field) or
  `failed` → one `document_processed` event, `doc:{document_id}`, appended and queued once.
  A clean `processed` document of a requirement's `type_id` closes its obligation as
  `satisfied` in that unit of work; `needs_review` leaves it open.
- Checks against the load: `po_number` (the load's PO; on load 481207 it is in the instructions, `PO 55821`), `mc_number`
  (the carrier's), `stop_name` (matches any of the load's stop names, the shipper or the
  consignee), `date_in_window` (for a fixed appointment: on the appointment's day; for a
  window appointment: inside the window), `seal_number` (the seal the driver reported),
  `signature` (present). A POD's `receiver` is the receiving facility (consignee), not the
  person who signed; its `po_numbers` are checked, so a POD with the wrong PO goes to
  review.
- **Only the `document_processed` run acts on a document.** The message's own run never
  does (the prompt and `sops/standard/documents.md` say so), so an attachment is never
  handled twice.
- Ingress: `POST /api/loads/{id}/attachments` (multipart) and `attachments[].ref`.

## Evals for non-determinism

Card W3-EVK. The spec gains `expect.intent`, `tools_used`, `sections_read`,
`max_rejections` and `judge {rubric, min_score}`; `script.agent` (`script.decide` is its
alias, merged in front) may carry `tool_calls` (`ScriptedReply`), so the transcript fold
is tested in scripted mode; `script.judge`, `script.extract`; `MessageStep.attachments`
(`{filename, fixture, content_type}`: fixture bytes go into the trial's `MemoryBlobStore`).
Graders read `RunRecord.trajectory`. The judge (role `judge`) is called by the runner on
the uncounted inner model: judge calls never count in `llm_calls`, and in scripted mode
without `script.judge` it is skipped with a warning. `--broker-file` runs a suite on an
exported broker (its catalog plus published sections); `agent eval draft` drafts scenarios
from a broker's sections (role `compile`); `eval lint` errors on an intent without a
scenario. New suites: `agent` (an outside email refused at the call, the rejection budget,
a gap searched then handed to a Hero) and `docs` (clean POD, POD with exceptions, overdue
POD). The negative control adds an agent that stages `send_email` to an outside address.

Replay cassettes miss until the wave 3 re-record (card W3-BAKE): the role is `agent` now
and the schemas changed. On `wave3`, CI runs the replay step with `continue-on-error`.

## Wave 3 file ownership

Paths are under `src/freight_agent/` unless they start with `tests/`, `evals/`, `sops/`,
`docs/`, `config/`, `migrations/` or `web/`. A card writes only its files (plus new test
files under its own names). A change it needs elsewhere goes in its report as "needs from
<card>". Every stub C0 created names its owning card in its docstring.

| Card | Owns | Must not touch |
|---|---|---|
| **W3-BRK** | `domain/broker.py` (catalog validation, methods), `ports/brokers.py`, `adapters/persistence/memory/brokers.py`, `adapters/persistence/postgres/brokers.py`, `migrations/0003_brokers.sql`, `api/routes/brokers.py`; `tests/unit/domain/test_broker.py`, `tests/contract/test_brokers.py`, `tests/integration/test_api_brokers.py`, `tests/property/test_broker_properties.py` | runtime, context, executor, prompts, `api/schemas.py`, the stores' `store.py`, `web/` |
| **W3-KNW** | `ports/knowledge.py`, `domain/sop.py`, `adapters/knowledge/{memory,postgres,embeddings,fake_embedder,pdf_pages}.py`, `adapters/sop/{files,lint,importer}.py`, `adapters/persistence/{memory,postgres}/sops.py`, `migrations/0004_knowledge.sql`, `application/sop_ingestion.py`, `application/prompts_knowledge.py`, `cli_sop.py`, `api/routes/knowledge.py`, `evals/fixtures/sops/acme_sop.{pdf,md}` (later edits); `tests/unit/adapters/test_knowledge_*.py`, `tests/unit/adapters/test_sop_files.py`, `tests/unit/adapters/test_sop_lint.py`, `tests/unit/application/test_sop_ingestion.py`, `tests/unit/domain/test_sop.py`, `tests/contract/test_sops.py`, `tests/contract/test_sop_index.py`, `tests/integration/test_api_knowledge.py`, `tests/integration/test_api_sops.py`, `tests/unit/test_sop_seed.py`, `tests/property/test_sop_properties.py` | agent, prompts, runtime, `adapters/llm/openrouter.py`, `adapters/documents/pdfium.py`, the bodies of `sops/*.md`, `bootstrap.py` (expose `knowledge_index(settings) -> SopIndex` in `adapters/knowledge/postgres.py`, under that name) |
| **W3-AGT** | `application/agent.py`, `application/agent_tools.py`, deletes `application/tools.py` and `application/decide.py`, `application/{gate,prompts,runtime,context,executor,escalation}.py`, `application/graph/{build,nodes,state}.py`, `domain/{rules,validation,actions,intents,obligations,runs}.py`, `adapters/llm/{openrouter,cassette,scripted}.py`; `tests/unit/application/*` (but `test_documents.py` and `test_sop_ingestion.py`), `tests/unit/domain/test_{rules,validation,actions,obligations}.py`, `tests/unit/adapters/test_{openrouter,cassette,scripted_model}.py` | `evalkit/`, `api/`, `web/`, persistence, knowledge adapters, the document pipeline, `sops/` text, `config/` |
| **W3-DOC** | `ports/documents.py`, `domain/documents.py`, `application/documents.py` (`DocumentPipeline`, the constructor `bootstrap.build_documents` calls), `application/prompts_documents.py`, `adapters/documents/{fs_blobs,memory_blobs,pdfium,fake_pages}.py` (`PdfiumPageReader` in `pdfium.py`, under that name), `adapters/persistence/{memory,postgres}/documents.py`, `migrations/0005_documents.sql`, `api/routes/documents.py`, `api/simulation.py` (the upload path only), the body of `sops/standard/documents.md`, `evals/fixtures/docs/*` (later edits); `tests/unit/application/test_documents.py`, `tests/unit/adapters/test_pdfium.py`, `tests/unit/adapters/test_blobs.py`, `tests/contract/test_documents.py`, `tests/integration/test_api_documents.py` | agent, prompts, executor, knowledge, `application/runtime.py` (reached through `DocumentProcessor` and `ingest_message`), `evalkit/`, `web/` |
| **W3-EVK** | `evalkit/*` (with `judge.py`, `draft.py`), `cli.py`, `api/evals.py`, `api/routes/evals.py`, `api/routes/broker_evals.py`, `adapters/observability/langsmith.py`, `evals/scenarios/agent/`, `evals/scenarios/docs/`, `evals/brokers/`; `tests/evalkit/*`, `tests/integration/test_api_evals.py`, `tests/unit/adapters/test_langsmith_tracer.py` | application, domain, persistence, `web/`, `config/`, `evals/scenarios/core/` |
| **W3-WEB** | everything under `web/` | any Python |
| **W3-OPS** | `docs/{answers.md,api.md,architecture.html,build_diagrams.js}`, `README.md`, `Makefile`, `.github/workflows/ci.yml`, `config/*.yaml` (after C0), `bootstrap.py`, `settings.py`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `evals/RESULTS.md`, `migrations/0006_*` if needed; `tests/unit/test_bootstrap.py`, `tests/unit/test_settings.py`, `tests/unit/test_cli_platform.py`, `tests/integration/test_postgres_scenarios.py`, `tests/integration/test_postgres_migrations.py` | application, domain, `evalkit/` internals, `web/` |

Frozen after C0 (no card edits them; changes go through the orchestrator): `api/schemas.py`
(the wave 3 bodies are there), `api/routes/__init__.py`, `api/app.py`, `api/views.py`,
`ports/repositories.py`, `ports/llm.py`, `adapters/persistence/memory/store.py` and
`adapters/persistence/postgres/store.py` (they wire every repository once),
`adapters/llm/models_config.py`, `domain/events.py`, `pyproject.toml` and `uv.lock`,
`tests/contract/{conftest,uow_factories,test_unit_of_work}.py`, `tests/conftest.py`,
`evals/scenarios/core/` (zero edits: the 22 scenarios must pass as written),
`docs/decisions.md`, and the applied migrations `0001` and `0002`.

**Migration numbers are reserved:** `0003` brokers (W3-BRK), `0004` knowledge (W3-KNW),
`0005` documents (W3-DOC), `0006` W3-OPS if needed. One file per card, never shared: the
migrator refuses an applied file that was edited.

**Adapters found by name.** The composition root loads a card's adapter by module and
attribute (`bootstrap.LazyAdapter`), so the card plugs in without editing it:
`adapters.knowledge.postgres.knowledge_index(settings)` (W3-KNW, for
`PERSISTENCE=postgres`; until then the runs read `sops/`), and
`application.documents.DocumentPipeline(uow_factory=, clock=, ids=, llm=, blobs=, pages=,
timeout_s=)` with `adapters.documents.pdfium.PdfiumPageReader()` (W3-DOC; until both exist
the runtime runs `NullDocumentProcessor`). The Postgres repository stubs raise
`NotImplementedError("card BRK|KNW|DOC")`, except the broker reads, which find no broker
(the standard catalog applies); so on Postgres a message with an attachment cannot be
ingested until W3-DOC lands.
