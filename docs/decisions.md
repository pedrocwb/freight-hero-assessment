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
| `documents` | standard | `document` | Document handling (stub: create a Hero task to review the document) |

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
| `inbound_message` with attachments and no text | `document` | none |
| `inbound_message` | from the gate model | `gate` role, `max_tool_rounds = 0` (direct structured output, no tools) |
| `hero_update` | `other`, no action | none |

The run continues to decide when `decision.act` is true or the intent is in `MUST_ACT`.
Otherwise the run ends as `noop` with one model call (the gate).

Only a confirmed risk transition into or out of `high` becomes an event. `medium` is
tracked but does not wake the agent. On every confirmed transition the runtime saves the
load with `delay_risk` set to the new band, in the same unit of work as the tracker.

## Run context

`application.context.load_run_context` reads the load's memory once per run. `recent_events`
are the last N events **up to and including the event being processed**: messages queued
behind it are already in the log but are not history for this run (they get their own runs).
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
unit of work. Delivery happens afterwards from the outbox.

Run status: `skipped` (prefilter), `noop` (gate does not act, or the plan is empty, or
every action was dropped), `acted` (at least one approved action), `escalated` (the run
created a Hero task because of a failure path: unknown sender, no SOP section, model
failure or timeout, a high-severity validator violation), `failed` (unexpected error; the
event goes back to the queue and becomes a dead letter after too many attempts).

A model failure (`LlmError`, after fallbacks) or a run over its deadline creates one
urgent Hero task and completes the event, so the event is never lost and never retried
into a duplicate.

## Executor semantics

The executor applies approved actions in plan order, inside the run's unit of work.
`now` is the clock's time when the run executes.

| Action | Effect |
|---|---|
| `send_sms` with `purpose: eta_request` | If no open `eta_request` obligation exists, open one (`attempts = 1`); otherwise `record_attempt`. Outbox `sms`. |
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

An action that would break a domain invariant (for example `schedule_timer` with no open
obligation) is skipped and reported in `ExecutionResult.skipped`; the rest of the plan runs.
The validator normally drops such actions first. Validator rules evaluate each action against
the state left by the actions approved before it in the same plan. Outbound text must not
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

Ids for runs, obligations, timers and outbox items come from `IdGenerator` with the
prefixes `run`, `obl`, `timer`, `out`.

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
