<!-- generated-by: gsd-doc-writer -->
# Durable load workflows

`agent temporal` runs the existing LangGraph agent with one active Temporal Workflow
per broker/load. Its workflow ID is `load/<broker_id>/<load_id>` (URL-escaped components).
Postgres keeps the business state; Temporal keeps the orchestration history and waits.
The broker workspace UI uses these real-clock services for every scenario. The eval
CLI, Studio and compatibility simulator API retain their virtual-clock behavior.

## Local service topology

The two Compose projects use separate application databases. The diagram shows the local
demo deployment; `worker` executes both Workflow tasks and Activities. Temporal's own
SQLite volume stores orchestration history, independently of the business Postgres data.

```mermaid
flowchart TB
  browser["Browser<br/>Broker workspace"]
  subgraph platform["Main Compose project"]
    app["app :8000<br/>UI + /api/durable bridge"]
    legacy[("Postgres :5432<br/>Compatibility simulator")]
    app -->|Legacy simulator API| legacy
  end
  subgraph durable["compose.temporal.yml"]
    subgraph services["Application processes"]
      ingress["ingress :8001<br/>Workspace and durable API"]
      worker["worker<br/>Workflows and Activities"]
      relay["relay<br/>Recovery and facility memory"]
    end
    db[("Postgres :5433<br/>Business state + profiles + vector memory")]
    blobs[("Shared blobs volume<br/>Attachment bytes")]
    temporal["Temporal start-dev<br/>gRPC :7233 / UI :8233"]
    history[("Temporal volume<br/>SQLite execution history")]
    migrate["migrate<br/>One-shot schema setup"]
    services <--> db
    services --> blobs
    services <-->|Signal, poll, report results| temporal
    temporal --> history
    migrate --> db
  end
  browser -->|HTTP| app
  app -->|/api/durable proxy| ingress
```

The main API uses `host.docker.internal:8001` in Docker to reach the durable ingress.
The ports above are host ports; both Postgres containers listen on 5432 internally.
The API and relay notify Temporal after business commits. Model calls and channel sends
are Activity work; the relay separately runs the facility-memory extraction pipeline.
The detailed [workflow loop](architecture.md#10-durable-execution-and-production-at-10x)
shows task priority and cooperative stop.

## Timer lifecycle: 30-minute ETA follow-up

This sequence follows Northline's seeded 30-minute cadence without a deferral. A saved
broker profile can set a different interval. Each arrow from the Workflow to business
storage goes through an Activity.

```mermaid
sequenceDiagram
  autonumber
  participant U as Operator / workspace
  participant I as Durable ingress
  participant W as LoadWorkflow
  participant A as Activities / Runtime
  participant P as Application Postgres
  participant G as Recording gateway

  W->>A: run_load_graph(exact event ID)
  A->>P: Commit ETA obligation + pending SMS outbox
  A-->>W: Complete
  W->>A: deliver_load_item(stable outbox ID)
  A->>G: Send SMS with stable idempotency key
  G-->>A: accepted_at
  A->>P: Atomic: sent receipt + timer due_at = accepted_at + 30 min
  A-->>W: Complete
  W->>A: load_snapshot
  A-->>W: Next absolute deadline
  Note over W: Durable wait, no sleeping Activity or model call
  alt Input arrives before the deadline
    U->>I: Driver message or operator stop
    I->>P: Persist input or stop request
    I->>W: Signal with Start: wake
    Note over I,W: Relay repairs a lost notification
    W->>A: Reread snapshot, process selected work
    alt Driver supplies an ETA and agent satisfies the obligation
      A->>P: Close obligation + cancel pending timer
    else Driver asks for PO information
      A->>P: Commit reply, preserve existing ETA deadline
      Note over W: Send reply, then wait for the original deadline
    else Operator requests stop
      A->>P: Cancel pending work, mark automation stopped
      Note over W: Complete after the current Activity settles
    end
  else Deadline expires
    W->>A: load_snapshot, then fire_load_timer if still due
    A->>P: Recheck active obligation, atomically mark fired + queue timer_due
    Note over P: Existing events retain their FIFO position
    W->>A: run_load_graph when timer event reaches FIFO head
    A->>P: Commit retry or escalation according to pinned SOP
  end
```

A message is not automatically an ETA: the agent must interpret it and satisfy the
obligation. Timer validity is rechecked both at firing and before the event acts. A
worker outage delays execution but does not erase the deadline. A gateway retry can
repeat the external call; a real provider must persistently deduplicate the key. The
recording adapter deduplicates only within its process. For stop semantics, a delivery
already running may complete before the stop is confirmed.

## What changed

- `orchestration/workflow.py` makes decisions only from Activity results, signals and
  Temporal's clock. No database, network or LLM call runs inside replayable workflow code.
- `run_load_graph` calls `AgentRuntime.process_event(load_id, event_id)`. This executes
  the existing graph, validator and executor inside an Activity. A retry after commit
  finds the stored run and does not call the model again. A retry before commit can
  repeat model calls; staged tools have not sent external messages yet.
- Claims carry tokens. Commit and release verify the token under a database lock, so
  an old Activity cannot write after another worker has reclaimed its event. An exact
  event claim cannot take another load or jump its load's FIFO.
- In durable mode, an approved SMS/email with `requests` automatically binds the
  broker catalog's `follow_up_minutes` to its outbox item. `eta_request` defaults to
  **30 minutes**. The model need not call `schedule_timer` to establish that follow-up.
  A timer requested in the same plan does not override the catalog's cadence.
- Confirmation and creation of the timer commit together. The deadline uses the
  gateway's `DeliveryReceipt.accepted_at`, when supplied, or the time its successful
  response was observed. The same outbox item cannot reset an already committed timer.
- A provider must durably deduplicate the idempotency key and return the original
  acceptance timestamp on retries to preserve the deadline across a lost response.
  Providers returning `None` retain the old gateway contract; their timestamp is the
  local observation time, so that external lost-response window remains approximate.
- PO questions leave an existing deadline intact unless the agent explicitly performs
  a valid ledger change. Closing an obligation cancels its timer. Expiry rechecks the
  current ledger; an old timer cannot revive a closed or superseded obligation.
- Overdue timers enter the FIFO before the workflow drains more messages. Messages
  already queued remain ahead of that timer; later traffic cannot indefinitely starve it.
- The relay repairs a crash between the Postgres commit and Signal with Start. It
  checks queued/in-flight events and pending deliveries every 10 seconds by default;
  on startup it also discovers pending timers. Notifications are coalesced wakeups,
  not copies of business events. Temporal receives IDs, not message bodies or SOP text.
- Continue-as-new after 100 turns (or Temporal's history-size suggestion) keeps history
  bounded. New runs reread absolute deadlines. A delivered load closes after its queue,
  outbox and obligations are resolved. A late event can start a new execution under the
  same workflow ID; Temporal still permits only one active execution for that ID.

The domain database still uses a globally unique `load_id`. Durable registration refuses
an ID belonging to a different broker and does not overwrite an existing load's state.
Provider message IDs are namespaced by broker/load/channel kind at ingress.

## Run locally

Use the standalone compose file for the durable backend and the main compose file
for the UI/API bridge. They keep separate databases; simulator reset/advance endpoints
must never operate on the durable database. Configure the model key and prompt source
as described in the [root setup](../README.md#2-set-up) first.

```sh
docker compose -f compose.temporal.yml up -d --build
docker compose up -d --build app
```

- Broker workspace UI: <http://localhost:8000/#/scenarios?broker=northline>
- Durable API and interactive OpenAPI: <http://localhost:8001/docs>
- Temporal UI: <http://localhost:8233>
- Application Postgres: localhost:5433 (`agent` / `agent` / `agent`)
- Temporal gRPC: localhost:7233, namespace `default`, task queue `freight-loads`

The `migrate` job applies all migrations, including `0003_temporal.sql` and
`0004_broker_profiles.sql`. The `worker` service runs Workflows and Activities.
The `relay` service repairs notifications and
runs the facility-memory pipeline; run **one relay per database**. Document extraction
runs in a scoped Activity in the load Workflow, before its next graph invocation, so
it cannot change the ledger concurrently with that load's agent.
The `ingress` service accepts real-clock events. These services share the application DB
and attachment volume; the Temporal server has its own persisted volume.

The Temporal container uses the official CLI development server with a SQLite file.
It survives a container restart, but this compose stack is for local demonstration.
Production requires an operated Temporal cluster (or Cloud), supported persistence,
backups, authentication and worker deployment/versioning. See
[Temporal's self-hosted guide](https://docs.temporal.io/self-hosted-guide).

To run Python processes on the host with the two infrastructure containers:

```sh
docker compose -f compose.temporal.yml up -d postgres temporal
export PERSISTENCE=postgres
export DATABASE_URL=postgresql://agent:agent@localhost:5433/agent
export TEMPORAL_ADDRESS=localhost:7233
uv run agent db migrate
# Three terminals with the same environment:
uv run agent temporal worker
uv run agent temporal relay
uv run agent temporal serve
```

For a host-run UI API, start `PERSISTENCE=memory uv run agent serve` in another terminal
and run the web development server as described in [development setup](development.md).
That API's compatibility simulator stays in memory; live operations use the durable
ingress and its Postgres database.
`DURABLE_API_URL` defaults to `http://127.0.0.1:8001`; the containerized UI API uses
`http://host.docker.internal:8001` to reach the separate stack.

As elsewhere in the assessment, **gateways record deliveries; they do not send real
SMS/email/Slack**. OpenRouter is live when its key is configured. Without a model key,
the workspace start endpoint returns 503 instead of starting a live scenario. The
lower-level runtime retains its unavailable-model fallback. The API is
local and unauthenticated; broker validation is data isolation, not caller authentication.

## Drive live scenarios

### Broker workspaces and pinned SOPs

Select Northline, Keystone, Aurora or Acme in the sidebar, choose a scenario under
**Cenários**, and select **Iniciar ao vivo**. There are 36 YAML scenarios and the
generated Aurora demonstration, grouped by broker. Each start creates a fresh load,
shifts its appointment windows into live time, saves its broker catalog and SOP
sections, and ingests an initial location ping. A high-risk scenario starts with high
risk; a quiet scenario keeps its fixture's risk.

In **Operações**, send sample messages/documents with **Enviar esta entrada**, or use
the contact/channel composer for custom driver, dispatcher or broker messages. The
operator controls inbound messages; wait-only steps cannot advance time. The screen
polls every two seconds and displays deadlines, recorded communications and agent
decisions. Closing the tab does not interrupt execution; reopen a load from the
broker's operations list.

**SOPs e configurações** edits the broker catalog and client/load-type extensions.
The standard layer is read-only. **Salvar para novas execuções** persists a revision
in `broker_profiles`; a local draft does not affect execution. ETA placeholders use
`{{eta_request.max_attempts}}`, `{{eta_request.follow_up_minutes}}` and
`{{eta_request.on_exhausted}}`. Other obligation kinds expose the same fields.

The start request freezes the rendered sections and catalog in `load.sop_snapshot`.
Graph and document Activities use that snapshot, including after worker replacement.
**Consultar SOP usado** shows the saved text and version. A later profile save changes
new loads only. The snapshot does not pin the application/model build: runtime fixes
such as the [stage-aware operational stop context](stop-grounding.md) also apply to
older loads.

Profile seeds come from `sops/` on first access. File changes do not overwrite saved
profiles, and CLI evals read the files rather than UI profiles. See
[Live broker workspaces](live-workspace.md) and [SOP authoring](../sops/README.md).

**Parar execução** durably records an operator request. It wakes the workflow,
settles its current Activity, cancels pending timers/outbox actions and open
obligations, and completes the workflow. The UI distinguishes a pending stop from
a confirmed stop. A delivery already in progress may finish before confirmation;
recorded sends remain in the history. No new contact starts after the confirmed
stop. A queued control event repairs a lost Temporal notification; the relay and
ingress cannot restart a stopped load. To continue, create a new execution.

### Aurora: ten unanswered requests

Choose Aurora's `aurora_extreme` scenario and leave the driver composer unused.
With the seeded profile, the expected procedure is:

- Send the first ETA request after processing the initial high-risk ping.
- Send requests 2–10 after each preceding delivery's one-minute wait.
- Wait one final minute after SMS 10, create one urgent Hero task and close the ETA
  obligation as exhausted. There is no SMS 11 or dispatcher/Slack escalation.

Aurora's fixture carries 18 tonnes of packaged food from Curitiba to CD Aurora São
Paulo. Its files are `sops/clients/aurora/broker.yaml`,
`sops/clients/aurora/delay_risk_high.md` and `evals/fixtures/load_aurora_extreme.json`.
The workspace derives the effective policy from the saved broker profile. Its frozen
catalog reserves SMS rate capacity for the configured ETA attempts plus a reply; the
file-backed Aurora catalog itself allows ten SMS per hour.

Each deadline is `accepted_at + 1 minute`. Model and queue latency occur after
expiry, so a live run takes **more than ten minutes**. Only confirmed deliveries count
in the UI's progress. For ETA cancellation, start a separate load and send “Chego em
20 minutos” as the driver; the agent should satisfy the obligation and cancel its timer.
SMS and Hero gateways record their deliveries, so this exercises scheduling and model
decisions without reaching a handset or external operations system.

### Northline through the workspace API

This follows the same start path as the UI: saved profile revision, immutable load
snapshot, rebased dates and initial ping. Run it from the project environment with
`uv run python`; it invokes the configured live model and incurs model cost.

```python
from uuid import uuid4

import httpx

api = httpx.Client(base_url="http://localhost:8001", timeout=20, trust_env=False)
broker_path = "/workspace/brokers/northline"
profile_response = api.get(f"{broker_path}/profile")
profile_response.raise_for_status()
profile = profile_response.json()
load_id = f"northline-{uuid4()}"
path = f"{broker_path}/loads/{load_id}"
started = api.post(
    f"{path}/start",
    json={"scenario_id": "a_client_a_high_risk", "revision": profile["revision"]},
)
started.raise_for_status()
print(started.json())  # includes workflow_id for the Temporal UI
print(f"http://localhost:8000/#/durable?broker=northline&load={load_id}")
```

Inspect `GET {path}` until an outbox item is `sent` and an ETA timer is pending.
The seeded Northline deadline is `accepted_at + 30 minutes` (a valid deferral can
move it later). Then submit the PO question from the same Python session:

```python
sent = api.post(
    f"{path}/messages",
    json={
        "message_id": "presentation-po-1",
        "channel": "sms",
        "sender_contact_id": "drv_20931",
        "body": "What's the PO number?",
    },
)
sent.raise_for_status()
```

The reply should cite PO 55821 and preserve the existing timer. Retrying with the
same load/message ID is a duplicate. Open the returned `workflow_id` in Temporal UI
to inspect Activities and the wait. Restarting the worker during a wait demonstrates
recovery, but interrupts processing for every load handled by that worker:

```sh
docker compose -f compose.temporal.yml restart worker
```

The deadline stays unchanged. If no worker is available when it becomes due,
processing resumes when one returns. This guarantees persistence/recovery, not exact
wall-clock execution during an outage. `timer_due.data.due_at` and the event's
`occurred_at` expose lateness. Monitor worker availability, timer lag, queue age and
failed workflows.

### Compatibility endpoints

The current UI uses `/workspace/brokers/...` through the `/api/durable` bridge.
The following older surfaces remain available:

- `POST /loads` accepts `{"metadata": ...}`; `/brokers/{broker_id}/loads/{load_id}`
  exposes snapshots, messages, pings and stop controls. Raw registration does not
  rebase fixture dates or pin a saved workspace profile. Loads registered this way
  use the worker's file-backed SOPs.
- `GET /demos/aurora-policy` and `POST /demos/aurora/{load_id}` use `DemoPolicy` with
  the older `{{max_attempts}}` and `{{interval_minutes}}` placeholders. These are
  compatibility demo routes, not the current broker editor.
- `/api/sim` belongs to the separate platform API on port 8000. It retains the
  virtual-clock simulator for compatibility and tests; workspace operations never
  use its reset/advance controls.

Request schemas are available at <http://localhost:8001/docs> after startup.

## Failure handling and verification

Transient infrastructure failures retry with backoff (1 to 30 seconds). The application
keeps its business limits: three failed event attempts / expired claims and five failed
deliveries by default. Exhaustion fails the workflow for operator attention. The relay
excludes loads with dead letters or failed deliveries, avoiding automatic restart loops.
Investigate the recorded error, repair/requeue the affected row in a controlled operation,
and signal/start that load again. There is no operator remediation UI in this change.
Attempts in the obligation ledger are reservations when the request is staged; a delivery
failure stops progression rather than spending later follow-ups as though it was sent.

```sh
make ci
make integration                     # Postgres at localhost:5432 by default
make temporal-test                   # time-skipping server; no paid model calls
```

Contract tests run against memory and Postgres: scoped FIFO, claim fencing, idempotent
commit, timer-after-confirmation, provider receipt on retry, failed send and cancellation.
The Temporal suite runs the **real Workflow and real LangGraph/runtime**, with scripted
model responses: 30-minute wait, PO + duplicate, ETA cancellation, and replacement worker
while the timer is pending, plus continuation after 100 turns with 105 events queued.
It also checks Aurora's ten requests/final wait and stopping during a wait or graph
Activity. Time skipping makes these cases finish in seconds. CI runs both the Temporal
suite and the Postgres contract suite.

Real-model stop selection is covered separately by the seven scenarios in the
[`grounding` eval suite](stop-grounding.md), included in `make eval`. Those use the
virtual-clock player and file-backed SOPs; they do not grade an edited live profile.

This change has not load-tested the assignment's 100k loads/month target. The relay uses
bounded notification concurrency; startup scans and per-load snapshots can be paginated
or narrowed if measurements show pressure. The five-minute processing target depends on
healthy infrastructure, available workers and providers, and bounded model latency.
