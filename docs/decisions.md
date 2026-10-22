<!-- generated-by: gsd-doc-writer -->
# Design decisions and contracts

The rules that the code, the SOP files and the eval scenarios all follow. When one changes,
this file changes in the same commit. The words used here are explained at the start of
[`answers.md`](answers.md#words-used-here). Python paths leave out `src/freight_agent/`.

## Clients and load types

| In the assignment | Client id | Name | Where |
|---|---|---|---|
| Client A | `northline` | Northline Logistics | The fixture `evals/fixtures/load_481207.json` |
| Client B | `keystone` | Keystone Freight | Scenarios set `load_overrides: {broker: {id: keystone, ...}}` |
| Client B, refrigerated | `keystone` with load type `reefer` | | `load_overrides: {load_types: [reefer]}` |
| A third client with its own catalog | `acme` | Acme Freight | The `docs` suite |
| Extreme live test | `aurora` | Aurora Transportes | `evals/fixtures/load_aurora_extreme.json`: ten ETA SMS, one minute apart, then a Hero |

The SOP client id is the load's `broker_id`. A client with no folder runs on the standard
layer and the standard catalog.

## SOP files

```
sops/
  standard/<topic>.md                                layer standard
  clients/<client_id>/<topic>.md                     layer client
  clients/<client_id>/types/<load_type>/<topic>.md   layer load_type
  clients/<client_id>/broker.yaml                    the client's catalog changes
```

Each Markdown file is one section: a YAML header between `---` lines, then a plain-English
body. `README.md` files are for editors and are never read as sections.

```yaml
---
id: delay_risk_high            # required, snake_case, the merge key across layers
title: Delay risk changes to high
description: One line. The agent sees it in the list of sections.
intents: [delay_risk_high, eta_follow_up_due]   # empty or missing: every intent
stages: [to_pickup, to_delivery]                # empty or missing: every stage
mode: replace                  # replace (default) or extend; standard/ is always replace
---
```

- **Merge** per `id`, from least to most specific (standard, client, load type). `replace`
  swaps body and header. `extend` adds its body after the inherited body and keeps the
  inherited title, description, intents and stages. The lint refuses an `extend` with
  nothing to extend, `mode: extend` in `standard/`, and an id used twice in one folder.
- **Applies** to an event when its intents include the event's intent (or are empty) and
  its stages include the load's stage (or are empty).
- **Visibility:** a run sees the standard layer, its own client's layer and that client's
  load-type layers, never another client's.
- **Intents in files:** `standard/` files name only standard intents; a client's files may
  name the intents of its catalog.
- **File-backed version:** a 12-hex-digit hash of the whole folder, recorded with every run
  as `sop_version`. The file-backed runtime reads the folder on each run, so an edited file
  applies to the next run. A file that cannot be read fails the runs of the clients that
  see it, never the others. Live workspace loads use the immutable profile snapshot below.

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
- **`northline`:** the same two SMS; then email the dispatcher in the main thread, then an
  urgent Hero task, and close the request as exhausted.
- **`keystone`:** one SMS; no ETA after 30 minutes, alert the broker's Slack channel; never
  contact the dispatcher.
- **`acme`:** up to three SMS, 20 minutes apart; then email the dispatcher, an urgent Hero
  task, and close the request as exhausted.
- **`aurora`:** ten SMS, one minute apart; wait one final minute after the tenth accepted
  SMS, then create an urgent Hero task and close the request as exhausted.

## Broker profiles and execution snapshots

The workspace (`orchestration/profiles.py`, `orchestration/workspace.py`) seeds four broker
profiles from the SOP files. `PostgresProfiles` stores the current profile and revision in
`broker_profiles`; saving uses a revision compare-and-swap, so a stale browser cannot
overwrite another edit. Profiles cannot edit the inherited standard layer or include a
different broker's client sections. Client and load-type sections support `replace` and
`extend`, subject to catalog, identifier and inheritance validation.

Starting an execution validates the requested profile revision, creates a new load id,
rebases scenario appointments to real time, freezes the catalog and layered sections into
`Load.sop_snapshot`, and sends an initial location ping. Scenario messages are operator
inputs; their scripted wait steps cannot advance the clock. Repeating an accepted start
with the same load id, scenario and original revision keeps its original snapshot.

`freeze_profile` renders obligation placeholders (`max_attempts`, `follow_up_minutes`,
`on_exhausted`) and hashes the rendered data with the profile revision. It reserves SMS
capacity of at least the ETA attempt count plus one reply in the rate window. Thus the
three-SMS default is not a universal limit for live custom profiles. `pinned_index` supplies
the snapshot to graph and document Activities. Subsequent profile edits affect only new
executions. Existing profiles are stored data, not automatically migrated by later changes
to seed files. CLI evals read file-backed SOPs and do not import these saved profiles.
Approval, canaries and version-history rollback are not implemented.

## Client catalog

A client's catalog (`domain/broker.py`) is the standard catalog plus the changes in its
`broker.yaml`:

| Key | What it holds | How a client changes it |
|---|---|---|
| `name` | text | replaces |
| `triage` | true by default; false: no gate model call, every message goes to the agent as `other` | replaces |
| `intents` | id, title, description, examples, `priority` (order in the gate prompt), `must_act` (default true; the gate may not end the run), `noop_eligible` (the gate may end the run), `system` (set only by code) | a standard id replaces that entry; a new id is added |
| `obligations` | kind, title, description, `follow_up_minutes` (30), `max_attempts` (2), the follow-up intent (default `<kind>_follow_up_due`), `on_exhausted` (`dispatcher_email`, `hero_task`, `broker_slack`) | same |
| `document_types` | type id, title, description, fields (name, description, kind, check) | same |
| `documents` | requirements: type, `at_stage`, `within_minutes`, `opens_obligation` | added (the standard has none) |
| `limits` | the rules' limits (SMS rate, deferral policy, attempt caps), `max_steps` (8), `max_cost_usd` (0.05), `max_rejections` (3) | replaces |

The standard catalog: 8 message intents (`eta_provided`, `eta_deferred`, `question`,
`driver_departed`, `document`, `status_update`, `acknowledgement`, `other`; the last three
are not `must_act`), 4 system intents (`delay_risk_high`, `delay_risk_low`,
`eta_follow_up_due`, `document_processed`), the `eta_request` obligation (30 minutes, 2
attempts, then `dispatcher_email` and `hero_task`) and the document types `bol`, `pod`,
`rate_confirmation`, `receipt`.

Each catalog is checked as a whole, and every problem is reported in one error: ids are
unique snake_case; the system intents code emits are present; each obligation follows up
with a system intent (`eta_request` with `eta_follow_up_due`); `document` and `other` are
present, `other` last; a field's check fits its kind; a requirement names a known type and
kind, and `within_minutes` needs `opens_obligation`; limits stay within caps (`max_steps` 1
to 20, `max_cost_usd` up to 1, `max_rejections` up to 20, `max_attempts` 1 to 10, waits up
to a week).

## Intents and the gate

| Event | Intent | Model call |
|---|---|---|
| `risk_changed` to `high` | `delay_risk_high` | none |
| `risk_changed` from `high` to lower | `delay_risk_low` | none |
| `timer_due` | the follow-up intent of its obligation kind (`eta_follow_up_due`, `pod_follow_up_due`); `other` for an unknown kind | none |
| `document_processed` | `document_processed` | none |
| `attachment_received`, or a message with attachments and no text | `document` | none |
| `hero_update` | `other`, no action | none |
| `inbound_message` | from the gate | one call |

`GATE_ENGINE` chooses what reads a message:

- **`jev`** (the default): the decision model (`decisions.gate` in `config/models.yaml`)
  answers two questions in one call: which of the catalog's message intents it is, and the
  probability that it needs the agent now (`application/gate_decision.py`). The run ends as
  `noop` only when the intent is `noop_eligible`, not `must_act`, and that probability is
  under `GATE_ACT_THRESHOLD` (0.2). So an uncertain reading goes on to the agent.
- **`llm`**: the gate's chat model answers intent, act and a reason. The run goes on to the
  agent when it says act, or when the intent is `must_act`; otherwise it ends as `noop`.

On either engine a message can only get a message intent of the catalog. Any other answer
becomes `other`, which goes on to the agent.

Only a confirmed change into or out of `high` becomes an event (`domain/ping_filter.py`): a
rise is confirmed by 1 ping, a drop by 2. `medium` is tracked but does not wake the agent.
The tracker keeps the ids of the last 64 pings, so a ping delivered twice changes nothing.

## Run context

`load_run_context` (`application/context.py`) reads the load's memory once per run:

- **Recent events:** the last 30, in log order, without the run triggers logged after the
  event being processed (they get their own runs). Every `agent_action` is kept, wherever it
  falls. `get_history` uses the same rule over 200 events.
- **Recent outbound:** one record per recipient of each SMS, email and broker Slack post in
  the SMS rate window (a broker Slack post counts against the first broker contact on Slack).
- **Also:** all obligations, the pending timers, the timer a `timer_due` event names, the
  risk tracker, whether the event was already processed, the client's catalog, and the
  load's documents when an attachment or a `document_processed` event is in view.

The agent's read tools answer from this snapshot and the SOP index, so no tool opens a
transaction.

**Stop grounding.** `Load.current_stop()` honors an explicit `current_stop_sequence` when
it matches a stop; otherwise it chooses a pickup for `dispatched`, `to_pickup`, `at_pickup`,
and a delivery for `to_delivery`, `at_delivery`, `delivered` (falling back to the final stop
if that type is absent). `application/prompts.py` renders the operational stop and its
relation to the stage in the load block and `get_load`, and repeats the resolved fact beside
the event. The SOP's generic “next stop” or “destination” means this stop for ETA work,
not the subsequent itinerary stop. At a facility, arrival has already happened; after
delivery there is no pending arrival. This shared context reaches local and pinned Hub
prompts, including executions with older SOP snapshots. It guides model wording; it does
not add a hard validator that rejects every wrong facility reference.

## Prefilter

Code, before any model call, in this order; the first match decides:

| Condition | Result |
|---|---|
| A run already exists for the event | `skipped`, `already_processed` |
| `timer_due` and the timer is stale: superseded, cancelled, its obligation closed or gone, or no longer the active timer | `skipped`, `stale_timer` |
| `inbound_message` with an empty body and no attachments | `skipped`, `empty_message` |
| `inbound_message` whose sender is not a contact of the load | `escalated`, `unknown_sender`: one Hero task, no model call |

## Run graph and budgets

`prefilter -> gate -> knowledge -> agent <-> tools -> validate` (`application/graph/`; the
diagram and the limits table are in [architecture.md section 4](architecture.md#4-the-run-graph)).
The runtime runs the claim and the commit, the run's two transactions, around it.

- **agent and tools:** one model round, then every tool call of the round in order. An
  answer in text instead of a tool call gets one more round that must call `finish`. Bad
  arguments are a refused call with the reason. After `max_steps` rounds, or once the rounds
  cost `max_cost_usd`, the next round must call `finish`; more refused calls than
  `max_rejections` go straight to `validate`.
- **validate** runs the rules over every proposed action, in call order, refused ones
  included, and decides in this order: a high-severity violation, or more refused calls than
  `max_rejections`, escalates with "Agent action blocked by a safety check" (the approved
  actions still run); `no_procedure` with nothing approved escalates with "No SOP for this
  situation"; nothing approved is `noop`; otherwise `acted`.
- **Statuses:** `skipped` (prefilter), `noop`, `acted`, `escalated` (a Hero task made by
  code), `failed` (an unexpected error: the event goes back to the queue; after 3 failed
  attempts it is a dead letter, and only then is the failed run stored).
- **Escalations by code** (`application/escalation.py`, fixed titles the scenarios match):
  unknown sender, SOP gap, model failure after the fallbacks (urgent), run past its
  240-second deadline (urgent), blocked actions (urgent). A model failure or the deadline
  completes the event, so a retry never creates a duplicate.
- **Claim lease:** each pass over the queue first returns to it the events in flight for
  longer than the run deadline plus 60 s, each counted as a failed attempt. A slow run that
  finishes after its event was taken back cannot commit or release the replacement claim:
  `owns_claim(event_id, claim_token)` is checked in the transaction. An event already
  committed is skipped as `already_processed` on retry.

The rules (`domain/rules.py`), high severity first: `recipient_is_contact` (high),
`no_contact_details_in_text` (high: an email address or a phone number in outgoing text; a
bare 10-digit number counts as a phone number), `channel_allowed`, `sms_rate_limit` (3 per
contact per 60 minutes), `known_obligation_kind`, `attempt_cap` (per kind, from the catalog;
counts SMS and email that request it), `obligation_exists`, `deferral_policy` (one deferral
of at most 15 minutes per obligation), `stage_moves_forward`, `no_duplicate_action`. A rule
judges an action against the state that the approved actions before it will leave, and never
edits it.

## Executor semantics

The executor (`application/executor.py`) applies the approved actions in order, inside the
commit transaction. "Now" is the clock's time when the run commits.

| Action | Effect |
|---|---|
| `send_sms` / `send_email` with `requests: <kind>` (`purpose: eta_request` means `requests: eta_request`) | Opens the obligation of that kind with 1 attempt, or adds one attempt to the open one. Outbox `sms` / `email` |
| other `send_sms`, `send_email` | Outbox only |
| `post_broker_slack`, `write_tms_note`, `create_hero_task`, `post_internal_slack` | Outbox `broker_slack`, `tms_note`, `hero_task`, `internal_slack` |
| `update_stage` | Saves the stage; outbox `stage_update`. A document requirement at that stage opens its obligation (0 attempts) with a timer after `within_minutes`, unless one is open or a clean document of that type is in hand |
| `schedule_timer` | Needs the open obligation. Due at the later of now + minutes and `deferred_until` + 2 minutes, in real minutes; the obligation's pending timers become `superseded`; the new one is its active timer |
| `defer_obligation` | Deferred until the event time + minutes; an active timer is replaced by one due no earlier than that plus 2 minutes |
| `close_obligation` | Closes with `satisfied`, `exhausted` or `cancelled`; pending timers become `cancelled` |
| `open_obligation` | Opens one with 0 attempts (a document the load owes); refused when one is open |

An action that would break a domain invariant is skipped and reported in the run record; the
rest of the plan runs. When every approved action was skipped, the run is `noop`.

**Durable follow-ups.** The table describes the base executor. With
`delivery_followups=True` (required by `LoadActivities`), an ETA request carries its
follow-up configuration on the outbox item. Delivery records `accepted_at`, then arms
the timer from that receipt plus the configured minutes, respecting a deferral. An explicit
`schedule_timer` in the same plan does not start the wait ahead of that send. Document
requirements and timers unrelated to an outgoing request retain their normal semantics.

**Outbox.** The idempotency key is `{event_id}:{index}`, where index is the action's place
in the approved list. Delivery checks the payload first (a malformed one fails at once). A
gateway error keeps the item pending, with its attempts and last error, until 5 attempts
make it `failed`; the eval batch continues with other items. In Temporal, delivery is
serialized per load and a failed item makes `load_snapshot` fail non-retryably, requiring
operator intervention. Recording gateways ignore repeated keys within their process. A real provider adapter
must persistently deduplicate or reconcile an ambiguous send; Temporal retry alone does
not guarantee exactly-once external delivery.

| Kind | Payload |
|---|---|
| `sms` | `contact_id`, `text`, `purpose` |
| `email` | `to_contact_ids` (list), `subject`, `body`, `purpose` |
| `broker_slack` | `broker_id`, `text` |
| `tms_note` | `text` |
| `hero_task` | `title`, `description`, `urgent` |
| `internal_slack` | `text` |
| `stage_update` | `stage` |

**Actions are memory.** For every outbox item the executor adds an `agent_action` event in
the same transaction: id `act:{idempotency_key}`, outbound, at now, with the text (or the
Hero task's title and description) and `data = {kind, payload, run_id, outbox_id}`. Channel
by kind: `sms` and `email` keep theirs, `broker_slack` is `slack`, `hero_task` and
`internal_slack` are `hero`, `tms_note` and `stage_update` are `system`. These events never
start a run.

## Documents

- A new message saves one pending document per attachment, in the message's transaction.
- Legacy `tick()` runs, in order: fire due timers, read pending documents, run the queue, deliver
  the outbox, then the facility memory extraction. So a document is read before the queue
  runs, and its event queues behind the message. Live operations use
  `process_load_documents` before the queued graph event and use the load's pinned catalog.
- The pipeline (`application/documents.py`): blob, pages (a page with little text becomes an
  image), one `extract` call answering `{type_id, confidence, fields, notes}`, checks in
  code, then a status: `processed`, `needs_review` (confidence under 0.7, unknown type,
  missing or unreadable value, failed check, POD exceptions) or `failed` (a file, reader or
  model error; never retried). One `document_processed` event per document. A clean
  `processed` document of a required type closes its obligation as `satisfied` in the same
  transaction.
- Checks: `po_number` (the load's PO; on load 481207 it is in the instructions),
  `mc_number` (the carrier's), `stop_name` (a stop, the shipper or the consignee),
  `date_in_window` (the appointment's day, or inside its window), `seal_number` (the last
  seal a message reported), `signature` (present).

## Facility memory

- **A note** (`domain/memory.py`) is a text of at most 200 characters with a kind
  (`check_in`, `hours`, `requirement`, `access`, `contact`, `other`), its client, its
  facility, and its source (load, event, time).
- **The facility key** is `<slug of the name>~<slug of the address>`, for example
  `riverbend-plant~stockton-ca`. The same place on two loads gets the same key; a renamed
  stop is a new facility.
- **Namespaces** in the LangGraph store: `("facility_memory", <client>, <facility_key>)` for
  the notes, indexed on their text; `("facility_memory_jobs", <client>)` for the extraction
  jobs, one per load. Every method of `interfaces/memory.py` names the client: a write
  naming another client is refused whole, and a read drops anything of another client.
- **Read:** the knowledge node reads up to 5 notes of the current stop and 5 of the next,
  most recently updated first, and the run record keeps them. If the memory fails, the run
  goes on with no notes. In the prompt each note is one line with its stop, facility, kind,
  source load and date, and its text as a JSON string. `recall_facility {query, stop}`
  searches by meaning, in one stop's facility or across the client's facilities.
- **Write:** a run whose commit moves the load to `delivered` queues a job after the commit;
  if that fails, the run still stands. On start on a durable store, the durable relay queues
  every delivered load that has no job (why: [answers.md 3.1](answers.md#31-how-does-the-agent-know-its-past-actions-and-the-past-messages-of-the-load)),
  and `agent memory extract --load` runs one by hand. The relay processes pending jobs; eval ticks run up to 5
  pending jobs. One `memory` call per job reads the load's inbound messages and Hero
  updates, each with its event id and the stops it may be about, plus the current notes of
  those facilities, and answers a list of add, update and delete operations.
- **Which stop a message is about:** the stop of the stage in effect when it arrived, plus
  any stop it names. A load with no stage update may be at any of its stops.
- **Checks on every operation,** in order: the facility is a stop of the load; the cited
  event is an inbound message or Hero update of the load, with text, about that stop; an
  update or delete names an existing note of that facility; the text is not empty, at most
  200 characters, and has no email address, phone number, money (rates, prices, pay, fees,
  "$") or words aimed at the agent ("ignore your rules", "system prompt"); an add is not a
  note the facility already has and fits its cap of 12 notes; at most 10 operations per job.
  A refused operation is dropped, and the job keeps it with its reason.
- **Failures:** a model failure leaves the job pending; after 3 tries it is `failed`. A load
  with no inbound text is done with no model call.
- **Store:** `PERSISTENCE` chooses it, as for the load memory. On `postgres`: LangGraph's
  Postgres store on `DATABASE_URL`, on a small connection pool of its own, with a pgvector
  index (1536 dimensions, HNSW, cosine) on the note text. `agent db migrate` creates its
  tables, so the app never creates tables at start. The vector width is fixed by that table:
  on Postgres the offline hash is 1536 wide too, and an embedder of another width stops the
  start. On `memory`: an in-memory store with the same index. Evals and Studio always get a
  fresh in-memory one per play.
- **Lifecycle:** eval and Studio plays use a fresh facility memory. The live worker and relay share durable Postgres memory; starting
  a live scenario does not reset other loads. Memory-suite fixtures receive an isolated
  facility address per live execution, preventing their seeded notes from contaminating
  other test loads.

## Event ids

| Event | `event_id` |
|---|---|
| Message | the integration layer's `message_id` (the dedupe key) |
| Risk change | `risk:{ping_id}` |
| Timer due | `timer:{timer_id}`; `occurred_at` is the firing time, `data.due_at` the due time |
| Document processed | `doc:{document_id}`; `data = {document_id, type_id, status, attachment_id}` |
| Agent action | `act:{event_id}:{index}` |

Ids of runs, obligations, timers, outbox items and documents have the prefixes `run`, `obl`,
`timer`, `out`, `doc`.

## Eval timing

The scenario player (`evalkit/runner.py`) is used by experiments, CLI plays and Studio. The broker workspace uses Temporal and wall-clock time instead:

- The virtual clock starts at the scenario's `date` and `start`, in its `timezone` (default
  `America/Los_Angeles`).
- Each step first moves to its `at`, stopping at every pending timer due on the way and
  ticking there. Timers fire with no delay, so the 07:31 follow-up leaves at 07:31.
- A message or ping is taken in at `at`. The clock then moves `run_latency_seconds` (default
  60, never past the next step) and ticks once, so the first SMS of scenario A leaves at
  07:01 and its timer is due at 07:31. A timer due inside that window fires on the same
  tick, after the injected event's run.
- Steps are numbered from 0; default message and ping ids are `{scenario_id}:{step_index}`.
- Checks read the outbox items created during the step, and the ledger and timers at its
  end. `llm_calls` counts the gate, one per agent run whatever its rounds, and one per
  document read; the judge is not counted.

The `grounding` suite covers the six load stages plus departure from pickup into the
delivery leg. It checks the facility in SMS and Slack text, rejects the other stop and
uses judge rubrics for stage-appropriate meaning. Its negative-control tests
(`tests/evalkit/test_stop_grounding.py`) prove that the original wrong-stop Slack alert,
a missing stop and a response naming both stops fail. Live workspace scenario controls
reuse inputs, not these automated graders.

## Temporal execution

- **Identity:** `LoadKey.workflow_id` is `load/<broker_id>/<load_id>`. The load id remains
  globally unique in Postgres. Inbound provider ids are scoped by workflow and channel;
  location ping ids are scoped by workflow.
- **Ingress:** commit business data, then signal-with-start using `wake`. A notification
  failure does not undo the database commit. `TemporalIngress.relay` repeatedly discovers
  loads with pending work and repairs the database-to-Temporal gap; startup also finds
  existing timers.
- **Workflow:** deterministic `LoadWorkflow` reads a snapshot, checks stop, then services
  outbox, due timer, pending document, or queued event, in that order. Existing messages
  remain ahead of a newly fired timer in the FIFO. It waits on `wake` or the next absolute
  timer deadline, and rereads state before acting. After 100 turns, or Temporal's history
  recommendation, it continues as new.
- **Activities:** `run_load_graph` wraps the existing claim/think/commit runtime. Its
  timeout is 280 seconds, surrounding the 240-second thinking deadline and inside the
  300-second claim lease. Snapshot, timer and stop Activities have 30-second timeouts;
  delivery has 60 seconds; documents have 280 seconds. Transient Activity errors retry
  with backoff. Dead-letter events and exhausted delivery require operator intervention.
- **Stop:** save the request and wake the workflow. The current Activity settles before
  `stop_load_execution` cancels pending timers, open obligations and pending outbox items,
  saves `automation_stopped_at` and completes the workflow. History and accepted sends
  remain. Inputs after a stop request are refused; the relay does not restart a stopped
  load. A new execution is required to continue.
- **Completion:** a delivered load completes when no queued event, pending send or open
  obligation remains. Other idle loads wait for inputs. The relay separately processes
  facility extraction jobs and repairs missed jobs at startup.
- **Guarantee boundary:** durable deadlines survive worker restarts; they are not exact
  wall-clock delivery promises during outages, backlog or model latency. A real external
  provider still needs an idempotent delivery adapter.

## Prompts and their versions

- **Six templates,** one per model role: `gate`, `agent`, `extract`, `judge`,
  `gate_decision` (the decision gate's texts and the instructions of its two questions) and
  `memory` (the facility memory extraction). Each is a system and a user message
  (`interfaces/prompts.py`), stored in the Prompt Hub as `freight-agent-<role>`, with a
  reviewable copy in `prompts/<role>.yaml`.
- **Pins:** `config/prompts.yaml` pins each role to `<name>:<tag>` (`prod` is what runs) or
  `<name>:<commit>`. `PROMPTS_TAG` (or `agent eval run --prompt-tag`) runs every role at
  another tag.
- **Start:** the runtime resolves every pin once, at process start; a pin the Hub cannot
  serve stops the start. Without `LANGSMITH_API_KEY`, or with `PROMPTS_SOURCE=local`, the
  runtime reads `prompts/`, with a warning.
- **Versions:** a version is the Hub commit's first 12 hex digits, or `local:<sha256
  prefix>` for the repository copy. Every run records the version of each prompt it sent;
  the run's trace carries the gate engine and the gate and agent prompt versions; an
  experiment's metadata carries every role's version and the gate engine.
- **Variables:** code computes them (`application/prompts.py`): the system message from the
  client only (its intents, kinds, limits), the user message from the run. Each service
  checks at start that its template uses only variables it computes, so a bad Hub edit stops
  the process instead of rendering an empty block. Message text is rendered as a JSON
  string, so untrusted text cannot open a heading.
- **Commands:** `agent prompts status` compares the copy with each pin; `push --tag T`
  commits each changed template and moves T; `pull` writes a Hub version back. Both refuse a
  template the code cannot render.

## Storage

- **Choice.** `PERSISTENCE=memory` (default) or `postgres` with `DATABASE_URL`, read only by
  the composition roots. Temporal requires Postgres and a real clock, configured in
  `compose.temporal.yml`. The main API is a stateless broker-workspace proxy to ingress.
  Evals and Studio build a fresh in-memory store per play, whatever the setting.
- **Schema.** `migrations/NNNN_name.sql`, applied in order by `agent db migrate`, each in its
  own transaction and recorded in `schema_migrations`. An applied file that changed is
  refused: add a new file instead. `0001` holds the tables; `0002` enables pgvector;
  `0003` adds claim fencing and delivery receipts; `0004` adds broker profiles. Then the
  command sets up LangGraph's Postgres store for facility memory. Ids are text
  in the `C` collation, and ties sort by natural id as in the memory store. Foreign keys are
  checked at commit, the event log refuses UPDATE and DELETE, and a load's metadata and a
  run record are JSONB.
- **Transactions and locks.** One transaction per unit of work. Reading a load locks its row,
  and reading an obligation locks the obligation's. The queue claim and the due timers use
  `FOR UPDATE SKIP LOCKED` (each worker skips rows another holds), so several workers could
  share them. A partial unique index allows one event in flight per load. The claim time is
  the runtime's clock, for the claim lease.
- **Lifecycle.** The live workspace appends independent loads in the durable
  database and never resets it on scenario start. Temporal resumes waits and the relay
  repairs notifications and extraction jobs. Attachment files go to `BLOBS_DIR` on
  Postgres, in memory otherwise.
- **Tests.** `tests/contract/` runs every suite on both stores, the facility memory's
  included (Postgres when `TEST_DATABASE_URL` or `DATABASE_URL` is set, marked
  `integration`, each test in a schema of its own); `make integration` runs them with the
  Postgres-only tests.
