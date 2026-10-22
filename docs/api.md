<!-- generated-by: gsd-doc-writer -->
# Platform API

The current React UI is a broker workspace. Every operation it starts uses a real clock,
Postgres and one Temporal workflow per load. The platform server on port 8000 serves the
UI and proxies `/api/durable/*` to the durable ingress on port 8001. The UI server has
no runtime or database of its own. Virtual-clock playback is available in the CLI evals
and LangGraph Studio.

| Surface | Entry point | API reference |
|---|---|---|
| UI and durable proxy | `uv run agent serve` | `http://127.0.0.1:8000/docs` |
| Durable workspace and integration ingress | `uv run agent temporal serve` | `http://127.0.0.1:8001/docs` |

The proxy upstream is `DURABLE_API_URL` (default `http://127.0.0.1:8001`). The platform's
OpenAPI lists the workspace proxy as a wildcard route; the durable server's OpenAPI
contains the individual routes and request models. See [development](development.md)
for starting both stacks.

## Authentication and transport

Neither API implements authentication or an HTTP request rate limiter. All routes below
require no credentials. The Compose files bind published ports to `127.0.0.1`.
Broker/load checks enforce matching identifiers but are not user authentication.

Bodies are JSON. Domain models serialize
with `model_dump(mode="json")`; timestamps are ISO 8601. The durable workspace stamps
operator messages with server UTC time. Integration messages and location pings carry
their own `occurred_at` values. Broker action limits (for example SMS attempt caps) are
domain rules, separate from HTTP rate limiting.

The model is live on OpenRouter. Outbound gateways currently record SMS, email, Slack,
Hero tasks and TMS actions; they do not contact external recipients. The health response
reports `gateways: "recording"` and `model_ready`.

## Current UI: broker workspace

Use prefix `/api/durable/workspace` on port 8000, or `/workspace` directly on port 8001.
The table uses the latter. The seeded brokers are `northline`, `keystone`, `aurora` and
`acme`; scenarios are filtered by their fixture's broker.

| Method | Path | Body / response | Auth required |
|---|---|---|---|
| GET | `/workspace/brokers` | `[{id, name, description}]` | No |
| GET | `/workspace/brokers/{broker}/profile` | `BrokerProfile`; seeds the saved profile on first read | No |
| PUT | `/workspace/brokers/{broker}/profile` | Complete `BrokerProfile` with its current `revision`; returns the saved profile with incremented revision | No |
| GET | `/workspace/brokers/{broker}/scenarios` | `[{id, title, description, suite, load_types, stage, steps}]` | No |
| GET | `/workspace/brokers/{broker}/loads` | `[{load_id, stage, stopped, stopping, scenario, version, started_at}]` | No |
| POST | `/workspace/brokers/{broker}/loads/{load}/start` | `{scenario_id, revision}` → `{load_id, broker_id, workflow_id}` (202) | No |
| GET | `/workspace/brokers/{broker}/loads/{load}` | `LoadSnapshot` plus `processing` | No |
| POST | `/workspace/brokers/{broker}/loads/{load}/messages` | `OperatorMessage` → `IngestResult` (202) | No |
| POST | `/workspace/brokers/{broker}/loads/{load}/pings` | `LocationPing` → `IngestResult` (202) | No |
| POST | `/workspace/brokers/{broker}/loads/{load}/steps/{index}` | No body; injects that zero-based scenario input → `IngestResult` (202) | No |
| POST | `/workspace/brokers/{broker}/loads/{load}/stop` | No body → `{status: "stopping" \| "stopped"}` (202) | No |
| GET | `/health` | `{status: "ok", mode: "temporal", gateways: "recording", model_ready}`; proxy path `/api/durable/health` | No |

`BrokerProfile` has `revision`, a complete domain `broker` catalog (intents, obligations,
document definitions and limits), and `sections`. Each section carries `section_id`,
`layer` (`standard`, `client`, `load_type`), `client_id`, `load_type`, `title`,
`description`, `intents`, `stages`, `mode` (`replace`, `extend`), `body` and `source`.
Read the profile before editing and PUT the full document back. A stale revision is a
409. Standard sections are inherited and immutable through this API; customize them
with a client or load-type replacement/extension.

Starting a load freezes the profile revision, rendered SOP sections, broker catalog,
scenario and start time in `load.sop_snapshot`, with a content version. Later profile
edits apply to new loads. Appointment windows are shifted into real time, and an initial
location ping is always submitted. A quiet scenario may produce no model call from that
ping. The load ID must match `[a-z0-9-]{1,80}`. Retrying the same load ID, scenario and
original revision reuses the accepted start; a conflicting start returns 409.

Start request, using a revision returned by the profile endpoint:

```json
{
  "scenario_id": "d1_keystone_high_risk",
  "revision": 1
}
```

`OperatorMessage` uses a contact from the load and one of that contact's allowed channels.
The server supplies the load ID and current UTC timestamp. Text must contain non-whitespace
characters and be at most 2,000 characters. Keep `message_id` stable when retrying:

```json
{
  "message_id": "driver-reply-001",
  "sender_contact_id": "drv_20931",
  "channel": "sms",
  "body": "I will reach the pickup in 20 minutes."
}
```

`LocationPing` includes the path's load ID and an explicit timestamp:

```json
{
  "ping_id": "location-002",
  "load_id": "keystone-demo-001",
  "lat": 37.9577,
  "lon": -121.2908,
  "occurred_at": "2026-10-21T12:00:00Z",
  "delay_risk": "high"
}
```

`IngestResult` is `{accepted, duplicate, event_id, reason}`. HTTP acceptance means the
input was committed, not that the agent has finished. The relay repairs missed Temporal
notifications. The UI polls the load snapshot every two seconds and the broker's load
list every five seconds. `processing` indicates
a queued load event, not every possible workflow activity.

`LoadSnapshot` contains `load`, `obligations`, `timers`, `events`, `outbox` and `runs`.
The load includes `sop_snapshot`, `automation_stop_requested_at` and
`automation_stopped_at`. Durable outbox items include delivery state and `accepted_at`;
ETA follow-up timers are based on confirmed gateway acceptance.

Scenario steps are reference inputs, not an automatic test runner in this UI. Message
and ping steps run when the operator clicks them. Repeated clicks reuse stable IDs.
Wait-only steps return 409: only the real Temporal timer advances the procedure. Example
ETA times and supported document fixtures are adapted to the live load. Scenario
`expect`/`forbid` assertions are graded by the eval runner, not by this endpoint.

Stop is asynchronous and durable. New inputs are rejected once a stop is requested;
the worker settles any current activity, cancels pending work and records
`automation_stopped_at`. Already accepted outbound actions remain in history. Poll for
confirmation; start a new load to run again.

## Durable integration routes

These routes are directly available on port 8001 and use the same real-clock service.
The generic integration routes are not all exposed by the platform proxy.

| Method | Path | Body / response | Auth required |
|---|---|---|---|
| POST | `/loads` | `{metadata: <assignment load metadata>}` → `{workflow_id, load_id}` (201) | No |
| GET | `/brokers/{broker}/loads/{load}` | `LoadSnapshot` | No |
| POST | `/brokers/{broker}/loads/{load}/messages` | `InboundMessage` → `IngestResult` (202) | No |
| POST | `/brokers/{broker}/loads/{load}/pings` | `LocationPing` → `IngestResult` (202) | No |
| POST | `/brokers/{broker}/loads/{load}/stop` | `{status: "stopping" \| "stopped"}` (202) | No |

`InboundMessage` requires `message_id`, `load_id`, `channel`, `body` and `occurred_at`;
`sender_contact_id` defaults to null and `attachments` to an empty list. Attachment
references must already be accessible to the configured blob store. Message IDs are
scoped by workflow and channel; ping IDs by workflow. Unknown sender handling belongs
to the runtime, while the workspace composer requires a known contact.

Generic registration creates the per-load workflow without the workspace scenario
snapshot or automatic initial ping. Submit the integration's ping/message separately.

## Durable errors

Errors use `{"detail": "..."}`; FastAPI validation errors use a `detail` list.

| Status | Meaning |
|---|---|
| 409 | Domain conflict: stale profile revision, conflicting start, mismatched broker/load, stopped execution, unknown scenario or wait-only step |
| 422 | Invalid JSON model, load ID, sender/channel/text, or registration metadata |
| 503 | Missing OpenRouter key when starting a scenario, or an unavailable/non-JSON upstream at the platform proxy |

The proxy forwards upstream JSON status codes and uses a 15-second HTTP timeout. A
timeout after an accepted write may still leave work committed: retry with the same
input/load ID. `/health` is an ingress response, not proof that a worker is polling.

## Platform liveness and retired routes

`GET /api/health` on port 8000 checks only the UI/proxy process and returns
`{status: "ok", mode: "workspace-proxy"}`. Use `/api/durable/health` to check ingress;
neither endpoint proves a worker is currently polling.

The virtual-clock HTTP simulator (`/api/sim`, `/api/loads`, `/api/v1`, `/api/scenarios`,
`/api/sops`, `/api/graph`, `/api/fixtures`, `/api/stream`) and the original Aurora-only
start/proxy routes were removed. They return 404. Use `/workspace/brokers/aurora/...`
for Aurora, the generic durable integration routes for external inputs, and
`agent play` / `agent eval run` / Studio for virtual-clock playback. Arbitrary uploads
and saving a conversation as YAML through the old HTTP API are no longer supported.
The workspace still accepts supported scenario attachment fixtures.

Existing loads and their pinned snapshots remain readable and stoppable through the
broker workspace. No migration deletes historical loads, SOPs or volumes.
CORS permits the local Vite origins `http://localhost:5173` and `http://127.0.0.1:5173`.
