# Platform API

The backend of the simulation platform: drive a load through time, play the driver, the
dispatcher and the broker, act as the Hero team, and inspect every agent run. The code is
in `src/freight_agent/api/`; the React UI in `web/` is built against this page.

```
uv run python -m freight_agent.api            # http://127.0.0.1:8000, --host / --port to change
open http://127.0.0.1:8000/docs               # OpenAPI, generated from the same models
```

Base path `/api`. JSON everywhere. Domain objects are serialized with
`model_dump(mode="json")`: datetimes are ISO 8601 with their offset, in the simulation's
timezone. A request time without an offset is read in that timezone.

The API runs **one simulation in process**: the same `AgentRuntime` as the CLI and the
evals, built by the composition root, on a virtual clock, an in-memory store (whatever
`PERSISTENCE` says, so a reset really starts over) and recording gateways (nothing is
sent anywhere). The model follows `LLM_MODE`, default `replay`: `replay` answers only the
calls recorded in `evals/cassettes/` (a run whose call was never recorded fails and, after
three attempts, becomes a dead letter that `/api/health` reports); `live` needs
`OPENROUTER_API_KEY`; `scripted` has nothing queued interactively, so every model call
escalates to a Hero.

Everything that moves the clock or feeds the agent is serialized by one lock; reads are
not blocked by a run that is thinking.

## Simulation and clock

| Method | Path | Body | Response |
|---|---|---|---|
| POST | /api/sim/reset | `{"start": "2026-10-13T06:55:00-07:00", "llm_mode": "live" \| "replay" \| "scripted" \| "record" \| null, "timezone": "America/Los_Angeles"}`, every field optional, body optional | `SimState` |
| GET | /api/sim/state | | `SimState` |
| POST | /api/sim/advance | `{"minutes": 30}` or `{"to": ISO}`, exactly one (422 otherwise) | `{"state": SimState, "runs": [Run]}` |
| POST | /api/sim/tick | | `{"state": SimState, "runs": [Run]}` |

- A new app starts a simulation at `2026-10-13T06:55` in `America/Los_Angeles`, five
  minutes before the assignment's ping; `reset` with no `start` goes back there.
- `reset` builds the new simulation first: a mode that cannot be built (`live` with no key)
  is a 400 that names the missing variable, and the old simulation stays.
- `advance` moves time the way the eval runner does: it stops at every pending timer due
  on the way, in due order, sets the clock there and ticks (timers have zero latency);
  then it sets the target time and ticks once more. `runs` are every run on the way.
  A target before now is a 400.
- `tick` fires due timers, runs the queue and delivers the outbox, without moving the clock.

`SimState` = `{"now", "timezone", "next_timer_due_at" (null when none), "llm_mode",
"queue_size", "loads": [LoadSummary]}`.

`LoadSummary` = `{"load_id", "broker_id", "broker_name", "stage", "delay_risk",
"open_obligations": int, "pending_timers": int, "cost_usd": float}`.

`Run` = the `RunRecord` (`run_id`, `event_id`, `load_id`, `started_at`, `finished_at`,
`status`, `gate`, `sop_section_ids`, `plan`, `violations`, `usage` with each call's cost,
`skip_reason`, `error`, `trace_url`) plus `cost_usd`, the run's total.

## Loads

| Method | Path | Body | Response |
|---|---|---|---|
| POST | /api/loads | the assignment's load metadata JSON (+ optional `load_types`) | `LoadSummary` (201); 400 with the reason on invalid metadata; 409 when the `load_id` exists |
| GET | /api/loads | | `[LoadSummary]`, by load id |
| GET | /api/loads/{load_id} | | `LoadSnapshot` = `{"load", "obligations", "timers", "events", "outbox", "runs"}`; 404 `{"detail": "unknown load 'x'"}` |
| GET | /api/fixtures | | `[{"name", "metadata"}]` |

Fixtures are every `evals/fixtures/*.json` (`load_481207`, Northline, Client A) plus two
built-in variants of it for the other clients in `docs/decisions.md`:

| Name | `load_id` | Changes |
|---|---|---|
| `load_481207_keystone` | `481208` | `broker` = Keystone Freight (Client B) |
| `load_481207_keystone_reefer` | `481209` | Keystone, `load_types: ["reefer"]`, `stage: at_pickup`, a frozen commodity |

The variants have their own load ids so that the three loads can run side by side.

## Ingress (the contract with the integration layer)

| Method | Path | Body | Response |
|---|---|---|---|
| POST | /api/v1/events?latency_seconds=60 | `InboundMessage` = `{"message_id"?, "load_id", "channel": "sms" \| "email" \| "slack", "sender_contact_id", "body", "occurred_at"?, "attachments"?}` | `IngestResult` = `{"accepted", "duplicate", "event_id", "reason"}` |
| POST | /api/v1/pings?latency_seconds=60 | `LocationPing` = `{"ping_id"?, "load_id", "lat", "lon", "occurred_at"?, "delay_risk": "low" \| "medium" \| "high"}` | `IngestResult` |

- `message_id` and `ping_id` default to a new uuid; `occurred_at` defaults to the
  simulation's now. A message id seen before is accepted as a `duplicate` and runs nothing.
- An `occurred_at` after now is reached first, firing the timers due on the way, as an
  eval step does. An earlier one is kept as the event's time; the clock never goes back.
- Then `latency_seconds` (default 60, 0 to 86400) of virtual time pass and the API ticks
  once, so the 07:00 ping's SMS leaves at 07:01 (the eval timing in `docs/decisions.md`).
  A timer that comes due in that window fires on the same tick, after the input's run.
- An unknown `load_id` is a 404. A sender that is not a contact of the load is accepted:
  the agent escalates it to a Hero without a model call.

## Channels

| Method | Path | Response |
|---|---|---|
| GET | /api/loads/{load_id}/conversation?channel=sms\|email\|slack | merged, time-ordered `[ConversationEntry]`; no `channel`: all three |
| GET | /api/loads/{load_id}/tms | `[{"at", "text"}]`, the agent's TMS notes |

`ConversationEntry` = `{"at", "direction": "in" | "out", "channel", "who", "role",
"text", "subject"?, "status"?, "to"?}`. Inbound entries are the load's `inbound_message`
events on that channel: `who` is the sender's contact id and `role` its role on the load
(`driver`, `dispatcher`, `broker`, or `unknown` for a sender who is not a contact).
Outbound entries are the agent's outbox items of the matching kind (`sms`, `email`,
`broker_slack` for `slack`): `who` and `role` are `"agent"`, `status` is the delivery
status (`pending`, `sent`, `failed`), `to` lists the recipients of an SMS or an email and
`subject` is the email's. Keys with no value are left out. At the same instant an inbound
message comes before the agent's answer.

## Hero console

| Method | Path | Body | Response |
|---|---|---|---|
| GET | /api/hero/tasks?status=open\|done\|all | | `[HeroTask]` across loads, oldest first; default `all` |
| POST | /api/hero/tasks/{task_id}/complete | `{"note": "called the driver, ETA 9:40"}` (optional) | the `HeroTask`; 404 for an unknown task |

`HeroTask` = `{"task_id" (= the outbox id), "load_id", "title", "description", "urgent",
"created_at", "status": "open" | "done", "done_at"?, "note"?}`. The tasks are the agent's
`hero_task` outbox items. Completion is platform state only, kept with the simulation (a
reset clears it); it does not create an agent event yet. `done_at` is the simulation's
now. Completing a done task again returns it unchanged.

## Inspector and SOPs

| Method | Path | Response |
|---|---|---|
| GET | /api/loads/{load_id}/runs | `[Run]`, oldest first |
| GET | /api/sops?client_id=&load_types=a,b | `{"version", "client_id", "load_types", "sections": [ResolvedSection]}` |
| GET | /api/sops/preview?client_id=&load_types=&intent=&stage= | `ResolvedSop` = `{"sections", "catalog"}` |

`/api/sops` gives the effective sections after the layers merge (`domain.sop`
`effective_sections`), by id; each `ResolvedSection` = `{"section_id", "title",
"description", "intents", "stages", "body", "sources"}`, `sources` listing the files that
shaped it, least specific first. `version` is the content hash recorded with every run.
The preview is what the resolver gives the agent for one event (`resolve`): the sections
that apply to the intent and stage, and the catalog of every section the client sees.
`client_id` is the load's `broker_id`. An unknown `intent` or `stage` is a 422; a SOP file
that cannot be read is a 422 that names the file.

## Evals

| Method | Path | Body | Response |
|---|---|---|---|
| GET | /api/evals/scenarios | | `[{"id", "title", "suite", "tags"}]`, by suite then id |
| POST | /api/evals/run | `{"suite": "core", "mode": "scripted" \| "replay", "k": 1, "scenario": null}` | `SuiteSummary` |

A run uses `evalkit` (`ScenarioRunner`, then `grade_step` and `check_invariants` per
trial) in a worker thread with its own event loop, so it never blocks the simulation; it
may take seconds. `k` is 1 to 10 trials per scenario. `scripted` plays each scenario's own
script; `replay` uses the recorded cassettes, and a missing recording fails the trial with
the reason. Live runs are for the CLI. An unknown suite or scenario is a 404.

```json
{
  "suite": "core", "mode": "scripted", "k": 1, "sop_version": "fd4530a85ea2",
  "total": 1, "passed": 1, "failed": 0, "pass_rate": 1.0, "cost_usd": 0.0,
  "scenarios": [{
    "id": "b_ack_with_nothing_open", "title": "B1: ...", "tags": ["regression", "sop:acknowledgements"],
    "trials": 1, "passes": 1,
    "passed": true, "pass_at_k": true, "pass_hat_k": true,
    "cost_usd": 0.0,
    "failures": [{"trial": 1, "step_index": 0, "check": "expect.actions[0]", "message": "..."}]
  }]
}
```

`passed` is pass^k (every trial passed, also given as `pass_hat_k`); `pass_at_k` is at least
one. A failure with `check: "error"` means the trial could not run (a missing cassette).

## Health and stream

| Method | Path | Response |
|---|---|---|
| GET | /api/health | `{"status": "ok" \| "warn" \| "critical", "reasons", "queue", "runs", "cost", "outbox", "timers", "stalled", "settings"}` |
| GET | /api/stream | Server-Sent Events |

Health is `application/metrics.py` over every load of the simulation, judged on the
simulation clock: `reasons` = `[{"check", "level", "message"}]`, most severe first;
`queue` (size, oldest event's age against the 5-minute SLA, dead letters), `runs`
(durations, status mix, validator blocks, model error rates), `cost` (per load against
the $1 budget), `outbox` (pending, failed, stuck), `timers` (firing lag) and `stalled`
(open obligations with no timer). `settings` is `Settings.describe()`: every setting by
variable name, a secret only as `set` or `unset`.

The stream sends `: connected`, then a `clock` event with the current `SimState`, then
every change as it happens, one JSON object per `data:` line:

| Event | Data | When |
|---|---|---|
| `run` | `Run` | each run of a tick (also failed attempts that will be retried) |
| `outbox` | `OutboxItem` | an item created or whose status changed |
| `load` | `LoadSummary` | a load whose summary changed |
| `clock` | `SimState` | after every change, last |
| `hero_task` | `HeroTask` | a task was completed |

A comment (`: heartbeat`) goes out after 15 s of silence. Each subscriber has a bounded
queue (256 messages); a subscriber that falls behind loses its oldest messages, so a client
should refetch what it shows on the next `clock` event.

## Errors and CORS

Errors are `{"detail": "..."}` with a 4xx: 400 bad input (invalid metadata, an unknown
timezone, the clock going back, a mode that cannot be built), 404 unknown load, task,
suite or scenario, 409 a load that exists, 422 a body or query that does not validate
(FastAPI's `detail` is then a list of problems) or a SOP file that cannot be read. An
unexpected error is a 500 with `{"detail": "internal error <id>: the server log has the
details", "error_id": "<id>"}`: no internals reach the client, and the id finds the logged
traceback.

CORS allows `http://localhost:5173` and `http://127.0.0.1:5173` (the Vite dev server).
