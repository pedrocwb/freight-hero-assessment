# Architecture

The system in diagrams, from the outside in. Each diagram says what is built and what is
only designed. The answers to the assignment's questions are in
[`answers.md`](answers.md); the contracts every part follows are in
[`decisions.md`](decisions.md). Python paths below leave out `src/freight_agent/`.

| Diagram | Shows |
|---|---|
| [1. System context](#1-system-context) | Who sends events, who receives actions, which services the agent calls |
| [2. Code layers](#2-code-layers) | Domain, interfaces, application, adapters, and who may import whom |
| [3. One run](#3-one-run-claim-think-commit-deliver) | The three phases of a run and the outbox |
| [4. The run graph](#4-the-run-graph) | The LangGraph graph, its budgets and its exits |
| [5. Memory](#5-memory) | Event log, ledger, timers, outbox, documents, run records |
| [6. Load 481207, 07:00 to 08:01](#6-load-481207-0700-to-0801) | The assignment's situation, run by run |
| [7. Documents](#7-documents) | An attachment from upload to the agent's action |
| [8. SOP layers and selection](#8-sop-layers-and-selection) | How a run gets its procedure |
| [9. Evals and prompt versions](#9-evals-and-prompt-versions) | Scenarios to datasets to experiments; prompts in the Hub |
| [10. Production at 10x (designed)](#10-production-at-10x-designed) | Queues, workers, scheduler, Postgres, object store |

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
    runtime["AgentRuntime: per-load FIFO, run graph, executor, outbox"]
    memory[("Load memory: event log, ledger, timers, documents, run records")]
    sops[("SOP files and client catalogs: sops/")]
  end

  gateways["Gateways: recording fakes in this repository"]

  subgraph outbound["Outbound"]
    driverOut["Driver: SMS"]
    dispatcherOut["Dispatcher: email in the main thread"]
    brokerOut["Broker: Slack channel"]
    hero["Hero team: tasks, internal Slack"]
    tms["TMS: notes, stage"]
  end

  openrouter["OpenRouter: the gate's decision model; agent, extract, judge models"]
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
  runtime -. "traces; templates at start" .-> langsmith
```

Built: the runtime, the load memory in memory or in Postgres (`PERSISTENCE`), the SOP
files, the OpenRouter and LangSmith adapters, the platform (FastAPI and a React UI) that
plays every contact. Not built: real integrations (the gateways record what they would
send), an always-on worker.

## 2. Code layers

```mermaid
flowchart TB
  entry["Entry points: api/ (FastAPI), cli.py (agent ...), studio.py (LangGraph Studio), evalkit/ (experiments)"]
  boot["bootstrap.py: the composition root, the only place that picks adapters"]
  app["application/: run context, prefilter, gate, agent and tools, run graph, executor, runtime, document pipeline"]
  interfaces["interfaces/: repositories, SOP index, language model, decision model, prompts, gateways, blobs, page reader, clock, tracer"]
  domain["domain/: load, events, obligations, timers, rules and validation, SOP merge, catalog, documents, actions"]
  adapters["adapters/: memory and Postgres stores, OpenRouter with retries and fallbacks, OpenRouter Decisions API, SOP files, Prompt Hub, pypdfium2, LangSmith, recording gateways"]

  entry --> boot
  boot --> app
  boot --> adapters
  app --> interfaces
  app --> domain
  interfaces --> domain
  adapters --> interfaces
  adapters --> domain
```

The arrows are the only imports allowed. import-linter enforces them (`make arch`, five
contracts in `pyproject.toml`), pyright runs in strict mode on `domain`, `interfaces` and
`application`, and LangChain is imported only in `adapters/llm`.

## 3. One run: claim, think, commit, deliver

```mermaid
sequenceDiagram
  autonumber
  participant Q as Event queue (per-load FIFO)
  participant R as AgentRuntime
  participant S as Store (unit of work)
  participant G as Run graph
  participant M as Models (OpenRouter)
  participant O as Outbox dispatcher
  participant W as Gateways

  R->>S: transaction 1: claim the oldest event whose load has nothing in flight
  S-->>R: event and run context (load, ledger, timers, last 30 events, documents, catalog)
  R->>G: think, outside any transaction, under the 240 s deadline
  G->>M: gate call (messages only), then agent rounds
  M-->>G: intent, tool calls, finish
  G-->>R: verdict: status and approved actions
  R->>S: transaction 2: executor applies actions (ledger, timers, stage), outbox items, agent_action events, run record, event completed
  O->>S: pending outbox items
  O->>W: send with idempotency key event_id:index
  W-->>O: sent, or error (retried up to 5 times)
```

No transaction stays open while a model thinks. A run either commits everything it
decided or nothing. A worker that dies after the claim leaves the event in flight; the
claim lease (deadline plus 60 s) returns it to the queue, counted as a failed attempt;
three failed attempts make it a dead letter.

## 4. The run graph

```mermaid
flowchart TD
  start(["claimed event, run context loaded"]) --> prefilter
  prefilter["prefilter: code checks"]
  prefilter -- "duplicate, stale timer, empty message" --> skipped(["skipped: no model call"])
  prefilter -- "sender is not a load contact" --> unknown(["escalated: Hero task, no model call"])
  prefilter --> gate
  gate["gate: intent from code for system events; for a message, a decision model's probabilities and a policy in code"]
  gate -- "a quiet intent and P(act) under 0.2" --> noop1(["noop: one model call"])
  gate --> knowledge
  knowledge["knowledge: SOP sections for intent and stage, procedures in progress, catalog of the rest"]
  knowledge --> agent
  agent["agent: one model round"]
  agent -- "tool calls" --> tools
  agent -- "prose instead of a tool call: asked to finish" --> agent
  agent -- "finish" --> validate
  tools["tools: read tools answer; action tools stage, checked by the rules at the call"]
  tools -- "answers" --> agent
  tools -- "finish in the same round, or refused calls over max_rejections" --> validate
  validate["validate: rules over every staged action"]
  validate -- "approved actions" --> acted(["acted: the commit executes them"])
  validate -- "nothing approved" --> noop2(["noop"])
  validate -- "high-severity violation or rejection budget spent" --> blocked(["escalated: blocked task; approved actions still run"])
  validate -- "no_procedure and nothing approved" --> gap(["escalated: SOP gap task"])
```

| Budget | Default | Where | When it is spent |
|---|---|---|---|
| `max_steps` | 8 rounds | `BrokerLimits`, per client | The next round must call `finish` (the tool choice forces it) |
| `max_cost_usd` | $0.05 per run | `BrokerLimits` | The next round must call `finish` |
| `max_rejections` | 3 refused action calls | `BrokerLimits` | The run stops and escalates; accepted actions still run |
| Model timeout | 60 s per round, its retries included | `RuntimeConfig.llm_timeout_s` | The call fails over to the next model of the role |
| Rate limit or overloaded provider | 2 retries on the same model, about 1 s then 2 s apart | `RetryPolicy`, `adapters/llm/resilient.py` | The call fails over to the next model of the role |
| Run deadline | 240 s | `RuntimeConfig.run_deadline_s` | The run escalates with one urgent Hero task; the event completes |
| Model failure | after the retries and the fallbacks | `adapters/llm/resilient.py` | The run escalates with one urgent Hero task; the event completes |

The graph is `application/graph/build.py`; the platform draws the same graph from
`GET /api/graph`, and LangGraph Studio shows it inside its scenario graph.

## 5. Memory

```mermaid
erDiagram
  LOAD ||--o{ EVENT : "event log, append-only"
  LOAD ||--o{ OBLIGATION : "ledger"
  OBLIGATION ||--o{ TIMER : "follow-up timers"
  LOAD ||--o{ DOCUMENT : "attachments read"
  EVENT ||--o| RUN_RECORD : "one run per triggering event"
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

Every run reads one snapshot of this memory (`application/context.py`); it never relies
on anything kept in the process between runs. The agent's own actions come back as
`agent_action` events, so the next run reads what was sent, to whom and when.

The memory lives in the store `PERSISTENCE` names, behind the interfaces of
`interfaces/repositories.py`: in the process (`adapters/persistence/memory/`, the default,
the tests, the evals and Studio, each play in a fresh world), or in Postgres
(`adapters/persistence/postgres/`, the Docker Compose platform), one table per entity of
the diagram plus `risk_trackers` and `event_queue` (`migrations/`, applied by
`agent db migrate`). A unit of work is one transaction; the queue claim and the due
timers skip rows another worker holds (`FOR UPDATE SKIP LOCKED`); a partial unique index
allows one event in flight per load. Both stores pass the same contract suites.

### Facility memory, across loads

The memory above ends with its load. Facility notes outlive it: what a driver learns about
a pickup or a delivery place is kept for the next load of the same client at that place.

```mermaid
flowchart LR
  subgraph run["A run (knowledge node, outside the units of work)"]
    K["facts_in_view: current and next stop"] --> P["agent prompt: Facility notes (untrusted)"]
    T["recall_facility tool"]
  end
  subgraph store["LangGraph store (InMemoryStore with an embeddings index)"]
    N[("facility_memory / client / facility key: one item per note, text embedded")]
    J[("facility_memory_jobs / client: one job per delivered load")]
  end
  C["commit of the run that sets delivered"] -->|enqueue| J
  X["tick, after the outbox: MemoryExtractor"] -->|pending| J
  X -->|"one memory model call: add, update, delete, each citing an event"| G["guardrails in code (domain/memory.py)"]
  G -->|"writes that pass, one client's namespace"| N
  K -->|recall by client and facility| N
  T -->|semantic search by client| N
```

- A note (`FacilityFact`) is one short text with a kind (`check_in`, `hours`,
  `requirement`, `access`, `contact`, `other`), its facility (`facility_key`: the stop's
  normalized name and address), its client, and its source: the load, the inbound event and
  when it was said.
- Embeddings: `openai/text-embedding-3-small` on OpenRouter (`embeddings.memory` in
  `config/models.yaml`), or a deterministic offline hash without the key and in the tests.
- The store is any LangGraph `BaseStore` (`adapters/memory/store.py`); a `PostgresStore`
  with pgvector takes the same namespaces.

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
  Note over R: ping filter confirms low to high and logs risk_changed (no model)
  R->>A: 07:01 run 1: intent delay_risk_high from code, Client A delay_risk_high section
  A-->>R: send_sms(driver, eta_request), schedule_timer(eta_request, 30), finish
  R->>L: open eta_request with 1 attempt, timer T1 due 07:31
  R->>D: SMS asking for the ETA
  D->>R: 07:12 "What's the PO number?"
  R->>G: 07:13 run 2: triage the message
  G-->>R: question (1.00), needs the agent now (0.97), so on to the agent
  R->>A: driver_questions in full, delay_risk_high as procedure in progress, ledger: 1 attempt, T1 at 07:31
  A-->>R: send_sms(driver, reply, "PO 55821"), finish
  Note over L: unchanged: a reply requests nothing, so attempts 1 and T1 stay
  R->>D: SMS with PO 55821
  L->>R: 07:31 T1 fires as timer_due (still the active timer, so not stale)
  R->>A: run 3: intent eta_follow_up_due from code, ledger: 1 attempt
  A-->>R: send_sms(driver, eta_request), schedule_timer(eta_request, 30), finish
  R->>L: attempts 2, T2 due 08:01
  R->>D: second SMS asking for the ETA
  L->>R: 08:01 T2 fires
  R->>A: run 4: ledger: 2 attempts of 2
  A-->>R: send_email(dispatcher), create_hero_task(call the driver), close_obligation(eta_request, exhausted), finish
  R->>P: email in the main thread
  R->>H: Hero task
```

Runs of injected events happen 60 s after the event in the evals and on the platform;
timers fire with zero latency, so the 08:02 actions of the assignment's table happen at
08:01 here. The scenario is `evals/scenarios/core/a_client_a_high_risk.yaml`.

## 7. Documents

```mermaid
flowchart LR
  upload["message with an attachment, or POST /api/loads/ID/attachments"] --> blob[("blob store: sha256 reference")]
  upload --> pending["Document saved pending, in the message's transaction"]
  pending --> tick["next tick, before the queue runs"]
  tick --> pages["pypdfium2: text per page; a page with little text becomes a PNG"]
  pages --> extract["one extract call (Gemini 2.5 Flash): type among the client's document types, field values"]
  extract --> checks["code checks against the load: PO, MC, stop names, date window, seal, signature"]
  checks --> status{"status"}
  status -- "every check passed" --> processed["processed: closes the matching requirement's obligation as satisfied"]
  status -- "check failed, field missing, low confidence, exceptions" --> review["needs_review"]
  status -- "file or model error" --> failed["failed"]
  processed --> event["document_processed event doc:ID, queued once"]
  review --> event
  failed --> event
  event --> run["its run: the agent reads the document and follows sops/standard/documents.md"]
```

The message's own run never acts on the attachment; only the `document_processed` run
does, so a document is handled once. Code: `application/documents.py`,
`domain/documents.py`, `adapters/documents/pdfium.py`.

## 8. SOP layers and selection

```mermaid
flowchart TB
  subgraph tree["sops/ (Markdown, one section per file)"]
    standard["standard/*.md: 11 sections every client reads"]
    client["clients/ID/*.md: replace or extend a standard section by id"]
    loadtype["clients/ID/types/TYPE/*.md: replace or extend for one load type"]
    catalog["clients/ID/broker.yaml: the client's intents, obligations, documents, limits"]
  end

  standard --> merge
  client --> merge
  loadtype --> merge
  merge["merge by section id: standard, then client, then load type"] --> visible["the sections this client and load type can see"]

  event["the event's intent (code or gate) and the load's stage"] --> select
  visible --> select["by_applies: section intents include the intent, or are empty; stages include the stage, or are empty"]
  select --> prompt["agent prompt: matching sections in full, the procedure of each open obligation as context, one line per other section"]
  visible --> tools["agent tools: search_sop (word search), read_sop_section"]
  catalog --> rendered["the catalog rendered into the gate and agent prompts and the rules' limits"]
  rendered --> prompt
```

A client folder holds only what the client changes. Another client's sections are never
visible. The tree's content hash is recorded with every run as `sop_version`.

## 9. Evals and prompt versions

```mermaid
flowchart LR
  subgraph git["Git"]
    scenarios["evals/scenarios/SUITE/*.yaml"]
    copy["prompts/ROLE.yaml: reviewable copy"]
    pins["config/prompts.yaml: ROLE pinned to freight-agent-ROLE:prod"]
  end

  subgraph ls["LangSmith"]
    dataset["dataset freight-agent-SUITE: one example per scenario"]
    hub["Prompt Hub: freight-agent-gate, -agent, -extract, -judge, -gate_decision; commits, tags prod and candidate"]
    experiment["experiment: rows, evaluator scores, pass^k summary, metadata"]
  end

  scenarios -- "agent eval run syncs: an example rewritten when its scenario hash changes" --> dataset
  copy -- "agent prompts push --tag candidate" --> hub
  hub -- "agent prompts pull" --> copy
  pins --> resolve["resolved once at process start; recorded per run"]
  hub --> resolve
  dataset --> run["aevaluate: k repetitions, each a fresh runtime on a virtual clock, live models"]
  resolve --> run
  run --> experiment
```

An experiment's metadata names the git sha, the model of each role, the prompt commit of
each role and the SOP hash. Promotion of a prompt: edit, `push --tag candidate`,
`agent eval run --prompt-tag candidate`, compare in LangSmith, `push --tag prod`, restart.

## 10. Production at 10x (designed)

Not built, except Postgres, which the platform already runs on. The interfaces stay the
same; the adapters behind them change.

```mermaid
flowchart LR
  webhooks["Ingress API: SMS, email, Slack webhooks, tracking feed"] --> pingstream[["Ping stream, partitioned by load"]]
  pingstream --> pingworker["Ping filter workers: only risk transitions continue"]
  webhooks --> queue[["Event queue: SQS FIFO, MessageGroupId = load_id"]]
  pingworker --> queue
  webhooks --> objects[("Object store: attachment bytes")]
  objects --> docworkers["Document workers: pages, extract call, checks"]
  docworkers --> queue
  scheduler["Timer scheduler: due timers claimed with FOR UPDATE SKIP LOCKED"] --> queue
  queue --> workers["Agent workers: stateless, autoscaled on queue age"]
  workers <--> pg[("Postgres: events partitioned by month, ledger, timers, runs, outbox")]
  docworkers <--> pg
  scheduler <--> pg
  workers <--> models["OpenRouter: per-role models, fallbacks, rate limits"]
  workers -.-> traces["LangSmith: sampled traces"]
  pg --> dispatcher["Outbox dispatcher"]
  dispatcher --> integrations["SMS provider, email, Slack, TMS, Hero task tool"]
  pg --> metrics["Metrics and alerts: queue age against 5 minutes, run p95, cost per load, dead letters"]
```

Sizing and the reasons for each piece are in [`answers.md`, 5.2](answers.md#52-volume-x10-what-must-change).
An earlier build of the pgvector search and the operational metrics is at the git tag
`full-platform`.
