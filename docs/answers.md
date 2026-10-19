# Answers to the assignment's questions

Each answer starts with the short answer, then the details and the trade-offs. The
diagrams are in `docs/architecture.html`, and the shared contracts are in
`docs/decisions.md`.

**Conventions.** Python paths leave out `src/freight_agent/`: `application/runtime.py`
means `src/freight_agent/application/runtime.py`. Other paths start at the repository
root. Scenario ids name files in `evals/scenarios/core/`. **Not built yet** marks a part
that is designed but not in the code.

| Built on `main` | Not built yet |
|---|---|
| Domain rules (ledger, timers, ping filter, SOP merge, validator), the run graph and `AgentRuntime` with a claim lease, the in-memory and Postgres stores (one contract suite), SOP files and lint, the OpenRouter, cassette, resilient, scripted and per-role models, the composition root, the LangSmith tracer, operational metrics, the eval harness (spec, runner, graders, k-trial suites, reports, negative control), 22 core scenarios, the `agent` CLI, the simulation platform (API and web UI), Docker compose, the live model bake-off | Document extraction, the LLM judge for wording, the always-on worker and scheduler, the per-client canary, a rolling summary of old events, a Telegram SMS channel |

`make eval` runs all 22 core scenarios in scripted mode; `evals/RESULTS.md` has the live
rounds on real models (k = 3) that chose the models in `config/models.yaml`.

## 1. Agent architecture

### 1.1 What is the architecture of your agent? What are the alternatives?

**Short answer.** One stateless run per event, with a per-load FIFO queue. Each run is a
fixed pipeline. Code does every deterministic step, and the model makes two language
judgments: what a message means (the gate) and what the SOP asks for now (decide). The
model only proposes a plan; code validates it and executes it through an outbox. Memory
is in our storage, not in the model or the process.

| Step | Code | Model call | Exits |
|---|---|---|---|
| load context | `application/context.py` | none | |
| prefilter | `application/prefilter.py` | none | `skipped`, or `escalated` (unknown sender) |
| gate | `application/gate.py` | small model, inbound messages only | `noop` |
| resolve SOP | `domain/sop.py` | none | `escalated` (no SOP section) |
| decide | `application/decide.py`, tools in `application/tools.py` | one structured call, at most 2 read-only tool rounds | |
| validate | `domain/validation.py`, `domain/rules.py` | none | `noop`, or `escalated` (high-severity violation) |
| commit and execute | `application/executor.py` | none | `acted` |

`AgentRuntime.process_next` (`application/runtime.py`) claims the event and reads its
context in one short transaction, runs the LangGraph graph (`application/graph/build.py`)
outside any transaction under the run deadline, then applies the approved actions and
writes ledger, timers, outbox and run record in a second transaction. Delivery happens
later from the outbox, with the idempotency key `{event_id}:{index}`.

- **The model cannot act.** Tools are read-only, and every action passes the nine rules
  in `domain/rules.py` (recipient is a contact, channel allowed, SMS rate limit, ETA
  attempt cap, no phone or email in outbound text, ...). A rule drops an action, never
  edits it. In `e1_prompt_injection` an email asks for the rate confirmation at an
  outside address; even a plan that obeys it cannot reach that address.
- **No event is lost.** A model failure after fallbacks, a run past its deadline, or an
  event no SOP section covers creates a Hero task and completes the event
  (`application/escalation.py`).
- **Deterministic where possible.** The same event and state give the same sections and
  the same prompt text, which makes replay possible.

| Alternative | Why not |
|---|---|
| One ReAct agent with action tools | The model acts directly: an injected email can trigger a send, and calls per event have no limit. |
| The full SOP and history in every prompt | A 100-page SOP is about 50,000 to 70,000 tokens; on about 80 events per load it breaks the $1 budget. |
| SOPs compiled into a state machine | Every SOP change needs an engineer. We keep a state machine only for obligations. |
| Several agents (per stage or role, or a supervisor) | Each handoff adds a model call and loses context; the stage is already an SOP key. |
| A durable workflow per load (Temporal) | A way to deploy this pipeline (5.2), not another agent design. |

### 1.2 How does the agent read documents, find their type and get their key values?

**Short answer.** Today it does not read document content. An attachment becomes a
`document` event, and the `documents` SOP section creates a Hero task to review it. The
design is a pipeline before the agent: a vision model finds the type and fills a schema
for that type, code checks the values against the load metadata, and a Hero reviews
every uncertain result.

Built: a message with attachments and no text gets the `document` intent from code, with
no gate call (`system_decision` in `application/gate.py`). `sops/standard/documents.md`
creates a Hero task and never forwards a rate confirmation. `read_document`
(`application/tools.py`) is a stub: it returns the file name, content type and storage
reference, and tells the model to create a Hero task.

**Not built yet: the document pipeline.**

1. **Normalize:** fetch the file by reference (S3), turn PDF pages into images.
2. **Classify:** one vision call with a forced structured output,
   `{type: bol | pod | rate_confirmation | receipt | other, confidence}`.
3. **Extract** with one schema per type, as a forced tool call like the gate and decide:

   | Type | Key values |
   |---|---|
   | BOL | BOL number, shipper, consignee, PO numbers, seal number, weight, pickup date, signatures |
   | POD | receiver, signature, delivery time, exceptions (shortage, damage, refused) |
   | Rate confirmation | carrier and MC number, rate and line items, stops and appointments |
   | Receipt | kind (lumper, scale, fuel), amount, date, vendor |

4. **Check in code** against the load metadata: PO (55821 on load 481207), MC number,
   stops, dates inside the appointment windows, the seal number the driver reported, a
   signature on a POD.
5. **Hero review** when a confidence is low, a value does not match or a required field
   is missing, with the values filled in. Corrections become the labelled document eval.
6. A clean result becomes a new "document processed" event, and the agent follows the SOP
   (a POD moves the stage to `delivered`).

OCR (AWS Textract) plus a text model would be cheaper on digital PDFs but weaker on
phone photos.

## 2. Instructions

### 2.1 How do you organize the SOPs of many clients?

**Short answer.** Three layers of Markdown files: `sops/standard/`,
`sops/clients/<client_id>/` and `sops/clients/<client_id>/types/<load_type>/`. Each file
is one section with a stable `id`. A client folder holds only the sections the client
changes, with `mode: replace` or `mode: extend`. Everything else comes from the
standard, so a new client needs no folder.

`effective_sections` in `domain/sop.py` merges the layers per `id`, from general to
specific:

- **`replace`** swaps body and header. `sops/clients/northline/delay_risk_high.md`
  (Client A) and `sops/clients/keystone/delay_risk_high.md` (Client B) replace the
  standard high-risk procedure.
- **`extend`** appends its body and keeps the header.
  `sops/clients/keystone/types/reefer/driver_departed.md` adds "ask for the trailer
  temperature and the seal number" for Keystone reefer loads only
  (`d2_keystone_reefer_departure`).

The client id is the load's `broker.id`; load types come from a `load_types` field we add
to the metadata. A run reads only the standard layer and its own client's folders, so one
client's edit cannot change another client's loads
(`test_another_clients_sections_never_change_the_result` in
`tests/property/test_sop_properties.py`). The 60 to 70% that clients share is written
once; a full copy per client would drift from the standard.

### 2.2 How does the agent find the correct SOP files for the current event? What are the advantages and disadvantages of your method?

**Short answer.** By rules, not by search. The event's intent (from code for system
events, from the gate for messages), the load's stage, the client and the load types
select the sections whose `intents` and `stages` match. The model gets them in full, plus
the procedures still in progress as context, plus a one-line catalog of the rest that it
can read with a tool.

Steps (`resolve_sop_node` in `application/graph/nodes.py`, `resolve` in `domain/sop.py`):

1. Merge the client's layers; keep the sections whose `intents` include the intent and
   whose `stages` include the stage (empty means all), so `communication_rules` and
   `escalation` always apply.
2. If no section with an explicit intent matches, escalate ("No SOP for this situation",
   `sop_gap` in `application/escalation.py`).
3. Add, as context, the sections that own the follow-ups of open obligations
   (`sections_in_progress` in `application/decide.py`).
4. List the other sections in one line each; `read_sop_section` returns one in full.

The run record keeps `sop_section_ids`, so each decision shows its sections.

| Advantages | Disadvantages |
|---|---|
| Deterministic: the same event and state select the same sections, so a scenario can assert the procedure. | A misclassified message gets the wrong sections. Mitigated: system events skip the model, `MUST_ACT` intents (`domain/intents.py`) always reach decide, the catalog and the tool. |
| Small prompts (about three sections, not 100 pages): low cost and latency, and no unrelated rules. | The intent list is in code: a new kind of situation needs an engineer. |
| No leaks between clients; a gap is a visible escalation, not improvisation. | Editors must tag sections from a fixed list of intents and stages (the lint checks them). |
| | As SOPs grow, one intent can collect many sections; then sub-intents or a second selection step. |

We rejected vector search as the main method: a missed section gives silently wrong
behavior, and a test cannot assert a similarity threshold. It stays an option for events
with no tagged section (**Not built yet**).

### 2.3 People without a technical background write and change the SOPs. How does your design make this safe?

**Short answer.** Editors write plain Markdown, a lint explains every problem in plain
English, client changes go into layers that never edit the standard, every change runs
the whole eval suite before a reviewer approves it, a content hash identifies the text
each run followed, and a change reaches a few loads of one client first. The code
guardrails hold whatever the text says.

| Safeguard | State |
|---|---|
| Plain Markdown with six header settings; `sops/README.md` is the editor's guide. | Built |
| Lint (`adapters/sop/lint.py`): bad header, unknown setting, intent or stage, a duplicate id, an `extend` with nothing to extend, and a warning for a section no scenario covers. A real message: "the setting \`intens\` is not known (did you mean \`intents\`?). Use only these settings: id, title, description, intents, stages, mode." `tests/unit/test_sop_seed.py` fails the build on a lint error. | Built: `uv run agent sop lint`, also a CI step |
| Layers: a client change cannot touch the standard or another client. | Built |
| Version: `FileSopRepository.version()` is a 12-character sha256 of every file; every run record stores the version it read (`RunRecord.sop_version`) and the LangSmith trace carries it. | Built; pinning it per obligation (`Obligation.sop_version` exists) is **not built yet** |
| Required evals: the whole suite, all clients, live mode (4.2): `uv run agent eval run --mode live --k 3`. | Built as a command; making it a required check before merge is a repository setting |
| Review: a person who knows the client reads the change and the eval results. A standard-layer change reaches every client, so it needs a second approver. | Designed; today a pull request |
| Canary per client: the new version serves a share of that client's new loads (by a hash of `load_id`); its escalation, no-op and block rates are compared with the old version; rollback points back. | **Not built yet** |
| Guardrails in code (`domain/rules.py`): at most 2 ETA requests, an SMS rate limit, only the load's contacts on their channels, no contact details in outbound text. | Built |

Today the SOPs are files in git. A future editor UI would write the same files, preview
which sections apply for a client, event and stage (`resolve` is a pure function, so the
preview is the production logic), and show the change in behavior from the eval run.

## 3. Memory

### 3.1 How does the agent know its past actions and the past messages of the load?

**Short answer.** It reads them from storage at the start of every run. Every message,
risk change, timer event and agent action is an entry in the load's append-only event
log; open obligations and timers are rows in a ledger. `load_run_context`
(`application/context.py`) reads them into one snapshot that every step uses.

| Memory | Store | In the decide prompt |
|---|---|---|
| Episodic | Event log: messages, risk changes, timer events, the agent's actions | Transcript of the last 30 events (`recent_events_limit`), "in" and "out" lines |
| Prospective | Ledger: obligations (status, attempts, deferral, active timer) and timers | "Ledger" and "Pending timers" |
| Procedural | SOP files | The selected sections (2.2) |
| Reference | Load metadata | "Load" and "Contacts" |

- **Actions are memory.** For each outbox item the executor appends an `agent_action`
  event (`act:{event_id}:{index}`) in the same transaction (`application/executor.py`),
  so the next run sees what was sent, to whom and why.
- **History stops at the current event.** Messages queued behind it are left out
  (`_history_until`), so a run never acts on an ETA that has not arrived for it.
- **The ledger holds only facts** (`domain/obligations.py`), never a pointer into the
  SOP text, and the process keeps nothing between runs.

The Postgres adapter (`adapters/persistence/postgres/`) and the in-memory store pass the
same contract suite (`tests/contract/`), and the 22 core scenarios leave the same state on
both at every step (`tests/integration/test_postgres_scenarios.py`). **Not built yet:** a
rolling summary of events older than the last 30.

### 3.2 At 07:13, how does the agent remember that the ETA request is still open?

**Short answer.** The model does not have to remember it: the ledger records it. The
07:01 run opened an `eta_request` obligation with 1 attempt and a timer due at 07:31. The
driver's question does not change that row, and the 07:31 timer will ask again unless a
run closes the request. The prompt shows the open request, so the reply neither asks
again nor closes it.

At 07:13 (`a_client_a_high_risk`, step 1) the ledger holds `eta_request` open, attempts
1, last action 07:01, no deferral, active timer due 07:31. The event log holds the 07:00
risk change, our 07:01 SMS with purpose `eta_request`, and the 07:12 "What's the PO
number?". The gate sees the open obligation and the recent events and returns
`question`. The decide prompt contains (shortened):

```
## SOP sections for this event (follow them exactly)
### communication_rules ...  ### driver_questions ...  ### escalation ...

## Procedure in progress (context)
An open obligation in the ledger follows this procedure. Its next step runs when its
follow-up timer fires. Do not run its steps for this event unless the sections above
tell you to.
### delay_risk_high: Delay risk changes to high (Northline) ...

## Ledger (the source of truth for obligations and attempts)
Open:
- eta_request: attempts 1; opened 07:01; last action 07:01 (12 min ago); deferred until
  none, deferrals used 0; follow-up timer due 07:31 (in 18 min)

## Transcript (recent events, oldest first; "out" lines are what you did)
- 07:00 system: delay risk changed low -> high
- 07:01 out sms to drv_20931 (driver), purpose eta_request: "Hi, this is Northline ..."
- 07:12 in sms from drv_20931 (driver): "What's the PO number?"  <- this event
```

The sections for a `question` do not say how the ETA request continues, so the "procedure
in progress" block shows the Northline high-risk section as context only.
`driver_questions` adds: "If an ETA request is open, leave it and its follow-up as they
are". The PO comes from the load's `instructions`.

The plan is one SMS with purpose `reply`. The executor counts only `eta_request` SMS as
attempts, so the ledger and the timer do not change. The scenario forbids an
`eta_request` SMS in this step and asserts attempts 1 and one timer due at 07:31.
`a_question_during_last_wait` checks the same after the second request, and
`b_ack_after_eta_request` checks that "ok" changes nothing.

### 3.3 How does a follow-up timer work? What does the agent see when the timer starts it?

**Short answer.** A timer is a row tied to an obligation. When due, it becomes a
`timer_due` event in the load's queue, like a message. Code first checks that it is still
the active timer of an open obligation; a stale timer ends its run with no model call.
Otherwise decide sees the timer event, the ledger with the attempt count, the transcript
and the SOP section, and does the next step.

1. **Set.** A plan proposes `schedule_timer {kind: eta_request, due_in_minutes: 30}`. The
   executor computes `due_at` in real minutes (`domain/timers.py`, safe across DST),
   never before a promised time plus 2 minutes of grace. Older pending timers of the
   obligation become `superseded`.
2. **Fire.** `AgentRuntime.fire_due_timers` turns each due timer into the event
   `timer:{timer_id}` (firing time, `data.due_at`, obligation kind), marks it `fired` and
   queues it behind the load's earlier events, in one transaction.
3. **Stale check** (prefilter, `is_stale`): superseded, cancelled, obligation closed, or
   no longer the active timer: `skipped` with no model call.
4. **Intent from code:** `eta_follow_up_due`, no gate call; `delay_risk_high` lists it.
5. **Decide.** At 07:31 the model sees the event line
   `timer: follow-up for eta_request, due 07:31 (now)`, the ledger line
   `eta_request: attempts 1; ... firing now (this event)`, the transcript with the 07:12
   question and the 07:13 reply, and the Northline section.
   Attempts 1 means the second step: SMS again and a new 30-minute timer. At 08:01,
   attempts 2: email the dispatcher, create an urgent Hero task, close the request as
   `exhausted`. Evals fire timers with zero latency, so these happen at 08:01, not 08:02.

Closing an obligation cancels its timers (`b_eta_received_cancels_follow_up`). "I'll tell
you in 5 minutes" at 07:30 replaces the 07:31 timer with one at 07:37, and the old
timer's event is skipped as stale (`g1_deferral_stale_timer`).

**Not built yet:** the always-on scheduler process. In this repository `tick()` fires
timers (the platform and the eval runner call it on a `VirtualClock`); the Postgres
adapter already claims due timers with `SKIP LOCKED`, so several workers can run it.
Production would call it on a schedule, or use EventBridge Scheduler or Temporal timers.

## 4. Evals

### 4.1 How do you make sure that the system works correctly in production?

**Short answer.** Three layers: tests and scenario evals before a release; code guards
at run time, so a wrong model answer cannot do damage and every failure reaches a
person; and monitoring of our own data after the release.

**Before a release.** Unit, contract and property tests (`make test`), layer rules
(`make arch`) and strict pyright. Then scenario evals: a YAML scenario
(`evalkit/spec.py`) drives the product's `AgentRuntime` on a virtual clock with recording
gateways (`evalkit/runner.py`). The graders (`evalkit/graders.py`) check facts, not
wording: action kind, recipient, purpose, counts, `forbid` lists, ledger, timers, run
status, model calls; and invariants such as unique idempotency keys and no recipient
outside the load's contacts.

| Mode | Model | Use |
|---|---|---|
| scripted | `ScriptedModel` returns answers written in the scenario | Tests the pipeline and graders, deterministically; not the model |
| replay | `CassetteModel` (`adapters/llm/cassette.py`), keyed by a sha256 of model id, role, prompts, tools, schema and tool-round limit | Every pull request, offline. A changed prompt, SOP or model misses its recording and fails with `CassetteMissing`; a stale answer is never replayed. |
| live | `OpenRouterModel` behind `ResilientModel` | Releases and model changes: k = 3 trials, and a scenario passes only if all three pass (pass^3), because a customer sees every run |

**Negative control.** A grader that never fails proves nothing: each grader check has a
case where it must fail (`tests/evalkit/test_graders.py`), the runner fails a step with an
unused or missing script entry, and `tests/evalkit/test_negative_control.py` runs the real
core suite with deliberately wrong agents (an SMS for "ok", a third ETA request, an email
to Client B's dispatcher, an ignored deferral) and asserts exactly which scenarios fail.

**At run time:** the validator, `MUST_ACT` intents the gate cannot skip, the prefilter,
escalation paths, and idempotent delivery.

**After the release:** `application/metrics.py` computes, from our own data, queue age
against the SLA, run p95, the status mix, validator blocks, errors per model, cost per
load, timer lag, stalled obligations and dead letters, and `health_summary` gives OK,
WARN or CRITICAL. A LangSmith trace per run (`adapters/observability/langsmith.py`)
fails open.

`uv run agent eval run` runs k trials per scenario, reports pass@k and pass^k per
scenario and tag, stops at a cost cap and writes a report with the git sha, SOP version,
models, prompt hash and a comparison with a baseline (`evalkit/suite.py`,
`evalkit/report.py`). **Not built yet:** the LLM judge for wording, replay of logged
loads, and turning Hero corrections into scenarios.

### 4.2 A client changes its procedure. How do you make sure that other behavior does not break?

**Short answer.** The change lives in that client's folder, so it cannot change another
client's sections. Every section has at least one scenario. The whole suite, for every
client, runs live before the merge.

1. **Layers.** A Northline edit is a file under `sops/clients/northline/`, and
   `effective_sections` never reads another client's folder. A change to
   `sops/standard/` reaches every client, which is why the whole suite runs.
2. **A scenario per section.** Scenarios carry `sop:<section_id>` tags and the lint warns
   about an uncovered section. Each of the 11 sections is tagged by at least one of the
   22 core scenarios.
3. **Replay sees the change.** A changed SOP text changes the decide prompt, so the
   affected cassettes miss; replay cannot pass without a new live recording.
4. **Compare.** Regression scenarios must stay at pass^3 = 100%; capability scenarios are
   compared with the baseline. The SOP hash ties each result to its text.
5. **Canary** for that client (2.3).

`tests/unit/test_sop_seed.py` also pins the shared contract: the sections per layer,
their intents and each client's high-risk rules.

### 4.3 You change the LLM. How do you make sure that the system does not break?

**Short answer.** A model is one line in `config/models.yaml`. Cassettes are keyed by
model id, so a new model must be recorded with a live run of the whole suite. A switch is
a bake-off: each candidate runs the live suite with k = 3 against thresholds fixed
before the round, reported as a pass^k matrix per model and scenario group.

- **Per-role config** (gate, decide, judge, driver_sim), with fallbacks from another
  provider family, a pinned provider for reproducible evals, and `require_parameters`
  (only endpoints that honour `tool_choice`).
- **Bake-off:** five cheap candidates in `config/models.yaml`. Thresholds fixed before
  the round: groups A and B at pass^3 = 100%, no `MUST_ACT` case skipped by the gate,
  cost per load within budget. The cheapest model that meets them wins.
- **Safety that does not depend on the model:** structured output is a forced tool call
  that pydantic validates, so a malformed answer is an `LlmError` (fallback, then a Hero
  task), never a half-read plan. The validator holds for any model.
- **Canary** on a share of loads, compared on the metrics of 4.1.

**Not built yet:** the bake-off has not run; today's models (GPT-4.1 Nano for the gate,
Gemini 2.5 Flash Lite for decide) were chosen from the catalog for tools, structured
output and price. The per-role router is not built either: the runtime takes one
`LanguageModel`, and each request names its role.

### 4.4 Clients have different procedures and different complexity. How do you organize the evals?

**Short answer.** Scenarios are YAML files, grouped by suite and tagged by purpose
(regression or capability) and by the SOP sections they cover. A client variant reuses
one load fixture with `load_overrides`, so a client with a more complex SOP gets more
scenarios, not a new harness.

| Group | Scenarios | Checks |
|---|---|---|
| A | `a_client_a_high_risk`, `a_question_during_last_wait` | The assignment's sequence |
| B | `b_ack_with_nothing_open`, `b_ack_after_eta_request`, `b_broker_thanks_by_email`, `b_medium_risk_does_not_wake`, `b_eta_received_cancels_follow_up` | Doing nothing |
| C | `c1_eta_from_driver`, `c2_eta_from_dispatcher`, `c3_relative_eta`, `c3_vague_eta`, `c4_risk_cleared` | ETA answers |
| D | `d1_keystone_high_risk`, `d2_keystone_reefer_departure`, `d4_lumper_no_sop` | Client B, reefer, no SOP answer |
| E | `e1_prompt_injection`, `e3_duplicate_message` | Injection, duplicates |
| G | `g1_deferral_stale_timer` to `g5_ping_during_deferral` | Deferrals |

- **Regression or capability.** Regression scenarios (20 of 22) must stay at 100%.
  Capability ones (`c3_relative_eta`, `d4_lumper_no_sop`) measure what we are improving
  and move to regression once they pass pass^3 in consecutive rounds.
- **When each mode runs:** every pull request in replay, an SOP change live, a model
  change as a bake-off.
- **Not built yet:** a folder per client next to a standard suite that runs once per
  client configuration, so a new client starts with the standard suite.

## 5. Scale, response time and cost

### 5.1 How does your design keep each agent run at 5 minutes or less?

**Short answer.** Each run is bounded by construction: a per-load FIFO with stateless
workers in parallel, at most three model calls under timeouts inside a 240-second run
deadline, and escalation to a Hero instead of endless retries. Queue age is measured
against the 5-minute SLA from the time the event happened.

| Mechanism | Where |
|---|---|
| Per-load FIFO: one event per load in flight, loads in parallel; stateless workers scale horizontally | `EventQueue` in `ports/repositories.py` |
| No transaction open while a model thinks | `application/runtime.py` |
| No model call for pings, stale timers and duplicates; the gate only for messages; decide makes at most 3 calls | `application/gate.py`, `RuntimeConfig.decide_max_tool_rounds` |
| 60 s per model call (the whole tool conversation), 240 s per run | `RuntimeConfig.llm_timeout_s`, `RuntimeConfig.run_deadline_s` |
| Fallback chain and circuit breaker: a failing model is skipped, not waited on | `adapters/llm/resilient.py` |
| Model failure or deadline: one urgent Hero task, the event completes; other errors retry, then dead-letter | `application/escalation.py`, `AgentRuntime.process_next` |
| Age of the oldest queued event against 5 minutes, run p95, timer lag | `queue_metrics`, `run_metrics`, `timer_lag_metrics` in `application/metrics.py` |

The 240-second deadline leaves 60 seconds for the queue wait and delivery. The step
ceilings in `docs/architecture.html` ("Budgets") add up to about 76 seconds; they are
targets until `Usage.latency_ms` gives measured values from live runs.

A claim lease (`EventQueue.reclaim_expired`, `RuntimeConfig.claim_lease_s`) returns an
event whose worker died mid-run to the queue, counted as a failed attempt.
**Not built yet:** the worker process with autoscaling on queue age, an always-on
delivery dispatcher (today `tick()` calls `dispatch_outbox`), and per-client concurrency
limits.

### 5.2 The volume increases ten times. What must change?

**Short answer.** The logic, the prompts and the cost per load stay the same; the
adapters behind the ports change. Today, 100,000 loads with 50 to 100 messages each is
about 7.5 million messages a month, about 10 per second in US business hours (22 days of
10 hours). At ten times: about 95 per second, plus the pings.

| Part | Today | At 10× |
|---|---|---|
| Queue | Per-load FIFO in memory or Postgres (`SKIP LOCKED` claims, one in flight per load) | SQS FIFO with `MessageGroupId = load_id` (order per load, parallel across loads), or Temporal with one workflow per load; workers scale on queue age |
| Timers | Rows fired by `tick()` | EventBridge Scheduler or Temporal timers, or polling with `FOR UPDATE SKIP LOCKED` |
| Pings | One transaction per ping; redeliveries caught by the ping ids kept with the tracker (`RiskTracker.recent_ping_ids`) | A stream partitioned by load keeps the tracker and last ping id; only transitions reach Postgres |
| Postgres | Built (`adapters/persistence/postgres/`), works through Neon's pooler | Neon or RDS. The events table partitioned by month on `occurred_at`; closed loads archived to S3; a pooler; read replicas for the platform and metrics |
| SOPs | Read from disk on every call (`adapters/sop/files.py`) | Each published version compiled once, cached by hash |
| Models | About 5 million tokens per minute in business hours | About 50 million: higher rate limits or provisioned throughput, more provider families in the fallback chain, per-client limits, priority for risk and timer events |
| Tracing | One trace per run, a bounded pool | Sampling (all escalations and failures, a share of the rest), batched export or self-hosting |
| Cost | About $0.07 per load | The same per load; total spend grows linearly |

The layer rules (import-linter contracts in `pyproject.toml`) keep storage, queues and
providers behind ports, so the pipeline, the ledger and the eval suite do not change.

### 5.3 How do you keep the LLM cost at approximately $1 or less for each load?

**Short answer.** With the measured prompt sizes and the prices in `config/models.yaml`,
a typical load costs about $0.07 and a heavy one about $0.13, planning for a 100-page SOP.
The levers keep it there: most events never reach a model, the gate keeps chatter away
from the expensive call, prompts stay small and cache well, and a meter watches each
load.

**Measured** (characters / 4) over the 67 requests of the 22 core scenarios, rendered by
`application/prompts.py` with the seed SOPs:

| Call | System | User | Tool schemas | Input tokens | Output tokens |
|---|---|---|---|---|---|
| Gate (22) | 2,679 chars | 319 to 771 chars | 535 chars | 945 median, 1,000 max | about 25 |
| Decide (45) | 6,391 chars | 6,455 to 8,590 chars | 5,220 chars | 4,720 median, 5,050 max | 85 median, 275 max |

**Planning figures.** Gate: 1,000 in, 50 out. Decide: 11,000 in, 200 out: the measured
4,700 plus about 950 for a full 30-event transcript, 3,300 for a catalog of about 120
sections (100 pages), 750 for section ids in the tool schema and 1,000 for longer
sections. A tool round resends the prompt, so 1.25 calls per decide run.

**Prices** per million input / output tokens: gate `openai/gpt-4.1-nano` $0.10 / $0.40;
decide `google/gemini-2.5-flash-lite` $0.10 / $0.40; decide fallback
`openai/gpt-4.1-mini` $0.40 / $1.60.

- Gate: 1,000 × $0.10/M + 50 × $0.40/M = **$0.00012** per call.
- Decide: (11,000 × $0.10 + 200 × $0.40)/M = $0.00118 per call, × 1.25 = **$0.00148**
  per run. On the fallback: $0.00472 × 1.25 = $0.0059 per run.

| Per load | Typical | Heavy |
|---|---|---|
| Inbound messages (one gate call each) | 75 | 100 |
| Messages that reach decide (40% / 60%) | 30 | 60 |
| Risk changes into or out of high, timers, attachments with no text (decide only) | 4 + 4 + 4 | 6 + 6 + 5 |
| Location pings | hundreds, $0 | hundreds, $0 |
| Gate | 75 × $0.00012 = $0.009 | 100 × $0.00012 = $0.012 |
| Decide | 42 × $0.00148 = $0.062 | 77 × $0.00148 = $0.114 |
| **Total against $1** | **about $0.07** | **about $0.13** |
| Every decide run on the fallback | about $0.26 | about $0.47 |
| Document extraction, when built (3,000 in, 300 out on GPT-4.1 Mini: about $0.002 each) | + $0.01 | + $0.01 |

The margin comes from cheap models: with a decide model at $3 / $12 per million and no
caching, the typical load would cost 42 × (11,000 × $3 + 200 × $12)/M × 1.25 + $0.009 =
about $1.87.

**Levers:**

1. **Pings never reach a model:** `domain/ping_filter.py` turns them into rare
   transitions (`b_medium_risk_does_not_wake`).
2. **The prefilter costs no model call:** duplicates, stale timers, empty messages and
   unknown senders stop in code; the stale timer in `g1_deferral_stale_timer` makes zero
   calls. Risk changes, timers and bare attachments also skip the gate.
3. **The gate skips noise:** an "ok" ends as `noop` after a 1,000-token call and never
   pays for an 11,000-token decide, while `MUST_ACT` keeps required intents.
4. **Small prompts:** only the matching sections go in full.
5. **Prompt caching of the stable prefix:** stable parts come first (schemas, system
   prompt, load, contacts: about 3,000 tokens that repeat on every decide run of a load).
   OpenAI and Gemini models cache long prefixes automatically, and a cache read costs 25%
   (Nano) or 10% (Flash Lite) of the input price. The adapter records
   `Usage.cached_tokens`, priced at `cached_input` (`adapters/llm/models_config.py`).
   Next step: move stage and delay risk next to the event, so a stage change keeps the
   cache.
6. **A cheap model per role,** chosen by the bake-off (4.3).
7. **A cost cap per load:** `cost_metrics` (`application/metrics.py`) reports each load's
   spend against $1 and warns at 80%; `Budget` in `evalkit/graders.py` fails a scenario
   over its cost or tokens. **Not built yet:** enforcement in the runtime (at 80%, no
   model above the cheap tier; at 100%, judgment calls go to a Hero).
