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
| `communication_rules` | standard | all | Tone, only the load's contacts, never share rates or prices, one question per SMS, no promises the agent cannot keep |
| `escalation` | standard | all | When and how to create a Hero task, when it is urgent, what to write in it |
| `delay_risk_high` | standard, `northline`, `keystone` | `delay_risk_high`, `eta_follow_up_due` | The high-risk procedure (see below) |
| `delay_risk_cleared` | standard | `delay_risk_low` | Risk is back to normal: close the open ETA request as cancelled, no messages |
| `eta_received` | standard | `eta_provided` | Close the ETA request as satisfied, write the ETA in the TMS; a vague answer ("soon") is not an ETA |
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
situation") and records the gap. The agent does not improvise.

## Intents and the gate

| Event | Intent | Model call |
|---|---|---|
| `risk_changed` to `high` | `delay_risk_high` | none |
| `risk_changed` from `high` to lower | `delay_risk_low` | none |
| `timer_due` for an `eta_request` obligation | `eta_follow_up_due` | none |
| `attachment_received` | `document` | none |
| `inbound_message` | from the gate model | `gate` role |
| `hero_update` | `other`, no action | none |

The run continues to decide when `decision.act` is true or the intent is in `MUST_ACT`.
Otherwise the run ends as `noop` with one model call (the gate).

Only a confirmed risk transition into or out of `high` becomes an event. `medium` is
tracked but does not wake the agent.

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

Outbox `idempotency_key` = `{event_id}:{index}`, with `index` the action's position in the
approved list. Outbox `payload` keys, used by the dispatcher and by the eval graders:

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
| Timer due | `timer:{timer_id}` |

Ids for runs, obligations, timers and outbox items come from `IdGenerator` with the
prefixes `run`, `obl`, `timer`, `out`.

## Eval timing

A step that injects an event at `at` runs `run_latency_seconds` (default 60) later, so the
first SMS of the main scenario leaves at 07:01 and its timer is due at 07:31. Timers fire
with zero latency: the 07:31 follow-up leaves at 07:31.

## Layer and file ownership

The layer rules in `pyproject.toml` (import-linter) are part of the design and are checked
by `make arch` and by a unit test. The LLM stack (LangChain, OpenRouter) is only imported
in `adapters/llm`. `application` may import LangGraph.
