<!-- generated-by: gsd-doc-writer -->
# Architecture

The system in diagrams, from the outside in. Each diagram says what is built and what is
only designed. The answers to the assignment's questions are in [`answers.md`](answers.md)
(its first part explains the words used here), and the exact contracts in
[`decisions.md`](decisions.md). Python paths leave out `src/freight_agent/`.

1. [System context](#1-system-context): who sends events, who receives actions, which
   services the agent calls.
2. [Code layers](#2-code-layers): which part of the code may import which.
3. [One run](#3-one-run-claim-think-commit-deliver): the three steps of a run, and the outbox.
4. [The run graph](#4-the-run-graph): the LangGraph graph, its limits and its exits.
5. [Memory](#5-memory): what is stored per load, and the facility memory across loads.
6. [Load 481207, 07:00 to 08:01](#6-load-481207-0700-to-0801): the assignment's situation,
   run by run.
7. [Documents](#7-documents): an attachment from upload to the agent's action.
8. [SOP layers and selection](#8-sop-layers-and-selection): how a run gets its procedure.
9. [Evals and prompt versions](#9-evals-and-prompt-versions): scenarios, datasets,
   experiments, and prompts in the Hub.
10. [Durable execution and production at 10x](#10-durable-execution-and-production-at-10x):
    the implemented Temporal loop and the remaining production work.
11. [Code map](#11-code-map): the main abstractions and directory responsibilities.

## 1. System context

```mermaid
flowchart LR
  subgraph inbound["Inbound"]
    driverIn["Driver: SMS, photos"]
    dispatcherIn["Carrier dispatcher: email"]
    brokerIn["Broker staff: email, Slack"]
    tracking["Tracking feed: location pings with delay risk"]
  end

  ingress["Ingress: messages, pings, attachments"]

  subgraph service["Agent service (this repository)"]
    runtime["Runtime: a queue per load, run graph, executor, outbox"]
    memory[("Load memory: events, ledger, timers, documents, runs")]
    sops[("SOP files, broker profiles and pinned catalogs")]
  end

  gateways["Gateways: record what they would send"]

  subgraph outbound["Outbound"]
    driverOut["Driver: SMS"]
    dispatcherOut["Dispatcher: email in the main thread"]
    brokerOut["Broker: Slack channel"]
    hero["Hero team: tasks, internal Slack"]
    tms["TMS: notes, stage"]
  end

  openrouter["OpenRouter: gate, agent, extract, memory and judge models"]
  langsmith["LangSmith: traces, Prompt Hub, datasets, experiments"]

  driverIn --> ingress
  dispatcherIn --> ingress
  brokerIn --> ingress
  tracking --> ingress
  ingress --> runtime
  runtime <--> memory
  sops --> runtime
  runtime --> gateways
  gateways --> driverOut
  gateways --> dispatcherOut
  gateways --> brokerOut
  gateways --> hero
  gateways --> tms
  runtime <--> openrouter
  runtime -. "traces; prompts at start" .-> langsmith
```

Built: the runtime; load and facility memory in memory or Postgres (`PERSISTENCE`);
OpenRouter and LangSmith adapters; and the React broker workspace. Live operations use
one Temporal workflow per load, a worker and a notification relay, with a real clock and
their own Postgres database. The workspace seeds Northline, Keystone, Aurora and Acme,
persists editable broker SOP profiles, pins a snapshot for each execution, and lets an
operator send contact messages, inject location pings and stop an execution. The CLI eval
player, Studio and legacy simulation API retain their virtual-clock execution path.
External SMS, email, Slack, Hero and TMS providers are still recording gateways; live
operation means real model calls and durable scheduling, not delivery to real contacts.

## 2. Code layers

```mermaid
flowchart TB
  entry["Entry points: api/, cli.py, studio.py, evalkit/, orchestration/"]
  boot["bootstrap.py and orchestration/: composition roots"]
  app["application/: prefilter, gate, agent and tools, run graph, executor, runtime, documents, facility memory"]
  interfaces["interfaces/: storage, SOP index, models, prompts, gateways, clock, tracer"]
  domain["domain/: load, events, obligations, timers, rules, SOP merge, catalog, documents"]
  adapters["adapters/: memory and Postgres stores, OpenRouter, SOP files, Prompt Hub, PDF reader, LangSmith, gateways"]

  entry --> boot
  boot --> app
  boot --> adapters
  app --> interfaces
  app --> domain
  interfaces --> domain
  adapters --> interfaces
  adapters --> domain
```

These are the core dependency directions. import-linter checks them (`make arch`, five
contracts in `pyproject.toml`, also run as a unit test):

- `domain` imports nothing outside itself: no LangChain, LangGraph, FastAPI or httpx.
- `interfaces` import only the domain.
- `application` imports neither the adapters nor the API. It may import LangGraph, not
  LangChain.
- `adapters` import neither the application nor the API; `evalkit` does not import the API.
- `bootstrap.py` wires the core runtime; `orchestration/worker.py` composes its real-clock
  Postgres dependencies with Temporal. The workspace also uses the Postgres profile adapter.

LangChain integrations are kept outside the domain and application. pyright runs in strict mode on `domain`, `interfaces` and
`application`.

## 3. One run: claim, think, commit, deliver

```mermaid
sequenceDiagram
  autonumber
  participant Q as Event queue (one per load)
  participant R as Runtime
  participant S as Store
  participant G as Run graph
  participant M as Models (OpenRouter)
  participant O as Outbox sender
  participant W as Gateways

  R->>S: transaction 1: claim the oldest event of a load with nothing in flight
  S-->>R: event and context (load, ledger, timers, last 30 events, documents, catalog)
  R->>G: think, outside any transaction, within 240 s
  G->>M: gate call (messages only), then agent rounds
  M-->>G: intent, tool calls, finish
  G-->>R: status and approved actions
  R->>S: transaction 2: verify claim token; apply state, outbox, run record, event done
  O->>S: read pending outbox items
  O->>W: send with idempotency key event_id:index
  W-->>O: accepted_at receipt, or error (up to 5 attempts)
  O->>S: mark sent; arm durable follow-up from accepted_at
```

No transaction stays open while a model thinks. Applicable approved actions, the run record
and completion commit atomically; domain-invariant failures are recorded as skipped
actions. If a worker dies after the claim, the event stays in flight until the claim lease
(the deadline plus 60 s) returns it to the queue, counted as a failed attempt. After three
failed attempts it becomes a dead letter.

Claims carry a fencing token: an expired Activity attempt cannot commit state or release
its replacement's claim. In durable mode (`delivery_followups=True`), an ETA request's
follow-up timer is armed after the gateway accepts the outbox item, not when the model
proposes it. Activity retries can replay sends: stable idempotency keys and a deduplicating
gateway are required to prevent duplicate external effects. The recording gateway
deduplicates within its process; no persistent production provider adapter has been
validated yet.

## 4. The run graph

```mermaid
flowchart TD
  start(["event claimed, context read"]) --> prefilter
  prefilter["prefilter: code checks"]
  prefilter -- "duplicate, stale timer, empty message" --> skipped(["skipped: no model call"])
  prefilter -- "sender is not a load contact" --> unknown(["escalated: Hero task, no model call"])
  prefilter --> gate
  gate["gate: intent from code for system events; for a message, the decision model's probabilities, then a rule in code"]
  gate -- "a quiet intent and P(act) under 0.2" --> noop1(["noop: one model call"])
  gate --> knowledge
  knowledge["knowledge: SOP sections for the intent and stage, procedures in progress, list of the rest"]
  knowledge --> agent
  agent["agent: one model round"]
  agent -- "tool calls" --> tools
  agent -- "text instead of a tool call: asked to finish" --> agent
  agent -- "finish" --> validate
  tools["tools: read tools answer; action tools propose, checked by the rules at once"]
  tools -- "answers" --> agent
  tools -- "finish in the same round, or too many refused calls" --> validate
  validate["validate: the rules over every proposed action"]
  validate -- "approved actions" --> acted(["acted: the commit applies them"])
  validate -- "nothing approved" --> noop2(["noop"])
  validate -- "serious violation, or too many refused calls" --> blocked(["escalated: blocked task; approved actions still run"])
  validate -- "no procedure and nothing approved" --> gap(["escalated: SOP gap task"])
```

| Node | Code | Model call | Can end the run as |
|---|---|---|---|
| prefilter | `application/prefilter.py` | none | `skipped` (duplicate, stale timer, empty message); `escalated` (unknown sender) |
| gate | `application/gate_decision.py` (`application/gate.py` with `GATE_ENGINE=llm`) | messages only: one call to TypeSafe's Jev 1.14 (GPT-4.1 Nano with `llm`); system events get their intent from code | `noop`: an intent that may end a run, such as an acknowledgement, with P(needs the agent now) under 0.2 |
| knowledge | `application/graph/nodes.py` | none | never |
| agent, tools | `application/agent.py`, `application/agent_tools.py` | one call per round, gpt-oss-120b | goes on to validate |
| validate | `domain/validation.py`, `domain/rules.py` | none | `acted`, `noop`, `escalated` |

| Limit | Default | Where | When it is reached |
|---|---|---|---|
| `max_steps` | 8 rounds | `BrokerLimits`, per client | The next round must call `finish` |
| `max_cost_usd` | $0.05 per run | `BrokerLimits` | The next round must call `finish` |
| `max_rejections` | 3 refused action calls | `BrokerLimits` | The run stops and escalates; accepted actions still run |
| Model timeout | 60 s per round, its retries included | `RuntimeConfig.llm_timeout_s` | The call moves to the next model of the role |
| Rate limit or overloaded provider | 2 retries on the same model, about 1 s then 2 s apart | `RetryPolicy`, `adapters/llm/resilient.py` | The call moves to the next model of the role |
| Run deadline | 240 s | `RuntimeConfig.run_deadline_s` | The run escalates with one urgent Hero task; the event is done |
| Model failure | after the retries and the fallbacks | `adapters/llm/resilient.py` | The run escalates with one urgent Hero task; the event is done |

The graph is `application/graph/build.py`. The platform draws the same graph from
`GET /api/graph`, and LangGraph Studio shows it inside its scenario graph.

## 5. Memory

```mermaid
erDiagram
  LOAD ||--o{ EVENT : "event log, append-only"
  LOAD ||--o{ OBLIGATION : "ledger"
  OBLIGATION ||--o{ TIMER : "follow-up timers"
  LOAD ||--o{ DOCUMENT : "attachments read"
  EVENT ||--o| RUN_RECORD : "one run per event"
  RUN_RECORD ||--o{ OUTBOX_ITEM : "approved actions"
  OUTBOX_ITEM ||--|| EVENT : "logged back as agent_action"
  TIMER ||--o| EVENT : "fires as timer_due"
  DOCUMENT ||--o| EVENT : "announced as document_processed"

  EVENT {
    string event_id "message id, risk:ping, timer:id, doc:id, act:key"
    string type "inbound_message, risk_changed, timer_due, document_processed, agent_action"
    datetime occurred_at
  }
  OBLIGATION {
    string kind "eta_request, pod, or a client kind"
    string status "open, satisfied, exhausted, cancelled"
    int attempts
    datetime deferred_until
    string active_timer_id
  }
  TIMER {
    datetime due_at
    string status "pending, fired, superseded, cancelled"
  }
  OUTBOX_ITEM {
    string idempotency_key "event_id:index"
    string kind "sms, email, broker_slack, tms_note, hero_task, internal_slack, stage_update"
    string status "pending, sent, failed"
  }
  RUN_RECORD {
    string status "skipped, noop, acted, escalated, failed"
    string intent
    string trajectory "tool calls, accepted or refused"
    string prompts "template version per role"
  }
```

Every run reads one snapshot of this memory (`application/context.py`) and keeps nothing in
the process between runs. The agent's own actions come back as `agent_action` events, so the
next run reads what was approved, to whom and when. The outbox status and `accepted_at`
separately establish whether a gateway accepted the action.

The load also carries its immutable `sop_snapshot`, the profile revision, and stop-requested
and stopped timestamps. Editable profiles live in `broker_profiles`; they are not the
per-run conversation memory. A saved profile changes subsequent loads, not existing snapshots.

`Load.current_stop()` resolves the operational stop from an explicit sequence when set,
otherwise from the stage: pickup for `dispatched`, `to_pickup`, `at_pickup`; delivery for
`to_delivery`, `at_delivery`, `delivered`. `application/prompts.py` includes this fact in
the load context, beside the triggering event and in `get_load`. It distinguishes a
future arrival, an already reached facility and a completed delivery. This grounding
applies to local and Hub templates and to older pinned SOPs; it is prompt guidance,
not a deterministic validator of every facility name in generated prose.

`PERSISTENCE` chooses where the memory lives, behind the interfaces of
`interfaces/repositories.py`: in the process (`adapters/persistence/memory/`; the default,
and in isolated eval/Studio plays), or in Postgres (`adapters/persistence/postgres/`;
the Docker Compose services and database tests), one table per entity above, plus `risk_trackers` and
`event_queue` (`migrations/`). Both stores pass the same contract tests. Transactions and
locks are in [decisions.md, Storage](decisions.md#storage).

### Facility memory, across loads

The memory above ends with its load. Facility notes outlive it: what a driver says about a
pickup or delivery place is kept for the next load of the same client there.

```mermaid
flowchart LR
  subgraph run["A run"]
    K["knowledge node: notes of the current and next stop"] --> P["agent prompt: Facility notes (untrusted)"]
    T["recall_facility tool"]
  end
  subgraph store["LangGraph store: Postgres with pgvector, or in memory"]
    N[("notes: per client and facility, text embedded")]
    J[("jobs: one per delivered load")]
  end
  C["commit of the run that sets delivered"] -->|queue a job| J
  X["Background extractor: relay maintenance or legacy tick"] -->|pending jobs| J
  X -->|"one memory model call: add, update, delete, each citing a message"| G["checks in code (domain/memory.py)"]
  G -->|"operations that pass, in one client's notes"| N
  K -->|read by client and facility| N
  T -->|search by meaning within the client| N
```

- A note is one short text with a kind (`check_in`, `hours`, `requirement`, `access`,
  `contact`, `other`), its facility (the stop's normalized name and address), its client,
  and its source: the load, the message and when it was said.
- Each note's text is embedded (turned into numbers that stand for its meaning) with
  `openai/text-embedding-3-small` on OpenRouter, or with an offline hash when there is no
  key and in the tests. The store is any LangGraph store (`adapters/memory/store.py`): on
  Postgres, LangGraph's Postgres store in the load memory's database, with a pgvector index
  (`adapters/memory/postgres.py`); otherwise an in-memory store with the same index.
- A job is queued after the delivering run's commit. If the process crashes in between, the
  platform or durable relay queues the job again when it starts on Postgres
  (`AgentRuntime.queue_missed_extractions`).

## 6. Load 481207, 07:00 to 08:01

```mermaid
sequenceDiagram
  participant T as Tracking feed
  participant R as Runtime
  participant L as Ledger and timers
  participant G as Gate (decision model)
  participant A as Agent model
  participant D as Driver
  participant P as Dispatcher
  participant H as Hero team

  T->>R: 07:00 ping, delay risk high
  Note over R: code confirms low to high, logs risk_changed (no model)
  R->>A: 07:01 run 1: intent delay_risk_high from code, Northline's delay_risk_high section
  A-->>R: send_sms(driver, eta_request), schedule_timer(eta_request, 30), finish
  R->>L: open eta_request with 1 attempt, timer T1 due 07:31
  R->>D: SMS asking for the ETA
  D->>R: 07:12 "What's the PO number?"
  R->>G: 07:13 run 2: read the message
  G-->>R: question (1.00), needs the agent now (0.97), so on to the agent
  R->>A: driver_questions in full, delay_risk_high as procedure in progress, ledger: 1 attempt, T1 at 07:31
  A-->>R: send_sms(driver, reply, "PO 55821"), finish
  Note over L: unchanged: a reply requests nothing, so 1 attempt and T1 stay
  R->>D: SMS with PO 55821
  L->>R: 07:31 T1 fires as timer_due (still the active timer, so not stale)
  R->>A: run 3: intent eta_follow_up_due from code, ledger: 1 attempt
  A-->>R: send_sms(driver, eta_request), schedule_timer(eta_request, 30), finish
  R->>L: 2 attempts, T2 due 08:01
  R->>D: second SMS asking for the ETA
  L->>R: 08:01 T2 fires
  R->>A: run 4: ledger: 2 attempts of 2
  A-->>R: send_email(dispatcher), create_hero_task(call the driver), close_obligation(eta_request, exhausted), finish
  R->>P: email in the main thread
  R->>H: Hero task
```

This diagram shows `evals/scenarios/core/a_client_a_high_risk.yaml` on the virtual-clock
player: an injected event runs 60 simulated seconds after it, and timers fire without
simulated delay. In the live workspace, the initial ping is sent immediately, model
latency is real, and the 30-minute wait begins when the gateway accepts the SMS. A durable
timer makes the work eligible at the deadline; worker availability, queue order and model
latency determine when the resulting action is actually accepted.

## 7. Documents

```mermaid
flowchart LR
  upload["message with an attachment, or POST /api/loads/ID/attachments"] --> blob[("blob store: named by sha256")]
  upload --> pending["document saved as pending, with the message"]
  pending --> tick["Temporal document Activity, or legacy tick"]
  tick --> pages["pypdfium2: text per page; a page with little text becomes an image"]
  pages --> extract["one extract call (Gemini 2.5 Flash): type and field values"]
  extract --> checks["code checks against the load: PO, MC, stops, dates, seal, signature"]
  checks --> status{"status"}
  status -- "every check passed" --> processed["processed: closes the required document's obligation"]
  status -- "failed check, missing field, low confidence, exceptions" --> review["needs_review"]
  status -- "file or model error" --> failed["failed"]
  processed --> event["one document_processed event, doc:ID"]
  review --> event
  failed --> event
  event --> run["its run: the agent reads the document and follows sops/standard/documents.md"]
```

The message's own run never acts on the attachment; only the `document_processed` run does,
so a document is handled once. Code: `application/documents.py`, `domain/documents.py`,
`adapters/documents/pdfium.py`.

## 8. SOP layers and selection

```mermaid
flowchart TB
  subgraph tree["sops/ (Markdown, one section per file)"]
    standard["standard/: 11 sections every client reads"]
    client["clients/ID/: replace or extend a standard section by id"]
    loadtype["clients/ID/types/TYPE/: replace or extend for one load type"]
    catalog["clients/ID/broker.yaml: the client's intents, obligations, documents, limits"]
  end

  standard --> merge
  client --> merge
  loadtype --> merge
  merge["merge by section id: standard, then client, then load type"] --> visible["the sections this client and load type can see"]

  event["the event's intent and the load's stage"] --> select
  visible --> select["select: section intents include the intent, or are empty; same for stages"]
  select --> prompt["agent prompt: matching sections in full, the procedure of each open obligation, one line per other section"]
  visible --> tools["agent tools: search_sop (word search), read_sop_section"]
  catalog --> rendered["catalog: in the gate and agent prompts, and the rules' limits"]
  rendered --> prompt
```

A client folder holds only what the client changes. Another client's sections are never
visible. File-backed evals record a hash of the SOP folder. The live workspace seeds a
`BrokerProfile` from those files, saves revisions in Postgres with compare-and-swap, and
freezes catalog and layered sections into the load at start. Parameters such as attempt
count, follow-up minutes and escalation channels are rendered into the snapshot before
its hash is computed. Both graph and document Activities read that pinned catalog.
The editor permits client and load-type replacements/extensions; the inherited standard
layer is read-only. Review/approval and canary rollout are not implemented.
CLI evals read the file-backed SOPs; they do not automatically evaluate saved UI profiles.
Changing a seed file also does not overwrite a profile already saved in Postgres.

## 9. Evals and prompt versions

```mermaid
flowchart LR
  subgraph git["Git"]
    scenarios["evals/scenarios/SUITE/*.yaml"]
    copy["prompts/ROLE.yaml: reviewable copy"]
    pins["config/prompts.yaml: each ROLE pinned to freight-agent-ROLE:prod"]
  end

  subgraph ls["LangSmith"]
    dataset["dataset freight-agent-SUITE: one example per scenario"]
    hub["Prompt Hub: one prompt per role (gate, agent, extract, judge, gate_decision, memory); commits, tags prod and candidate"]
    experiment["experiment: rows, evaluator scores, pass^k, metadata"]
  end

  scenarios -- "agent eval run syncs: an example is rewritten when its scenario changes" --> dataset
  copy -- "agent prompts push --tag candidate" --> hub
  hub -- "agent prompts pull" --> copy
  pins --> resolve["read once at process start; recorded per run"]
  hub --> resolve
  dataset --> run["k repetitions, each a fresh runtime on a virtual clock, live models"]
  resolve --> run
  run --> experiment
```

An experiment's metadata names the git commit, the model of each role, the prompt version of
each role and the SOP hash. To promote a prompt: edit it, `push --tag candidate`,
`agent eval run --prompt-tag candidate`, compare in LangSmith, `push --tag prod`, restart.

The `grounding` suite adds seven scenarios: all six stages and a pickup-to-delivery
transition whose history still mentions the pickup. Deterministic action matchers require
the relevant facility and forbid the other one; step rubrics judge whether the text
correctly describes an upcoming, current or completed stop. Negative controls in
`tests/evalkit/test_stop_grounding.py` reject wrong, missing and ambiguous stop references.
`tests/temporal/` separately verifies orchestration with a real Temporal test server and
scripted model responses. The live UI uses scenario inputs but does not run the automated
eval graders or advance virtual time.

## 10. Durable execution and production at 10x

The following loop is implemented in `orchestration/`, with a standalone local stack in
`compose.temporal.yml`. Postgres holds business state and FIFO order; Temporal holds
workflow progress and durable waits. Model calls, SQL and sends run in Activities, outside
the deterministic Workflow code.

```mermaid
flowchart LR
  ui["Broker workspace"] --> ingress["Durable ingress: commit, then notify"]
  ingress --> pg[("Postgres: loads, profiles, events, ledger, timers, outbox")]
  ingress --> workflow["LoadWorkflow: load/broker/load-id"]
  pg --> relay["Relay: repair missed notifications; memory maintenance"]
  relay --> workflow
  workflow --> activities["Activities: snapshot, graph, documents, delivery, timer, stop"]
  activities <--> pg
  activities --> graph["Existing LangGraph runtime"]
  graph --> models["OpenRouter"]
  activities --> gateways["Recording gateways"]
  workflow --> wait["Temporal durable wait: deadline or wake signal"]
  wait --> workflow
```

`LoadWorkflow` handles pending delivery, due timers, documents and queued graph events in
that order, then waits until a signal or the next deadline. It rereads state before firing,
so an ETA can cancel a pending follow-up. Signals coalesce; the persistent queue preserves
events. The relay repairs the commit-to-notification gap after a Temporal outage. Workflows
continue as new after 100 turns or when Temporal recommends it, rereading absolute
deadlines from Postgres.

Stop is cooperative: persist the request, let the current Activity settle, then cancel
pending timers, open obligations and unsent outbox items, preserve history and complete
the workflow. It cannot retract an already accepted send. New inputs to a stopping or
stopped load are rejected; continuing requires a new execution.

The local Temporal service is `start-dev` with a persistent SQLite volume, not a production
cluster. Production still needs a supported Temporal deployment, provider integrations,
operational alerts, capacity testing/autoscaling, object storage and rollout controls.
Queue age and model-provider limits must be measured; the 10x sizing in
[`answers.md`, 5.2](answers.md#52-volume-x10-what-must-change) is a capacity estimate, not a
load-test result. Hybrid SOP search and operational metrics existed in an earlier build
at the git tag `full-platform` but are not part of this runtime.

## 11. Code map

| Area / abstraction | Responsibility |
|---|---|
| `domain/` — `Load`, `Obligation`, `Timer`, `Broker`, SOP merge and action rules | Business facts and invariant checks independent of orchestration and model providers |
| `interfaces/` — `UnitOfWork`, `SopIndex`, model, prompt and gateway contracts | Boundaries the application uses without selecting infrastructure |
| `application/runtime.py` — `AgentRuntime`, `RuntimeDeps` | Ingress, fenced claim/think/commit, outbox delivery and background work |
| `application/graph/`, `agent.py`, `agent_tools.py`, `prompts.py` | Bounded tool loop, selected knowledge and grounded context |
| `adapters/` | Postgres/in-memory persistence, OpenRouter, Prompt Hub, SOP files, document readers and recording channels |
| `orchestration/` — `LoadWorkflow`, `LoadActivities`, `TemporalIngress`, `BrokerProfile` | Durable scheduling, real-clock composition, profile editing and execution snapshots |
| `api/`, `web/src/` | Main FastAPI service/proxy and broker workspace UI |
| `evalkit/`, repository `evals/` and `tests/` | Scenario execution/graders, fixtures and deterministic/Temporal verification |

This split keeps the same agent runtime usable in fast virtual-clock evals and durable
live operations while isolating infrastructure and UI concerns.
