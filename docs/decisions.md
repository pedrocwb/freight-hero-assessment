# Design decisions and contracts

The rules every part of the code, the SOP files and the eval scenarios agree on. When one
changes, this file changes in the same commit. Python paths leave out
`src/freight_agent/`.

## Clients and load types

| In the assignment | Client id (`broker.id`) | Name | Where |
|---|---|---|---|
| Client A | `northline` | Northline Logistics | The fixture `evals/fixtures/load_481207.json` |
| Client B | `keystone` | Keystone Freight | Scenarios set `load_overrides: {broker: {id: keystone, ...}}` |
| Client B, refrigerated | `keystone` with load type `reefer` | | `load_overrides: {load_types: [reefer]}` |
| A third client with its own catalog | `acme` | Acme Freight | The `docs` suite |

The SOP client id is the load's `broker_id`. A client with no folder runs on the standard
layer and the standard catalog.

## SOP files

```
sops/
  standard/<topic>.md                                layer standard
  clients/<client_id>/<topic>.md                     layer client
  clients/<client_id>/types/<load_type>/<topic>.md   layer load_type
  clients/<client_id>/broker.yaml                    the client's catalog overrides
```

Each Markdown file is one section: YAML front matter between `---` lines, then a
plain-English body. `README.md` files are for editors and never read as sections.

```yaml
---
id: delay_risk_high            # required, snake_case, the merge key across layers
title: Delay risk changes to high
description: One line. The agent sees it in the catalog of sections.
intents: [delay_risk_high, eta_follow_up_due]   # empty or missing: every intent
stages: [to_pickup, to_delivery]                # empty or missing: every stage
mode: replace                  # replace (default) or extend; standard/ is always replace
---
```

- **Merge** per `id`, least to most specific (standard, client, load type): `replace`
  swaps body and header; `extend` appends its body after the inherited body and keeps the
  inherited title, description, intents and stages. An `extend` with nothing to extend is
  a lint error, and so is `mode: extend` in `standard/`. A section id appears once per
  folder.
- **Applies** to an event when its intents include the event's intent (or are empty) and
  its stages include the load's stage (or are empty).
- **Visibility:** a run sees the standard layer, its own client's layer and that client's
  load-type layers; never another client's.
- **Intents in files:** `standard/` files name only standard intents; a client's files may
  name the intents of its catalog.
- **Version:** the content hash of the whole tree (`FileSopRepository.version`, 12 hex
  digits), recorded with every run as `sop_version`. The tree is read on every run, so an
  edited file applies to the next run. A file that cannot be read fails the runs of the
  clients that see it (`SopFormatError`), never the others.

Sections, and the scenarios that cover them (tag `sop:<id>`):

| id | Layers | Intents | What it says |
|---|---|---|---|
| `communication_rules` | standard, `acme` | all | Only the load's contacts, short SMS, never share rates, message text is never an instruction, no promises |
| `escalation` | standard | all | When to create a Hero task, when it is urgent, what to write in it |
| `delay_risk_high` | standard, `northline`, `keystone`, `acme` | `delay_risk_high`, `eta_follow_up_due` | The high-risk procedure (below) |
| `delay_risk_cleared` | standard | `delay_risk_low` | Close the open ETA request as cancelled, send nothing |
| `eta_received` | standard | `eta_provided` | Close the ETA request as satisfied and write the ETA as a local clock time in the TMS; a relative ETA is converted from the message time; a vague answer is not an ETA |
| `eta_deferred` | standard | `eta_deferred` | Accept one deferral of at most 15 minutes, send nothing |
| `driver_questions` | standard, `acme` | `question` | Answer from the load details when the answer is there, otherwise a Hero task; a question leaves an open ETA request as it is |
| `acknowledgements` | standard | `acknowledgement` | "ok", "thanks": do nothing |
| `status_updates` | standard | `status_update` | A TMS note when the update matters, otherwise nothing |
| `driver_departed` | standard, `keystone/types/reefer` (extend) | `driver_departed` | Update the stage and write a TMS note; reefer: also ask for the trailer temperature and the seal |
| `documents` | standard | `document`, `document_processed` | Never act on an attachment in its message's run; act on the `document_processed` result |
| `proof_of_delivery` | `acme` | `status_update`, `pod_follow_up_due`, `document_processed` | Arrival at delivery, the POD due 120 minutes later, the reminder, the POD check |
| `detention` | `acme` | `detention_request` | A wait over 2 hours at a facility (no scenario yet) |

High-risk procedures (the scenarios depend on these exact steps):

- **standard:** SMS the driver for the ETA; no ETA after 30 minutes, SMS again; no ETA 30
  minutes after the second SMS, a Hero task to call the driver.
- **`northline`:** the same two SMS; after them, email the dispatcher in the main thread,
  then an urgent Hero task, and close the request as exhausted.
- **`keystone`:** one SMS; no ETA after 30 minutes, alert the broker's Slack channel; never
  contact the dispatcher.
- **`acme`:** up to three SMS, 20 minutes apart; then email the dispatcher, an urgent
  Hero task, and close the request as exhausted.

## Client catalog

A client's catalog (`domain/broker.py`, `Broker`) is the standard catalog
(`standard_broker()`) with the overrides of its `broker.yaml` (`CatalogOverrides`,
applied by `client_broker`):

| Key | Shape | Override rule |
|---|---|---|
| `name` | text | replaces |
| `triage` | bool, default true; false: no gate model call, every message goes to the agent as `other` | replaces |
| `intents` | `IntentDef`: `intent_id`, `title`, `description`, `examples`, `priority` (gate prompt order, lower first; `other` is 1000), `must_act` (default true), `noop_eligible`, `system` | an id of the standard catalog replaces that entry; a new id is appended |
| `obligations` | `ObligationDef`: `kind`, `title`, `description`, `follow_up_minutes` (30), `max_attempts` (2), `follow_up_intent` (empty: `<kind>_follow_up_due`), `on_exhausted` (`dispatcher_email`, `hero_task`, `broker_slack`) | same |
| `document_types` | `DocumentTypeDef`: `type_id`, `title`, `description`, `fields` (`name`, `description`, `kind`, `check`) | same |
| `documents` | `DocumentRequirement`: `type_id`, `at_stage`, `within_minutes`, `opens_obligation` | appended (the standard has none) |
| `limits` | `BrokerLimits`: `validation` (SMS rate, deferral policy, attempt caps), `max_steps` (8), `max_cost_usd` (0.05), `max_rejections` (3) | replaces |

The standard catalog: 8 message intents (`eta_provided`, `eta_deferred`, `question`,
`driver_departed`, `document`, `status_update`, `acknowledgement`, `other`; the last three
are not `must_act`), 4 system intents (`delay_risk_high`, `delay_risk_low`,
`eta_follow_up_due`, `document_processed`), the `eta_request` obligation (30 minutes, 2
attempts, then `dispatcher_email` and `hero_task`) and the document types `bol`, `pod`,
`rate_confirmation`, `receipt`.

Every `Broker` is validated as a whole (`catalog_problems`, every problem in one error):
snake_case unique ids; the system intents code emits are present; each obligation's
follow-up intent is a system intent of the catalog; `eta_request` follows up as
`eta_follow_up_due`; `document` and `other` are present, `other` last; a field's check
fits its kind; a requirement names a known type and kind, and `within_minutes` needs
`opens_obligation`; limits within caps (`max_steps` 1 to 20, `max_cost_usd` up to 1,
`max_rejections` up to 20, `max_attempts` 1 to 10, waits up to a week).

## Intents and the gate

| Event | Intent | Model call |
|---|---|---|
| `risk_changed` to `high` | `delay_risk_high` | none |
| `risk_changed` from `high` to lower | `delay_risk_low` | none |
| `timer_due` | the follow-up intent of its obligation kind (`eta_follow_up_due`, `pod_follow_up_due`); `other` for an unknown kind | none |
| `document_processed` | `document_processed` | none |
| `attachment_received`, or a message with attachments and no text | `document` | none |
| `hero_update` | `other`, no action | none |
| `inbound_message` | from the gate model | `gate` role, one forced structured call |

The run goes on to the agent when the gate says `act`, or when the intent is `must_act` in
the client's catalog; otherwise it ends as `noop` after one model call. A message can only
have a message intent of the catalog: any other answer becomes `other`, which acts.

Only a confirmed transition into or out of `high` becomes an event (`domain/ping_filter.py`:
an upgrade is confirmed by 1 ping, a downgrade by 2). `medium` is tracked but does not wake
the agent. The tracker keeps the ids of the last 64 pings, stored with it, so a redelivered
ping changes nothing.

## Run context

`load_run_context` (`application/context.py`) reads the load's memory once per run:

- `recent_events`: the last 30 events in log order, without the run triggers logged after
  the event being processed (they get their own runs); every `agent_action` is kept,
  wherever it falls. `history`: the same rule over 200 events, for `get_history`.
- `recent_outbound`: one record per recipient of each SMS, email and broker Slack post in
  the SMS rate window (a broker Slack post counts against the first broker contact on
  Slack); the page grows until it reaches an action older than the window.
- `obligations` (all, oldest first), `pending_timers` (earliest first), `event_timer`
  (the timer a `timer_due` event names), the risk tracker, `already_processed`, the
  client's catalog, and the load's documents when an attachment or a `document_processed`
  event is in view.

The agent's read tools answer from this snapshot and the SOP index, so no tool opens a
transaction.

## Prefilter

Code, before any model call, in this order; the first match decides:

| Condition | Result |
|---|---|
| A run already exists for the event | `skipped`, `already_processed` |
| `timer_due` and the timer is stale: superseded, cancelled, its obligation closed or gone, or no longer the active timer | `skipped`, `stale_timer` |
| `inbound_message` with an empty body and no attachments | `skipped`, `empty_message` |
| `inbound_message` whose sender is not a contact of the load | `escalated`, `unknown_sender`: one Hero task, no model call |

## Run graph and budgets

`prefilter -> gate -> knowledge -> agent <-> tools -> validate` (`application/graph/`),
around which the runtime runs the claim and the commit, the run's two transactions.

- **agent** is one model round (`LanguageModel.chat`, role `agent`). It goes to `tools`
  when the round called tools, to `validate` when it called `finish`, and back to itself
  when the model replied in prose (the next round is told to answer and must call
  `finish`).
- **tools** answers every call of the round, in order. A read tool returns text; an action
  tool builds the action (`build_action`) and checks it with `check_action` against the
  actions accepted before it; bad arguments are a refused call with the reason. It goes to
  `validate` when the round also answered or the refused calls exceed `max_rejections`.
- **Budgets** (`BrokerLimits`): after `max_steps` rounds, or once the agent's rounds cost
  `max_cost_usd`, the next round is forced to call `finish`.
- **validate** runs the rules over every proposed action, in call order, refused ones
  included, and decides the verdict, in this order: a high-severity violation, or more
  refused calls than `max_rejections`, escalates with "Agent action blocked by a safety
  check" (reason `blocked: <rules>`), and the approved actions still run; `no_procedure`
  with nothing approved escalates with "No SOP for this situation" (`sop_gap:
  <intent>/<stage>`); nothing approved is `noop`; otherwise `acted`.
- **Statuses:** `skipped` (prefilter), `noop`, `acted`, `escalated` (a Hero task made by
  code), `failed` (an unexpected error: the event goes back to the queue; after 3 failed
  attempts it is a dead letter, and only then is the failed run stored).
- **Escalations by code** (`application/escalation.py`, stable titles the scenarios match):
  unknown sender, SOP gap, model failure after the fallbacks (urgent), run past its
  240-second deadline (urgent), blocked actions (urgent). A model failure or the deadline
  completes the event, so it is never retried into a duplicate.
- **Claim lease:** every `process_all` first returns the events in flight for longer than
  the run deadline plus 60 s to the queue, each counted as a failed attempt. A slow run
  that commits after its event was reclaimed changes nothing in the queue, and the event's
  next run is skipped as `already_processed`.

The rules (`domain/rules.py`), high severity first: `recipient_is_contact` (high),
`no_contact_details_in_text` (high: an email address or a phone number in outbound text;
a bare 10-digit number reads as a phone number), `channel_allowed`, `sms_rate_limit` (3
per contact per 60 minutes), `known_obligation_kind`, `attempt_cap` (per kind, from the
catalog; counts SMS and email `requests`), `obligation_exists`, `deferral_policy` (one
deferral of at most 15 minutes per obligation), `stage_moves_forward`,
`no_duplicate_action`. A rule judges an action against the state the approved actions
before it will leave, and never edits it.

## Executor semantics

The executor (`application/executor.py`) applies the approved actions in order, inside
the commit transaction. `now` is the clock's time when the run commits.

| Action | Effect |
|---|---|
| `send_sms` / `send_email` with `requests: <kind>` (`purpose: eta_request` means `requests: eta_request`) | Opens the open obligation of that kind with 1 attempt, or records one more attempt. Outbox `sms` / `email` |
| other `send_sms`, `send_email` | Outbox only |
| `post_broker_slack`, `write_tms_note`, `create_hero_task`, `post_internal_slack` | Outbox `broker_slack`, `tms_note`, `hero_task`, `internal_slack` |
| `update_stage` | Saves the load with the stage; outbox `stage_update`. A `DocumentRequirement` at that stage opens its obligation (0 attempts) with a timer after `within_minutes`, unless one is open or a clean document of that type is in hand |
| `schedule_timer` | Needs the open obligation. `due = max(now + minutes, deferred_until + 2 min grace)` in real minutes; pending timers of the obligation become `superseded`; the new one is the obligation's `active_timer_id` |
| `defer_obligation` | `until = event time + minutes`; an active timer is replaced by one due no earlier than `until` plus grace |
| `close_obligation` | Closes with `satisfied`, `exhausted` or `cancelled`; pending timers become `cancelled` |
| `open_obligation` | Opens one with 0 attempts (a document the load owes); refused when one is open |

An action that would break a domain invariant is skipped and reported
(`RunRecord.skip_reason`); the rest of the plan runs. When every approved action was
skipped the run is `noop`.

**Outbox.** `idempotency_key = {event_id}:{index}`, `index` the action's position in the
approved list. Delivery (`AgentRuntime.dispatch_outbox`) checks the payload first (a
malformed one fails at once); a gateway error keeps the item pending with `attempts` and
`last_error` until 5 attempts make it `failed`; the rest of the batch is still sent.
Gateways ignore a repeated key.

| Kind | Payload |
|---|---|
| `sms` | `contact_id`, `text`, `purpose` |
| `email` | `to_contact_ids` (list), `subject`, `body`, `purpose` |
| `broker_slack` | `broker_id`, `text` |
| `tms_note` | `text` |
| `hero_task` | `title`, `description`, `urgent` |
| `internal_slack` | `text` |
| `stage_update` | `stage` |

**Actions are memory.** For every outbox item the executor appends an `agent_action`
event in the same transaction: `event_id = act:{idempotency_key}`, `direction =
outbound`, `occurred_at = now`, `body` the text (or the Hero task's title and
description), `data = {kind, payload, run_id, outbox_id}`. Channel by kind: `sms` to
`sms`, `email` to `email`, `broker_slack` to `slack`, `hero_task` and `internal_slack` to
`hero`, `tms_note` and `stage_update` to `system`. These events never start a run.

## Documents

- `ingest_message` saves one pending `Document` per attachment of a new message, in the
  message's transaction.
- `tick()` is `fire_due_timers -> documents.process_pending -> process_all ->
  dispatch_outbox`, so a document is read before the queue runs and its event queues
  behind the message.
- The pipeline (`application/documents.py`): blob, pages (a page with little text becomes
  an image), one `extract` call answering `{type_id, confidence, fields, notes}`, checks
  in code, then status `processed`, `needs_review` (confidence under 0.7, unknown type,
  missing or unreadable value, failed check, non-empty POD `exceptions`) or `failed` (a
  file, reader or model error; never retried). One `document_processed` event per
  document. A clean `processed` document of a requirement's type closes its obligation as
  `satisfied` in the same transaction.
- Checks: `po_number` (the load's PO; on load 481207 it is in the instructions),
  `mc_number` (the carrier's), `stop_name` (a stop, the shipper or the consignee),
  `date_in_window` (the appointment's day, or inside its window), `seal_number` (the last
  seal a message reported), `signature` (present).

## Event ids

| Event | `event_id` |
|---|---|
| Message | the integration layer's `message_id` (the dedupe key) |
| Risk change | `risk:{ping_id}` |
| Timer due | `timer:{timer_id}`; `occurred_at` is the firing time, `data.due_at` the due time |
| Document processed | `doc:{document_id}`; `data = {document_id, type_id, status, attachment_id}` |
| Agent action | `act:{event_id}:{index}` |

Ids of runs, obligations, timers, outbox items and documents come from `IdGenerator` with
the prefixes `run`, `obl`, `timer`, `out`, `doc`.

## Eval timing

The scenario player (`evalkit/runner.py`), used by the experiments, the platform and
Studio:

- The virtual clock starts at the scenario's `date` and `start` in its `timezone`
  (default `America/Los_Angeles`).
- Each step first advances to its `at`, stopping at every pending timer due on the way
  and ticking there: timers fire with zero latency, so the 07:31 follow-up leaves at
  07:31.
- A message or ping is ingested at `at`; the clock then moves `run_latency_seconds`
  (default 60, never past the next step) and ticks once, so the first SMS of scenario A
  leaves at 07:01 and its timer is due at 07:31. A timer due inside that window fires on
  the same tick, after the injected event's run.
- Step indexes are 0-based; default message and ping ids are `{scenario_id}:{step_index}`.
- Checks read the outbox items created during the step and the ledger and timers at its
  end. `llm_calls` counts the gate, one per agent run whatever its rounds, and one per
  document read; the judge is not counted.

## Prompts and their versions

- Four templates, one per model role: `gate`, `agent`, `extract`, `judge`. Each is a chat
  template of a system and a user message (`ports/prompts.py`), stored in the LangSmith
  Prompt Hub as `freight-agent-<role>` with a reviewable copy in `prompts/<role>.yaml`.
- `config/prompts.yaml` pins each role to a reference: `<name>:<tag>` (`prod` is what runs)
  or `<name>:<commit>`. `PROMPTS_TAG` (or `agent eval run --prompt-tag`) runs every role at
  another tag.
- The runtime resolves every pin once, at process start (`bootstrap.build_prompts`); a pin
  the Hub cannot serve stops the start. Without `LANGSMITH_API_KEY`, or with
  `PROMPTS_SOURCE=local`, the runtime reads `prompts/`, with a warning.
- A version is the Hub commit's first 12 hex digits, or `local:<sha256 prefix>` for the
  repository copy. Every run records the version of each prompt it sent
  (`RunRecord.prompts`); the run's trace carries `prompt_gate` and `prompt_agent`; an
  experiment's metadata carries every role's version and reference.
- Code computes the variables (`application/prompts.py`): the system message from the
  client alone (the catalog's intents, kinds, limits), the user message from the run.
  Each service checks at build time that its template names only variables it computes,
  so a bad Hub edit stops the process instead of rendering an empty block. Message text
  is rendered as a JSON string, so untrusted text cannot open a heading.
- `agent prompts status` compares the copy with each pin; `push --tag T` commits each
  changed template and moves T; `pull` writes a Hub version back. Both refuse a template
  the code cannot render.

## Layers

The import rules in `pyproject.toml` (import-linter, `make arch`, also a unit test):
`domain` imports nothing outside itself (no LangChain, LangGraph, FastAPI or httpx);
`ports` import only the domain; `application` imports neither adapters nor the API (it
may import LangGraph, not LangChain); `adapters` import neither the application nor the
API; `evalkit` does not import the API. `bootstrap.py` is the only place that wires
adapters to ports. pyright runs in strict mode on `domain`, `ports` and `application`.
