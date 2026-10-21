# Answers to the assignment's questions

Each answer starts with the short answer, then the details and the trade-offs. Diagrams
are in [`architecture.md`](architecture.md), the contracts in
[`decisions.md`](decisions.md), the measurements in
[`../evals/RESULTS.md`](../evals/RESULTS.md).

Python paths leave out `src/freight_agent/`: `application/runtime.py` is
`src/freight_agent/application/runtime.py`. Scenario ids name files in
`evals/scenarios/<suite>/`. **Designed, not built** marks a part that is described but
not in the code.

| Built | Designed, not built |
|---|---|
| The runtime (per-load FIFO, claim/think/commit, outbox, claim lease), the LangGraph run graph with the bounded tool loop, the rules, the ledger and timers, the document pipeline, the SOP layers and client catalogs with their lint, the prompt templates versioned in the LangSmith Prompt Hub, the eval harness as LangSmith experiments, the platform (FastAPI and React), LangGraph Studio, the Docker image, Postgres for the load memory (`PERSISTENCE=postgres`, run by Docker Compose; the same contract suites as the memory store) | An always-on worker and timer scheduler, real integrations (the gateways record what they would send), an SOP editor with review and approval, SOP versioning with canary and rollback, hybrid search over SOPs, operational metrics and alerts, the per-load cost cap, a rolling summary of old events |

An earlier build of several designed parts (pgvector search over SOPs, SOP onboarding
from a PDF with review and publish, client config versions with a canary, metrics) is at
the git tag `full-platform`.

## 1. Agent architecture

### 1.1 What is the architecture of your agent? What are the alternatives?

**Short answer.** One stateless run per event, with a FIFO queue per load. Code does every
deterministic step: it reads the load's memory, drops what needs no thought, gives system
events their intent, selects the SOP sections and checks every action. Each inbound
message gets one call to a decision model that answers, with probabilities, its intent and
whether it needs the agent now; code decides from them, and a doubtful message goes on to
the agent. The agent model works inside one bounded tool loop: it reads what it needs (SOP, history, ledger, load,
documents) and stages actions one at a time. Each staged action is checked by the rules at
the call and answered "accepted" or "rejected by <rule>", with what an acceptable action
looks like; nothing runs until the run commits, and then only through an outbox. Memory
lives in storage, never in the process.

The run graph (`application/graph/build.py`, diagram in
[architecture.md section 4](architecture.md#4-the-run-graph)):

| Node | Code | Model call | Ends the run as |
|---|---|---|---|
| prefilter | `application/prefilter.py` | none | `skipped` (duplicate, stale timer, empty message); `escalated` (unknown sender) |
| gate | `application/gate_decision.py` (`application/gate.py` on `GATE_ENGINE=llm`) | messages only: one call to TypeSafe's Jev 1.14, a decision model that answers the intent and the probability of "needs the agent now" (GPT-4.1 Nano writes the decision on `llm`); system events get their intent from code | `noop` (an intent that may end a run, such as an acknowledgement, with that probability under 0.2) |
| knowledge | `application/graph/nodes.py`, `SopIndex.by_applies` | none | never |
| agent, tools | `application/agent.py`, `application/agent_tools.py` | one call per round, gpt-oss-120b, at most `max_steps` rounds | goes to validate |
| validate | `domain/validation.py`, `domain/rules.py` | none | `acted`, `noop`, `escalated` (blocked action, rejection budget, no procedure) |

Around the graph, `AgentRuntime.process_next` (`application/runtime.py`) runs three phases:
a short transaction claims the event and loads its context; the graph thinks outside any
transaction under a 240-second deadline; a second transaction executes the approved
actions (ledger, timers, stage, outbox items), writes the run record and completes the
event. Delivery happens afterwards from the outbox, with the idempotency key
`{event_id}:{index}`.

The agent's tools:

| Kind | Tools | Effect |
|---|---|---|
| Read | `search_sop`, `read_sop_section`, `get_history`, `get_ledger`, `get_load`, `read_document`, `list_document_requirements` | Text from the run's context and the SOP index. Message and document text comes back as quoted, untrusted data. |
| Action | `send_sms`, `send_email`, `post_broker_slack`, `write_tms_note`, `create_hero_task`, `post_internal_slack`, `update_stage`, `schedule_timer`, `defer_obligation`, `close_obligation`, `open_obligation` | Build the action and check it with `check_action` against the actions accepted so far. Answer `accepted as action #n` or `rejected by <rule>: <why>` (what is wrong, then how to correct it: "its requests are used up. No further message may carry requests: eta_request ..."). Execute nothing. |
| Answer | `finish(rationale, intent, no_procedure)` | Ends the loop. |

**Guardrails that hold whatever the model does:**

- **The model cannot act.** Action tools only stage; the executor applies the approved
  actions in the commit. The rules (`domain/rules.py`): the recipient is a contact of the
  load, on a channel that contact has; at most 3 SMS per contact per hour; the attempt cap
  of each obligation kind; obligation kinds from the client's catalog; one deferral of at
  most 15 minutes; the stage only moves forward; no duplicate action; no phone number or
  email address in outbound text. A rule refuses an action, never edits it; its refusal
  says what is wrong and what an acceptable action looks like, so the next round can
  correct it.
  `e1_prompt_injection` checks that an email asking for the rate confirmation at an
  outside address gets nothing sent there.
- **Budgets per client** (`BrokerLimits`): 8 rounds (`max_steps`), $0.05 of model spend per
  run (`max_cost_usd`); once either is spent the next round must call `finish`. More than 3
  refused action calls (`max_rejections`) stop the run and hand it to a Hero; the accepted
  actions still run.
- **No event is lost.** A model failure after the fallbacks, a run past its deadline, a
  blocked action or `finish(no_procedure=true)` with nothing accepted creates a Hero task
  in code (`application/escalation.py`) and completes the event.
- **Deterministic where it can be.** The prefilter, the intents of system events, the
  section selection, the rules and the executor are code; the same context renders the
  same prompt, byte for byte.

**Trade-offs.** The loop resends its prompt every round: an agent run reads about 16,200
input tokens over 2.7 rounds on average (`evals/RESULTS.md`), against about 4,700 for a
single structured plan. Its trajectory varies between runs. Both are bounded (the budgets,
the rules) and measured (trajectory checks, cost per run, pass^3).

| Alternative | Why not here |
|---|---|
| A chat model writes the gate's decision (GPT-4.1 Nano) | Measured against the decision model on the core suite at k = 3: the same reading of every message but one and the same 9 runs ended at the gate, at twice the latency (p95 1.24 s against 0.80 s) and twice the cost per call. Kept as `GATE_ENGINE=llm`. |
| One structured plan per event (one call, no tools) | Cheaper per run, and it was measured: 22/22 core scenarios on the same model. But it cannot search the SOP when the selected sections miss the situation, read a document on demand, or correct an action a rule refused. |
| A ReAct agent whose tools execute directly | An injected email can trigger a send, and a loop that fails halfway leaves half-applied state. Here tools stage and the commit applies all or nothing. |
| The whole SOP and history in every prompt | A 100-page SOP is about 50,000 to 70,000 tokens. On about 45 runs per load that is 2 to 3 million tokens per load: over $1 on any model above about $0.40 per million, and the model attends worse to the rule that matters. |
| The SOP compiled into a state machine | Every SOP change needs an engineer. A state machine is kept only where it pays: obligations and timers. |
| Several agents (per stage, per channel, a supervisor) | Each handoff adds a model call and loses context; the stage is already a key of the SOP selection. |
| A durable workflow per load (Temporal) | A way to run this pipeline at scale (5.2), not another agent design. |

### 1.2 How does the agent read documents, find their type and get their key values?

**Short answer.** A pipeline reads every attachment before the agent sees it: the bytes go
to a blob store, the pages are read (text, or the page as an image when the text is too
thin, as on a scan or a phone photo), and one `extract` call classifies the document among
the client's document types and fills that type's fields. Code checks the values against
the load. The result becomes a `document_processed` event, whose run reads the document and
follows the SOP. A failed check sends the document to a Hero for review.

| Step | Code |
|---|---|
| 1. A message with attachments is ingested; each attachment is saved as a `pending` document in the message's transaction | `AgentRuntime.ingest_message`; bytes in `adapters/documents/fs_blobs.py` (content-addressed, `sha256:<hex>`) |
| 2. The next tick reads pending documents before it runs the queue | `application/documents.py` (`DocumentPipeline`) |
| 3. Pages: the text of each page; a page with little text is rendered as a PNG | `adapters/documents/pdfium.py` (pypdfium2) |
| 4. One `extract` call, page images included: the type id, a confidence, the fields | prompt `prompts/extract.yaml`; model `google/gemini-2.5-flash` |
| 5. Code checks each field with a check (`FieldDef.check`) | `domain/documents.py` |
| 6. Status `processed` (every check passed), `needs_review` (a failed check, a missing field, confidence under 0.7, POD exceptions) or `failed` (file or model error); one `document_processed` event `doc:{document_id}`, queued once | `application/documents.py` |
| 7. A clean `processed` document of a required type closes its obligation as `satisfied` in the same transaction | same |
| 8. The `document_processed` run: the agent reads the document (`read_document`) and follows the SOP; a clean POD moves the stage to `delivered` and writes a TMS note; `needs_review` and `failed` create a Hero task | `sops/standard/documents.md`, `sops/clients/acme/proof_of_delivery.md` |

The document types are catalog data (`standard_broker()` in `domain/broker.py`); a client
adds or replaces types and fields in its `broker.yaml`:

| Type | Key values | Checked against the load |
|---|---|---|
| `bol` | BOL number, shipper, consignee, PO numbers, seal, weight, pickup date, signatures | shipper and consignee are stops; PO; seal is the one the driver reported; date in the pickup window; signed |
| `pod` | receiver (the facility), PO numbers, signature, delivery time, exceptions | receiver is a stop; PO; date in the delivery window; signed; exceptions empty |
| `rate_confirmation` | carrier and MC number, rate, line items, stops, appointments | MC number is the carrier's; stops |
| `receipt` | kind (lumper, scale, fuel), amount, date, vendor | date in a window |

**An attachment is handled once.** The message's own run does not act on the attachment
(the prompt and `documents.md` say so); the `document_processed` run does, and it queues
behind the message.

**Documents the load owes.** A client's `DocumentRequirement` (Acme: a `pod` at
`at_delivery` within 120 minutes) opens an obligation with a timer when the agent moves the
stage there; its follow-up timer asks the driver for the POD, then hands it to a Hero
(`p3_pod_overdue_follow_up`).

**Measured.** The fixtures are real PDFs (`evals/fixtures/docs/`: a clean POD, a POD with
exceptions and a mistyped PO, a scanned POD with no text layer, a BOL), each with its
expected values. An extraction of a one-page POD reads about 1,810 input and 113 output
tokens and costs $0.0008 in 1.3 s. The clean POD and the POD with exceptions were read
correctly in every live trial; the scanned POD and the BOL are covered by unit tests with
the fake model only, not by a live run.

**Trade-off.** One multimodal call per document is simple and handles phone photos.
OCR (for example AWS Textract) plus a text model would be cheaper on clean digital PDFs,
and a layout model would do better on dense rate confirmations; the pipeline's interface
(`DocumentProcessor`) allows either without touching the agent.

## 2. Instructions

### 2.1 How do you organize the SOPs of many clients (standard until a client changes it)?

**Short answer.** In three layers of Markdown files, one section per file with a stable
`id`. A client folder holds only the sections the client changes, each one replacing or
extending the standard section of the same id; a load type folder does the same for one
client's loads of one type. Next to the sections, a client's `broker.yaml` holds only what
the client changes in the standard catalog of situations, follow-ups, documents and limits.

```
sops/
  standard/<topic>.md                              every client
  clients/<client_id>/<topic>.md                   one client: replace or extend by id
  clients/<client_id>/types/<load_type>/<topic>.md one client's loads of one type
  clients/<client_id>/broker.yaml                  the client's catalog overrides
```

`effective_sections` (`domain/sop.py`) merges the layers per id, from general to
specific. `replace` (the default) swaps the body and the header; `extend` appends its body
and keeps the inherited header.

| Client | What it writes | Effect |
|---|---|---|
| Northline (Client A, load 481207) | `delay_risk_high` (replace) | After two SMS with no ETA: email the dispatcher, then a Hero task |
| Keystone (Client B) | `delay_risk_high` (replace); `types/reefer/driver_departed` (extend) | One SMS, then the broker's Slack, never the dispatcher; reefer loads also ask for the trailer temperature and seal |
| Acme | five sections and a `broker.yaml` | Its own `detention_request` situation, ETA every 20 minutes up to 3 times, a POD due 120 minutes after arrival |
| Any other client | nothing | The standard layer and catalog |

The catalog (`domain/broker.py`) is what code needs to know about a client's procedure:

| Part | Holds |
|---|---|
| `IntentDef` | What an event can mean: id, description (the gate prompt's text), examples, priority, `must_act` (the gate cannot skip it), `system` (set only by code) |
| `ObligationDef` | What the load owes and how it is chased: `follow_up_minutes`, `max_attempts`, the follow-up intent, who gets the case when attempts run out |
| `DocumentTypeDef`, `DocumentRequirement` | Document types, their fields and checks; which stage requires which document, within how long |
| `BrokerLimits` | The rules' limits (SMS rate, deferral policy, attempt caps) and the loop's budgets |

The standard catalog has 12 intents, the `eta_request` obligation (30 minutes, 2
attempts, then the dispatcher email and a Hero task) and four document types. The share of
procedures all clients follow is written once; a client's change cannot reach another
client.

**A 100-page SOP** becomes about 100 to 150 sections of 10 to 40 lines each; only the
matching ones reach the prompt (2.2). **Designed, not built:** onboarding from the client's
own document: upload the PDF, compile it into draft sections whose bodies are the
document's words verbatim, with proposed intents and stages, review the draft, publish.
An earlier build of this is at tag `full-platform`; a live compile of a 3-page SOP produced
a usable draft, but its intent links varied between compiles, which is why review and the
publish lint are part of the design.

### 2.2 How does the agent find the correct SOP files for the current event? Advantages and disadvantages.

**Short answer.** Rules first, then search. The event's intent (from code for system
events, from the gate for messages), the load's stage, the client and the load types
select the sections whose `intents` and `stages` match; those go in the prompt in full,
with one line for every other section. When they do not cover the event, the agent
searches the client's SOP with `search_sop` and opens a section with `read_sop_section`.
If nothing covers it, it answers `no_procedure` and the run hands the event to a Hero.

1. **By rules** (`SopIndex.by_applies`, `domain/sop.py` `resolve`, in the `knowledge`
   node): a section applies when its `intents` include the intent (or are empty) and its
   `stages` include the stage (or are empty), so `communication_rules` and `escalation`
   apply to every event.
2. **Procedures in progress** (`sections_in_progress` in `application/agent.py`): for each
   open obligation, the section that owns its follow-up intent is added as context, so a
   question at 07:13 sees how the ETA request continues (3.2).
3. **By search** (the agent's tools): word overlap over the visible sections, title and
   description counting twice (`adapters/knowledge/memory.py`); another client's sections
   are never visible.
4. **No procedure:** `finish(no_procedure=true)` with nothing accepted escalates with
   "No SOP for this situation" (`t2_gap_hero_without_promise`, `d4_lumper_no_sop`).

The run record keeps `sop_section_ids` (by rules), `sections_searched` (found or read by
the agent) and `sop_version` (the SOP tree's hash), so every action can be traced to the
text it followed.

| Advantages | Disadvantages |
|---|---|
| The common path is deterministic: the same event and state select the same sections, so a scenario can assert the procedure | A misread message starts from the wrong sections. Mitigated: system events skip the model, `must_act` intents always reach the agent, and the agent sees the catalog and can search |
| Small prompts: about three to five sections, not 100 pages, so low cost and latency and no unrelated rules | The editor must tag each section with intents and stages; the lint checks the tags exist, not that they are right |
| A situation no tag names is still found by search before it goes to a Hero | Word search misses synonyms. **Designed, not built:** hybrid search (pgvector embeddings and full-text rank fused by reciprocal rank, embedded once at publish), built earlier at tag `full-platform` |
| No leak between clients: the index filters by client before it ranks | Search results are not exact, so evals assert what a run used (`tools_used`, `sections_read`), not a ranking |

### 2.3 Non-technical people write and change the SOPs: how does the design make this safe?

**Short answer.** The editor writes plain English in a fixed small format, and every safety
property that matters is in code, not in the text. A change is linted with plain-English
messages, tested by the live scenario evals that name the section it touches, reviewed by
a person who knows the client, and recorded as a version every run names. A client's change
cannot reach another client. The editor and the versioned rollout are designed, not built.

| Safeguard | State |
|---|---|
| Fixed format: a header of six settings, a body in plain English; `sops/README.md` is the editor's guide | Built |
| Lint (`agent sop lint`, a CI step, `adapters/sop/lint.py`): unreadable header, unknown setting, intent or stage, duplicate id, an `extend` with nothing to extend, a catalog that cannot work, a file outside the layout; a warning for a section no scenario covers. Messages say what to fix, for an editor | Built |
| Layers: a client's file cannot change the standard or another client | Built |
| Guardrails in code (1.1): only the load's contacts, attempt caps, SMS rate, no contact details in text, budgets. A badly written SOP cannot make the agent text a stranger or loop | Built |
| Evals: scenarios tag the sections they cover (`sop:<id>`); every suite runs live before a change ships (4.2) | Built |
| Versions: the SOP tree's hash is recorded with every run, trace and experiment; reverting a change in git restores the old text and version | Built |
| An editor in the platform: edit a section in a form, see the lint and a preview of the sections an event would get (`GET /api/sops/preview` exists), submit for review | Designed, not built. The SOP screen is read-only |
| Review and approval: one approver who knows the client; two for the standard layer, which reaches every client | Designed, not built |
| Published versions per client, a canary on a share of its loads (sticky per load by a hash of the load id), comparison of escalation, no-op and block rates against the previous version, one-step rollback | Designed, not built (built earlier at tag `full-platform`) |

## 3. Memory

### 3.1 How does the agent know its past actions and the past messages of the load?

**Short answer.** It reads them from storage at the start of every run and can read more
with tools while it thinks. Every message, risk change, timer, processed document and agent
action is an entry in the load's append-only event log; what the load still owes is in the
ledger, with its timers; documents are rows with their values and checks.
`load_run_context` (`application/context.py`) reads one snapshot that every step uses.

| Memory | Store | In the agent prompt | Tool |
|---|---|---|---|
| Messages and actions | Event log | Transcript of the last 30 events, "in" and "out" lines | `get_history` (up to 200 events) |
| What is owed | Ledger: obligations (kind, status, attempts, deferral, active timer) and timers | "Ledger" and "Pending timers" | `get_ledger`, `list_document_requirements` |
| Procedure | The client's SOP sections | The selected sections (2.2) | `search_sop`, `read_sop_section` |
| Load facts | Load metadata and the client catalog | "Load", "Contacts" | `get_load` |
| Documents | Document rows | The document of a `document_processed` event | `read_document` |

- **Actions are memory.** For each outbox item the executor appends an `agent_action`
  event (`act:{event_id}:{index}`) in the same transaction (`application/executor.py`), so
  the next run sees what was sent, to whom, when and why.
- **History stops at the current event.** Messages already queued behind it are left out,
  so a run never acts on an ETA that has not arrived for it; agent actions are always kept.
- **The ledger holds only facts**, never a pointer into the SOP text, and nothing is kept
  in the process between runs, so any worker can run the next event.
- **Where it is stored.** `PERSISTENCE=memory` (the default, the tests and evals) keeps
  it in the process; `PERSISTENCE=postgres` (Docker Compose) keeps it in Postgres, one
  table per kind (`migrations/0001_load_memory.sql`), so a restarted platform goes on with
  its loads, runs and ledger. One transaction per unit of work; queue claims and due timers
  use `FOR UPDATE SKIP LOCKED`. Both stores pass the same contract suites
  (`tests/contract/`), and the assignment's scenario leaves the same state on both.
- **Designed, not built:** a rolling summary of the events older than the last 30. Today
  the agent pages back with `get_history`.

### 3.2 At 07:13, how does the agent remember the ETA request is still open?

**Short answer.** The model does not have to remember it: the ledger records it. The 07:01
run opened an `eta_request` obligation with 1 attempt and a follow-up timer due 07:31. The
driver's question does not change that row. The prompt shows the open request, its timer
and the procedure it belongs to, so the reply answers the question without asking for the
ETA again and without closing the request.

At 07:13 (`a_client_a_high_risk`, step 1) the event log holds the 07:00 risk change, the
07:01 SMS with `requests: eta_request`, and the 07:12 "What's the PO number?". The gate
returns `question`. The agent prompt carries (shortened):

```
## SOP sections for this event (follow them exactly)
### communication_rules: Communication rules ...
### driver_questions: Questions from the driver or other contacts
...
5. A question is not an ETA. If an ETA request is open, leave it and its follow-up
   as they are, and do not ask for the ETA again in your reply.
### escalation: Escalation to the Hero team ...

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
- 07:01 out sms to drv_20931 (driver), purpose eta_request: "What time will you arrive at the pickup?"
- 07:12 in sms from drv_20931 (driver): "What's the PO number?"  <- this event
```

The PO is in the load's instructions ("PO 55821 required at gate"), shown in the "Load"
block. The agent stages one SMS with purpose `reply`. Only an SMS or email that `requests`
an obligation kind counts as an attempt, so the ledger and the timer stay as they were.
The scenario forbids an `eta_request` SMS in this step and asserts 1 attempt and one timer
due at 07:31; `a_question_during_last_wait` checks the same after the second request, and
`b_ack_after_eta_request` checks that "ok" changes nothing.

### 3.3 How does a follow-up timer work? What does the agent see when the timer starts it?

**Short answer.** A timer is a row tied to an obligation. When it is due it becomes a
`timer_due` event in the load's queue, like a message. Code first checks that it is still
the active timer of an open obligation; a stale one ends its run with no model call.
Otherwise the agent sees the timer event, the ledger with the attempt count, the
transcript and the procedure, and does the next step.

1. **Set.** The agent stages `schedule_timer(kind=eta_request, due_in_minutes=30)`. The
   executor computes `due_at` in real minutes (`domain/timers.py`, safe across a DST
   change), never before a contact's promised time plus 2 minutes, makes it the
   obligation's active timer and marks older pending timers `superseded`. A document
   requirement sets one too (Acme's POD, 120 minutes after arrival).
2. **Fire.** `AgentRuntime.fire_due_timers` turns each due timer into the event
   `timer:{timer_id}`, marks it `fired` and queues it behind the load's earlier events, in
   one transaction.
3. **Stale check** (prefilter): superseded, cancelled, obligation closed, or no longer the
   active timer: `skipped`, no model call (`g1_deferral_stale_timer`).
4. **Intent from code:** the obligation kind's follow-up intent (`eta_follow_up_due`,
   `pod_follow_up_due`), with no gate call.
5. **What the agent sees at 07:31:**

```
## Ledger (the source of truth for obligations and attempts)
Open:
- eta_request: attempts 1; opened 07:01; last action 07:01 (30 min ago); deferred until
  none, deferrals used 0; follow-up timer due 07:31 (now), firing now (this event)

## Transcript (recent events, oldest first; "out" lines are what you did)
- 07:00 system: delay risk changed low -> high
- 07:01 out sms to drv_20931 (driver), purpose eta_request: "..."
- 07:12 in sms from drv_20931 (driver): "What's the PO number?"
- 07:13 out sms to drv_20931 (driver), purpose reply: "The PO number is 55821."
- 07:31 system: follow-up timer due for eta_request  <- this event

## Event (decide what to do about this)
type: timer_due
timer: follow-up for eta_request, due 07:31 (now)
```

with Northline's `delay_risk_high` section in full ("After 1 ETA request: send the driver
a second SMS ... Set a follow-up timer for 30 minutes"). One attempt means the second step.
At 08:01 the ledger shows 2 attempts, the client's cap: a third request would be refused
at the call by `attempt_cap`, and the section says to email the dispatcher, create an
urgent Hero task and close the request as `exhausted`.

Closing an obligation cancels its timers (`b_eta_received_cancels_follow_up`). "I'll tell
you in 5 minutes" at 07:30 replaces the 07:31 timer with one at 07:37, and the old timer's
event is skipped as stale (`g1_deferral_stale_timer`).

**Designed, not built:** an always-on scheduler. Here `tick()` fires the due timers (the
platform, the evals and Studio call it on a virtual clock). In production a scheduler
claims due timers with `FOR UPDATE SKIP LOCKED` from several workers, or EventBridge
Scheduler or Temporal timers fire them (5.2).

## 4. Evals

### 4.1 How do you make sure the system works correctly in production?

**Short answer.** Three layers: deterministic tests and live scenario evals before a
release; guardrails in code at run time, so a wrong model answer cannot do damage and every
failure reaches a person; and monitoring after the release, of which the traces are built
and the metrics and alerts are designed.

**Before a release.**

- `make ci`: lint, strict types, layer rules, 1,719 tests (unit, contract, property,
  integration, eval harness) with 99.5% coverage of `domain` and `application` (branches
  included; the gate is 90%), and the SOP lint.
  The tests run the real graph, rules and executor with a fake model
  (`tests/fakes.py`) that plays the model's side, including an agent that falls for an
  injected email (`tests/unit/application/test_graph_guardrails.py`).
- Live scenario evals (`agent eval run`): a YAML scenario drives the product's
  `AgentRuntime` on a virtual clock with recording gateways, on the real models, as a
  LangSmith experiment with k repetitions; a scenario passes only if every repetition
  passes (pass^k), because a customer sees every run.

| Evaluator | Checks |
|---|---|
| `facts` | Action kinds, recipients, purposes, counts, `forbid` lists, the ledger per obligation kind, timers, run statuses, model calls |
| `trajectory` | The run's intent, tools used and refused, sections read, refused calls (`RunRecord.trajectory`) |
| `judge` | Wording, against a rubric per step: an LLM judge (GPT-4.1 mini) scores the texts the step created |
| `invariants` | Unique idempotency keys; no recipient outside the load's contacts |
| `passed`, `cost_usd`; summary `pass_hat_k`, `pass_at_k` per tag, total cost | |

**Negative control.** A grader that cannot fail proves nothing:
`tests/evalkit/test_negative_control.py` corrupts one thing in a right play (a wrong
recipient, a missing timer, an extra SMS, a skipped tool, a low judge score, a reused
idempotency key) and asserts that exactly that evaluator fails.

**At run time:** the rules at the call and again in `validate`, the budgets, `must_act`
intents the gate cannot skip, the prefilter, the escalation paths, idempotent delivery.

**After a release.** Built: every run has a LangSmith trace (each node and model round,
with the prompt version of each role) and its link in the run record; the platform's load
inspector shows each run's path, tool calls and actions. **Designed, not built:**

| Signal | Alert |
|---|---|
| Age of the oldest queued event | above 2 minutes warns, above 5 is the SLA breach |
| Run p95, deadline escalations, dead letters | any dead letter; p95 over 60 s |
| Status mix per client: acted, noop, escalated, blocked | a shift after a deploy or an SOP change |
| Model errors, retries, providers and fallback share per model | fallback share over 5% (a weaker fallback costs pass rate: `evals/RESULTS.md`) |
| Cost per load against $1 | 80% of the budget |
| Stalled obligations (open, no timer), timer lag | any |

plus online evaluation: the LLM judge on a sample of production runs in LangSmith, and Hero
corrections turned into new scenarios.

### 4.2 A client changes its procedure: how do you make sure other behavior does not break?

**Short answer.** The change is a file in that client's folder, so it cannot change the
sections another client reads. It must pass the lint, and every suite runs live on it at
k = 3 before it ships: the scenarios tagged with the changed section show whether the new
behavior is right, and the rest show nothing else moved. A change to the standard layer
reaches every client, so the same full run is required.

1. **Layers.** `clients/northline/delay_risk_high.md` is visible only to Northline loads
   (`effective_sections` filters by client before anything else).
2. **Coverage.** Scenarios carry `sop:<section_id>` tags; the lint warns about a section no
   scenario covers (today: Acme's `detention`). A new rule comes with a new scenario.
3. **Client variants of the same scenarios.** A scenario runs on another client with
   `load_overrides` (`d1_keystone_high_risk`, `p1_pod_clean_delivers` on Acme), so a
   client's change is tested on its own loads and the other clients' scenarios stay as
   they are.
4. **Compare.** Each experiment records the SOP hash, git sha, models and prompt commits.
   Regression scenarios must stay at pass^3 = 100%; LangSmith's comparison view shows,
   per scenario, which repetitions changed.
5. **Designed, not built:** per-client published versions with a canary on a share of the
   client's loads and one-step rollback (2.3), so a change that passed the evals still
   meets real traffic gradually.

### 4.3 You change the LLM: how do you make sure the system does not break?

**Short answer.** A model is one line per role in `config/models.yaml`. A switch is a
bake-off: the candidate runs every suite live at k = 3 against thresholds fixed before the
run, and the results are compared as LangSmith experiments, whose metadata names the model
of each role. The safety rules do not depend on the model.

- **Thresholds** (`evals/RESULTS.md`): groups A and B at pass^3 = 100%, no `must_act`
  message dropped by the gate, at most $0.0015 per model-calling run, at most 0.5 refused
  calls per run, the SOP-gap scenario in every trial. The cheapest model that meets all
  wins. That chose `openai/gpt-oss-120b` for the agent ($0.00066 per run); Claude Haiku
  5.5 passed 22/22 at 3.6 times the cost and is the first upgrade.
- **The fallback is part of the change.** Each role has a chain in `config/models.yaml`:
  the model on its preferred provider (OpenRouter may serve it from another provider that
  takes the same parameters), two short retries on the same model when the provider rate
  limits, then fallback models with settings of their own, each model behind a circuit
  breaker (`adapters/llm/resilient.py`). An earlier k = 3 round showed why the fallback
  is measured too: rate limits moved agent rounds to Gemini 2.5 Flash Lite, and both core
  failures came from rounds it served. The agent's fallback is now Claude Haiku 5.5, which
  passed the suite as the agent model; in the current round the retries answered 91 of the
  99 rate-limited rounds on the primary and Haiku the other 8 (`evals/RESULTS.md`).
- **Model-specific behavior is found by these runs, not by tests:** OpenRouter has no
  Anthropic endpoint for `tool_choice: required` (the adapter sends `auto` there), only
  DeepInfra serves gpt-oss at bf16 with a forced tool call (so OpenRouter has no other
  provider to route an agent round to), and Google endpoints dropped a union type in a
  tool schema (the action tools have flat schemas, with no `$ref` and no union:
  `application/agent_tools.py`).
- **The gate's engine went through the same kind of run.** The rule was fixed first: the
  decision model would become the default gate only with a core pass^3 no lower than GPT-4.1
  Nano's, no `must_act` message ended at the gate, and a gate p95 no higher. It passed
  22/22 against 21/22 (the chat round's failure was the agent's), ended the same 9 runs
  at the gate, and answered at p95 0.80 s against 1.24 s (`evals/RESULTS.md`, "How the
  gate engine was chosen"). The experiment's metadata names the engine and its threshold.
- **Independent of the model:** structured output is a forced tool call validated by
  pydantic, and a decision model's answers are typed and checked the same way, so a
  malformed answer is an error (fallback, then a Hero task), never a half-read plan; the
  rules hold for any model.
- **Prompts** follow the same path: a template change is pushed to the Prompt Hub as
  `candidate`, measured with `agent eval run --prompt-tag candidate`, then tagged `prod`.

### 4.4 Clients have different procedures and complexity: how do you organize the evals?

**Short answer.** Scenarios are YAML files grouped in suites, each a LangSmith dataset,
tagged by purpose (regression or capability) and by the SOP sections they cover. A client
variant reuses the same load fixture with `load_overrides` (another broker, load type or
stage), so a new client starts from the standard scenarios run on its catalog and adds its
own only for what it changes.

| Suite (dataset) | Scenarios | Checks |
|---|---|---|
| `core` (`freight-agent-core`) | 22, groups A to G | The assignment's sequence and the standard procedures, on Northline, Keystone and Keystone reefer |
| `agent` (`freight-agent-agent`) | `t2_gap_hero_without_promise` | A request no SOP covers reaches a Hero with the driver's words and no promise, judged on the outcome |
| `docs` (`freight-agent-docs`) | `p1`, `p2`, `p3` | The document pipeline with real PDFs on Acme: a clean POD, a POD with exceptions, an overdue POD |

| Core group | Scenarios | Checks |
|---|---|---|
| A | `a_client_a_high_risk`, `a_question_during_last_wait` | The assignment's sequence |
| B | `b_ack_with_nothing_open`, `b_ack_after_eta_request`, `b_broker_thanks_by_email`, `b_medium_risk_does_not_wake`, `b_eta_received_cancels_follow_up` | Doing nothing |
| C | `c1` to `c4` | ETA answers: driver, dispatcher, relative, vague; risk cleared |
| D | `d1_keystone_high_risk`, `d2_keystone_reefer_departure`, `d4_lumper_no_sop` | Client B, a reefer load, a question no SOP answers |
| E | `e1_prompt_injection`, `e3_duplicate_message` | Injection, a duplicate delivery |
| G | `g1` to `g5` | Deferrals: a stale timer, an expired promise, a second deferral refused |

- **Regression or capability.** Regression scenarios gate a run (exit code 1);
  capability ones (`c3_relative_eta`, `d4_lumper_no_sop`) are measured and move to
  regression once they pass reliably.
- **Doing nothing is tested** in group B: an "ok", a broker's thanks, a medium risk, each
  asserting no outbox item and no agent call where none is needed.
- **Code guardrails are tests, not evals.** What must hold whatever the model does (an
  outside address refused, the rejection budget, the gap path) is tested with the fake
  model in `test_graph_guardrails.py`; evals measure what the model does.
- **When each runs.** Tests on every pull request (`.github/workflows/ci.yml`). Live evals
  by hand (`evals-live.yml`, `make eval`) with a cost cap, and required before a prompt,
  SOP or model change ships. **Designed, not built:** a suite per client folder
  (`evals/scenarios/<client>/`) once a client has more than a few own scenarios, run on
  every change to that client's folder.

## 5. Scale, response time and cost

### 5.1 How does the design keep each run at 5 minutes or less?

**Short answer.** Each run is bounded by construction: a per-load FIFO with loads processed
in parallel, no model call for most events, one gate call for messages and one agent loop
with a round and a cost limit, a 60-second timeout per model call (its retries included)
inside a 240-second run deadline, and an escalation to a Hero instead of a rerun when the
models or the deadline fail. Measured, the agent call (all its rounds) takes 7.7 s at p50
and 12.5 s at p95.

| Mechanism | Where |
|---|---|
| One event per load in flight, loads in parallel; stateless runs | `EventQueue` (`interfaces/repositories.py`), `application/runtime.py` |
| No transaction open while a model thinks | claim, think, commit in `AgentRuntime.process_next` |
| No model call for pings, stale timers, duplicates, empty messages; intents of system events from code | `domain/ping_filter.py`, `application/prefilter.py`, `application/gate.py` |
| At most 8 agent rounds and $0.05 per run, then a forced answer | `BrokerLimits`, `application/graph/nodes.py` |
| 60 s per model call, 240 s per run; past the deadline one urgent Hero task and the event completes | `RuntimeConfig`, `application/escalation.py` |
| A rate limit is retried at most twice on the same model, about 1 s then 2 s later, inside the call's timeout; then the fallback chain, whose circuit breakers skip a failing model instead of waiting on it | `RetryPolicy`, `adapters/llm/resilient.py` |
| A worker that dies mid-run: the claim lease (deadline plus 60 s) returns the event; 3 failures make a dead letter | `EventQueue.reclaim_expired` |

**Measured** (`evals/RESULTS.md`, k = 3 round, 135 agent runs of the core suite): agent call
p50 7.3 s, p95 11.4 s, max 14.9 s; gate call p50 0.4 s, p95 0.8 s (0.9 s and 1.2 s on
GPT-4.1 Nano); a document extraction 1.3 s. A rate-limited round adds its backoff, about 1
to 3 s; 63 core rounds were rate limited, and no run reached its deadline. Eight rounds at the 60-second timeout could
exceed 240 s, which is why the deadline, not the round count, is the bound. The deadline
leaves 60 seconds of the 5 minutes for the queue wait and the delivery.

**Designed, not built:** workers that scale on the age of the oldest queued event, an
always-on outbox dispatcher (today the tick delivers), and priority for risk changes and
timers over chatter.

### 5.2 Volume x10: what must change?

**Short answer.** The logic, the prompts and the cost per load stay; the adapters behind
the interfaces change: a durable queue partitioned by load, a timer scheduler, Postgres, an
object store, worker pools that scale on queue age, and higher model rate limits. The
first limit this design meets is the model providers' rate limits, not compute.

**Volume.** 100,000 loads a month with 50 to 100 messages each is 5 to 10 million messages a
month, about 10 per second over 22 business days of 10 hours. At 10x: about 100 messages
per second, plus pings. With one gate call per message and an agent run for about half of
them, that is about 50 agent runs per second; at 7 s each, about 350 runs in flight
(Little's law), and about 45 million input tokens a minute at peak.

| Part | Today | At 10x (designed) |
|---|---|---|
| Queue | Per-load FIFO in the store (a Postgres table on the platform) | SQS FIFO with `MessageGroupId = load_id` (order per load, parallel across loads), or Temporal with a workflow per load |
| Workers | `tick()` in one process | Stateless async workers, about 30 runs in flight each, autoscaled on queue age |
| Timers | Rows fired by `tick()` | A scheduler claiming due timers with `FOR UPDATE SKIP LOCKED`, or EventBridge Scheduler |
| Pings | One transaction per ping | A stream partitioned by load holds the risk tracker; only transitions reach the queue |
| Storage | Postgres (`PERSISTENCE=postgres`, one instance in Docker Compose) or the in-memory store, behind the repository interfaces | The same schema with the events table partitioned by month, closed loads archived to object storage, a pooler (the adapter needs no session state), a read replica for the platform and metrics |
| SOP index | Files read per run | Published versions in Postgres, cached per client by hash; hybrid search with pgvector |
| Documents | Blobs on the filesystem; read in the tick | An object store behind `BlobStore`; document workers scaled on pending documents |
| Models | One OpenRouter key; a rate limit is retried on the same model, OpenRouter may serve the model from another provider that takes the same parameters (the gate: OpenAI, then Azure), then a fallback model that passes the suite serves the call | Higher rate limits or provisioned throughput, per-client concurrency limits |
| Tracing | Every run | Sampling: every escalation and failure, a share of the rest |
| Cost | About $0.04 per load | The same per load; the total grows linearly |

The layer rules (import-linter contracts in `pyproject.toml`) keep storage, queues, blobs
and providers behind interfaces, so the run graph, the rules, the ledger and the eval suites do
not change. The Postgres adapter already passes the memory store's contract suites, and
its queue claim and due-timer read skip rows another worker holds (`SKIP LOCKED`), so
several workers can share them.

### 5.3 How do you keep LLM cost at about $1 or less per load?

**Short answer.** Most events never reach a model, the gate keeps chatter away from the
agent, the agent runs a cheap model chosen by a bake-off, its prompt holds only the
matching SOP sections, and each run has a round and a cost budget. Measured on the current
design: about $0.00005 per gate call and $0.0007 per agent run, so about **$0.04 for a
typical load and $0.06 for a heavy one**.

| Per load | Typical | Heavy |
|---|---|---|
| Inbound messages, one gate call each ($0.000049) | 75: $0.004 | 100: $0.005 |
| Agent runs ($0.00068): messages the gate passes, plus risk changes, timers and documents | 42: $0.029 | 77: $0.052 |
| Document extractions ($0.0008) | 4: $0.003 | 5: $0.004 |
| Location pings | no model call | no model call |
| **Total** | **about $0.04** | **about $0.06** |
| With the agent on Claude Haiku 5.5 | about $0.11 | about $0.20 |

Per-call figures are means over the k = 3 round (`evals/RESULTS.md`); the volumes are the
assignment's 50 to 100 messages and an assumption for the rest.

**Levers, in order of effect:**

1. **Pings never reach a model:** `domain/ping_filter.py` turns hundreds of pings into a
   few risk transitions (`b_medium_risk_does_not_wake`).
2. **The prefilter:** duplicates, stale timers, empty messages and unknown senders stop in
   code.
3. **The gate:** an "ok" ends as `noop` after a $0.00005 decision call instead of an
   $0.0007 loop; only an intent the catalog lets end a run can end it, so `must_act`
   keeps the intents that need action. The decision model's input costs $0.042 per
   million tokens and its output nothing.
4. **A cheap model per role,** chosen by the bake-off (4.3): the same loop on a $3 / $12 per
   million model with no caching would cost about 42 x (16,200 x $3 + 220 x $12) / 1M,
   about $2.15 per typical load.
5. **Small prompts:** only the matching sections in full; the rest is one line each and a
   search away.
6. **The run budget:** 8 rounds and $0.05 per run, per client.
7. **Prompt caching:** the stable parts come first (the system prompt is rendered from the
   client alone, then load, contacts, SOP) so every round of a loop repeats the same
   prefix; OpenAI and Gemini cache long prefixes automatically. DeepInfra reports no cache
   reads for gpt-oss, so the figures above have no caching discount.

**Designed, not built:** a per-load cost meter enforced in the runtime. The per-run cap
bounds a runaway loop, not a load (77 runs at $0.05 would be $3.85). The meter would sum
each load's run costs (already in every run record): at 80% of $1, an alert and a lower
round budget; at 100%, judgment calls go to a Hero. Also a rolling summary of old events,
which keeps the transcript short on loads with hundreds of events.
