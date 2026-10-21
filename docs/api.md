# Platform API

The backend of the platform: drive one simulated world through time, play every contact
of a load, play an eval scenario live and follow its checks, and inspect every agent run.
The code is in `src/freight_agent/api/`; the React UI in `web/` is built on this API.

```
uv run agent serve                      # http://127.0.0.1:8000; --host and --port to change
open http://127.0.0.1:8000/docs         # OpenAPI, generated from the same models
```

Base path `/api`, JSON everywhere. Domain objects are serialized as they are
(`model_dump(mode="json")`); datetimes are ISO 8601 with their offset, in the
simulation's timezone, and a request time without an offset is read in that timezone.

The API runs **one simulation in process**: the same `AgentRuntime` as the evals and the
CLI, built by the composition root, on a virtual clock, an in-memory store and recording
gateways (nothing is sent anywhere). The model is always the live one on OpenRouter: every
run that reaches a model spends from `OPENROUTER_API_KEY`. Without the key the platform
starts, `SimState.model_ready` is false, scenarios cannot start (503), and a run that
needs the model escalates to a Hero. With `LANGSMITH_TRACING=true` and its key, each run
has a LangSmith trace and `trace_url`.

Writes (anything that moves the clock or feeds the agent) are serialized by one lock;
reads are never blocked by a run that is thinking. While a scenario plays, every write
from a client is refused with 409.

## Simulation and clock

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/sim/reset` | optional `{"start": "2026-10-13T06:55:00-07:00", "timezone": "America/Los_Angeles"}` | `SimState` |
| GET | `/api/sim/state` | | `SimState` |
| POST | `/api/sim/advance` | `{"minutes": 30}` or `{"to": ISO}`, exactly one (422 otherwise) | `StepResult` |
| POST | `/api/sim/tick` | | `StepResult` |

- A new app, and `reset` without `start`, starts at `2026-10-13T06:55` in
  `America/Los_Angeles`, five minutes before the assignment's ping. `reset` clears the
  loads, runs, documents and blobs. An unknown timezone is a 400 and the old simulation
  stays.
- `advance` moves time as the scenario player does: it stops at every pending timer due
  on the way, in due order, sets the clock there and ticks (timers fire with zero
  latency); then it sets the target time and ticks once more. A target before now is a
  400.
- `tick` fires due timers, reads pending documents, runs the queue and delivers the
  outbox, without moving the clock.

`SimState` = `{"now", "timezone", "next_timer_due_at" (null when none), "queue_size",
"loads": [LoadSummary], "langsmith": {"enabled", "project_url"}, "model_ready"}`.

`LoadSummary` = `{"load_id", "broker_id", "broker_name", "stage", "delay_risk",
"open_obligations", "pending_timers", "cost_usd"}`.

`StepResult` = `{"state": SimState, "runs": [Run]}`.

`Run` = the run record plus `cost_usd`, its total model cost: `run_id`, `event_id`,
`load_id`, `started_at`, `finished_at`, `status` (`skipped`, `noop`, `acted`,
`escalated`, `failed`), `gate` (`{"intent", "act", "reason"}`), `gate_engine` (`llm`
or `jev`: what read the message; null when code gave the intent), `gate_scores` (on the
jev engine: `{"model", "intents" (intent to probability), "act" (the probability that the
message needs the agent now), "threshold", "intent_confidence"}`, else null), `intent`,
`sop_section_ids`, `sections_searched`, `sop_version`, `plan` (`{"rationale", "actions"}`:
every action the agent proposed, refused ones included), `violations`, `trajectory`
(`[{"tool", "args", "result", "accepted" (true, false, or null for a read tool),
"cost_usd"}]`), `rejections`, `nodes` (the graph nodes visited, in order), `usage` (one
entry per model call: `role`, `model`, tokens, `cost_usd`, `latency_ms`), `prompts` (role
to template version), `skip_reason`, `error`, `broker_id`, `trace_url`.

## Loads

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/loads` | the assignment's load metadata JSON, plus optional `load_types` | `LoadSummary` (201); 400 with the reason on invalid metadata; 409 when the `load_id` exists |
| GET | `/api/loads/{load_id}` | | `LoadSnapshot` = `{"load", "obligations", "timers", "events", "outbox", "runs"}`; 404 for an unknown load |
| GET | `/api/loads/{load_id}/runs` | | `[Run]`, oldest first |
| GET | `/api/loads/{load_id}/facility-memory` | | one entry per stop, in order: `{sequence, type, name, address, facility_key, facts}`, the client's notes on that place, most recent first; 404 for an unknown load |
| GET | `/api/fixtures` | | `[{"name", "metadata"}]` |

The list of loads is in `SimState.loads`. The fixtures are every `evals/fixtures/*.json`
(`load_481207`: Northline, Client A) plus two variants with their own load ids, so the
three loads can run side by side:

| Name | `load_id` | Changes |
|---|---|---|
| `load_481207_keystone` | `481208` | broker Keystone Freight (Client B) |
| `load_481207_keystone_reefer` | `481209` | Keystone, `load_types: ["reefer"]`, stage `at_pickup`, a frozen commodity |

## Ingress

The contract with the integration layer.

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/v1/events?latency_seconds=60` | `{"message_id"?, "load_id", "channel": "sms" \| "email" \| "slack", "sender_contact_id", "body", "occurred_at"?, "attachments"?}` | `IngestResult` = `{"accepted", "duplicate", "event_id", "reason"}` |
| POST | `/api/v1/pings?latency_seconds=60` | `{"ping_id"?, "load_id", "lat", "lon", "occurred_at"?, "delay_risk": "low" \| "medium" \| "high"}` | `IngestResult` |

- `message_id` and `ping_id` default to a new uuid; `occurred_at` to the simulation's now.
  A message id or ping id seen before is accepted as a `duplicate`, runs nothing and does
  not move the clock.
- An `occurred_at` after now is reached first, firing the timers due on the way. An
  earlier one is kept as the event's time; the clock never goes back.
- Then `latency_seconds` (default 60, 0 to 86,400) of virtual time pass and the API ticks
  once, so the 07:00 ping's SMS leaves at 07:01. A timer due in that window fires on the
  same tick, after the input's run.
- An unknown `load_id` is a 404. A sender who is not a contact of the load is accepted;
  the run escalates it to a Hero without a model call. Only a confirmed change into or out
  of `high` risk runs the agent.

## Attachments and documents

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/loads/{load_id}/attachments` | multipart: `file`, and the message that carries it: `sender_contact_id`, `body`, `channel` (`sms`, `email`, `slack`; default `sms`), `message_id`, `occurred_at` | `{"attachment_id", "document_id", "event_id"}` (201); 400 for an empty file; 409 for a message id already received; 413 over 20 MB |
| GET | `/api/loads/{load_id}/documents` | | `[Document]`, oldest first; 404 for an unknown load |

An upload stores the bytes, ingests a message with that attachment and saves its document
as `pending`; nothing runs yet. The next tick (`POST /api/sim/tick`, or any input) reads
the document before it runs the queue, then runs the message (which does not act on the
document) and the document's `document_processed` event (which does).

`Document` = `{"document_id", "load_id", "attachment_id", "broker_id", "filename",
"content_type", "status": "pending" | "processed" | "needs_review" | "failed", "type_id",
"confidence", "extracted", "checks": [{"field", "expected", "found", "ok"}], "text",
"pages", "received_at", "processed_at"}`.

## Scenarios

| Method | Path | Response |
|---|---|---|
| GET | `/api/scenarios` | `[{"id", "title", "suite", "tags", "description", "steps": [{"index", "at", "note", "kind"}]}]`, every scenario of `evals/scenarios/`, by suite then id |
| POST | `/api/scenarios/{scenario_id}/run` | `{"scenario_id", "load_id"}` (202); 404 unknown scenario; 409 while one is playing; 503 without `OPENROUTER_API_KEY` |
| GET | `/api/scenarios/run` | `ScenarioRun`, the one playing or the last one; `null` before the first |

`run` resets the simulation at the scenario's start, in its timezone, registers its load
and plays its steps in the background, live, with the player the LangSmith experiments
use; the load endpoints show its load and runs as they happen. `ScenarioRun` =
`{"scenario_id", "load_id", "status": "running" | "passed" | "failed" | "error",
"error", "cost_usd", "started_at", "finished_at", "steps": [{"index", "at", "note",
"status": "pending" | "running" | "passed" | "failed", "checks": [{"name", "passed",
"detail"}], "run_ids"}]}`. The checks are the eval graders' (`evalkit/graders.py`), the
judge's included; the last step adds the invariants.

## SOPs and the run graph

| Method | Path | Response |
|---|---|---|
| GET | `/api/sops?client_id=&load_types=a,b` | `{"version", "client_id", "load_types", "sections": [ResolvedSection]}` |
| GET | `/api/sops/preview?client_id=&intent=&stage=&load_types=` | `{"sections", "catalog"}`: what the agent gets for that event |
| GET | `/api/sops/search?q=&broker_id=&load_types=&k=5` | `[{"section_id", "title", "description", "score"}]`, best first (`client_id` is accepted for `broker_id`) |
| GET | `/api/graph` | `{"mermaid", "nodes"}`: the run graph as LangGraph draws it |

`/api/sops` gives the sections after the layers merge, by id; a `ResolvedSection` is
`{"section_id", "title", "description", "intents", "stages", "body", "sources"}`, with
`sources` the files that shaped it, least specific first; `version` is the SOP tree's hash
recorded with every run. `preview` is the selection by intent and stage plus the catalog
of every section the client sees; an intent outside the client's catalog is a 422.
`search` is the agent's `search_sop` on the same index: only the sections the client sees,
ranked by word overlap. All three read the index the runs read, so they show what the agent
sees; a SOP file that cannot be read is a 422 that names the file.

## Event stream

`GET /api/stream`: Server-Sent Events. A new subscriber gets `: connected`, then a `clock`
event with the current `SimState`, then every change, one JSON object per `data:` line:

| Event | Data | When |
|---|---|---|
| `run` | `Run` | every run of a tick |
| `outbox` | the outbox item | an item created, or its delivery status changed |
| `load` | `LoadSummary` | a load whose summary changed |
| `clock` | `SimState` | after every change, last |
| `scenario` | `ScenarioRun` | a scenario started, a step started or finished, the run ended |

A comment (`: heartbeat`) goes out after 15 s of silence. Each subscriber has a bounded
queue of 256 messages and loses its oldest ones when it falls behind, so a client refetches
what it shows on the next `clock` event.

## Errors and CORS

Errors are `{"detail": "..."}`: 400 bad input (invalid metadata, an unknown timezone, the
clock going back), 404 an unknown load or scenario, 409 a load that exists, a message
already received, or a write while a scenario plays, 413 a file over 20 MB, 422 a body or
query that does not validate (FastAPI's `detail` is then a list) or a SOP file that cannot
be read, 503 a scenario without the model's key. An unexpected error is a 500 with
`{"detail": "internal error <id>: the server log has the details", "error_id": "<id>"}`:
no internals reach the client, and the id finds the logged traceback.

CORS allows `http://localhost:5173` and `http://127.0.0.1:5173` (the Vite dev server).
When `web/dist` exists (`pnpm build`, or the Docker image), the same app serves the UI at
`/`, so one port is the whole platform.
