# Durable load workflows

`agent temporal` runs the existing LangGraph agent with one active Temporal Workflow
per broker/load. Its workflow ID is `load/<broker_id>/<load_id>` (URL-escaped components).
Postgres keeps the business state; Temporal keeps the orchestration history and waits.
The existing simulation UI, evals and Studio keep their virtual-clock behavior.

```mermaid
flowchart LR
  Input[Messages / location pings] --> API[Durable ingress API]
  API --> DB[(Postgres event FIFO)]
  API -->|Signal with start| WF[LoadWorkflow]
  DB --> Relay[Recovery relay]
  Relay -->|Missed notification| WF
  WF -->|Activity: exact event ID| Graph[Existing LangGraph runtime]
  Graph -->|Atomic commit| State[(Run + ledger + outbox)]
  WF -->|Activity: stable action ID| Send[Gateway]
  Send -->|Receipt + timer, one transaction| State
  WF --> Wait[Durable wait until due_at]
  Wait -->|Recheck timer / enqueue event| DB
```

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

Use the standalone compose file. It isolates durable data from the simulator, whose
reset/advance controls must never operate on the durable database.

```sh
docker compose -f compose.temporal.yml up --build
```

- Durable API and interactive OpenAPI: <http://localhost:8001/docs>
- Temporal UI: <http://localhost:8233>
- Application Postgres: localhost:5433 (`agent` / `agent` / `agent`)
- Temporal gRPC: localhost:7233, namespace `default`, task queue `freight-loads`

The `migrate` job applies the migrations, including `0003_temporal.sql`. The `worker`
service runs Workflows and Activities. The `relay` service repairs notifications and
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

As elsewhere in the assessment, **gateways record deliveries; they do not send real
SMS/email/Slack**. OpenRouter is live when its key is configured. Without a model key,
model-dependent runs escalate using the existing unavailable-model adapter. The API is
local and unauthenticated; broker validation is data isolation, not caller authentication.

## Drive the assignment scenario

Register `evals/fixtures/load_481207.json` through `POST /loads` wrapped in a `metadata`
object. Then send a high-risk ping with a current timestamp. This script uses the live
model configured in the worker, so it may incur model cost:

```python
import json
from datetime import UTC, datetime
from pathlib import Path
import httpx

api = httpx.Client(base_url="http://localhost:8001", timeout=20)
metadata = json.loads(Path("evals/fixtures/load_481207.json").read_text())
api.post("/loads", json={"metadata": metadata}).raise_for_status()
path = "/brokers/northline/loads/481207"
api.post(f"{path}/pings", json={
    "ping_id": "presentation-risk-1", "load_id": "481207",
    "lat": 37.95, "lon": -121.29, "delay_risk": "high",
    "occurred_at": datetime.now(UTC).isoformat(),
}).raise_for_status()
print(api.get(path).json())
```

Inspect the load at `GET /brokers/northline/loads/481207` until its outbox is `sent` and
an ETA timer is pending. The deadline is `accepted_at + 30 minutes` (or later if a valid
promise requires it). Send the PO question to the same path plus `/messages`:

```json
{
  "message_id": "presentation-po-1",
  "load_id": "481207",
  "channel": "sms",
  "sender_contact_id": "drv_20931",
  "body": "What's the PO number?",
  "occurred_at": "REPLACE_WITH_CURRENT_ISO_TIMESTAMP"
}
```

The response should cite PO 55821 and preserve the existing timer. Resending this exact
message ID is a duplicate. In the Temporal UI, open `load/northline/481207` to see the
Activity calls and durable timer. Restart just the worker during the wait:

```sh
docker compose -f compose.temporal.yml restart worker
```

The deadline stays unchanged. If no worker is available at that instant, processing
resumes when one returns. This guarantees persistence/recovery, not exact wall-clock
execution during an outage. `timer_due.data.due_at` and the event's `occurred_at` expose
that lateness. Monitor worker availability, timer lag, queue age and failed workflows.

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
Time skipping makes these cases finish in seconds. CI runs
both the Temporal suite and the Postgres contract suite.

This change has not load-tested the assignment's 100k loads/month target. The relay uses
bounded notification concurrency; startup scans and per-load snapshots can be paginated
or narrowed if measurements show pressure. The five-minute processing target depends on
healthy infrastructure, available workers and providers, and bounded model latency.
