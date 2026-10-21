# Answers to the assignment's questions

Each answer starts with a short answer; details and trade-offs follow. Diagrams are in
[`architecture.md`](architecture.md), exact formats and rules in
[`decisions.md`](decisions.md), measured numbers in [`../evals/RESULTS.md`](../evals/RESULTS.md).

Python paths leave out `src/freight_agent/`. A scenario id is a file in
`evals/scenarios/<suite>/`. **Designed, not built** marks a part described here but not in
the code.

## Words used here

- **SOP**: the client's written procedure; here, Markdown files in `sops/`, one section each.
- **Event**: anything that happens on a load: a message, a delay-risk change, a follow-up
  timer that is due, a document that was read. A **run** is one pass of the agent over one event.
- **Intent**: what an event means, from a fixed list per client (`question`, `eta_provided`,
  `acknowledgement` ...).
- **Gate**: the first step for an inbound message. It finds the intent and decides whether
  the message needs the agent. It uses a **decision model**: a model that answers fixed
  questions with probabilities and writes no text (TypeSafe's Jev).
- **Obligation**: something the load still owes, such as the ETA we asked for, with a status
  and a count of attempts. The **ledger** is the table of a load's obligations; code keeps
  it, not the model. A **timer** is a follow-up at a set time, tied to one obligation.
- **Rule**: a check in code on every action the agent proposes ("the recipient is a contact
  of the load").
- **Outbox**: the messages a run decided to send, written when the run commits and delivered
  afterwards. Each carries an **idempotency key**, a unique id, so a message sent twice
  arrives once.
- **Catalog**: a client's intents, obligation kinds, document types and limits
  (`broker.yaml`, on top of a standard one).
- **Tick**: one pass of the work loop: fire due timers, read new documents, run queued
  events, deliver the outbox.
- **pass^k**: a scenario passes only if all k repetitions pass (k = 3 here).
- **Prompt Hub**: LangSmith's store of prompt versions. The runtime reads its templates
  there, with a reviewable copy in `prompts/`.

## What is built

Everything described here is built, except the parts marked **designed, not built**: an
always-on worker and timer scheduler (3.3, 5.2), real integrations (the gateways record what
they would send), an SOP editor with review and approval and SOP versions with a canary and
rollback (2.3), hybrid search over SOPs (2.2), metrics and alerts (4.1), the per-load cost cap
and a rolling summary of old events (5.3). An earlier build of several of these (pgvector
search over SOPs, SOP onboarding from a PDF, client config versions with a canary, metrics)
is at the git tag `full-platform`.

## 1. Agent architecture

### 1.1 What is the architecture of your agent? What are the alternatives?

**Short answer.** Each event starts one run, and the run keeps nothing in the process
afterwards. Each load has its own first-in, first-out (FIFO) queue: one event at a time per
load, loads in parallel. Code does every step that needs no judgment: it reads the load's
memory, drops events that need no thought, gives system events their intent, picks the SOP
sections and checks every action, so the same context gives the same prompt, byte for byte.
A model is used in two places:

- **The gate** reads each inbound message once. The decision model gives the intent and the
  probability that the message needs the agent now. Code decides from those numbers; a
  doubtful message goes on to the agent.
- **The agent** works inside one bounded tool loop. It reads what it needs (SOP, history,
  ledger, load, documents) and proposes actions one at a time. Code checks each action at
  once and answers "accepted" or "rejected by <rule>", with what an acceptable action looks
  like. Nothing is sent until the run commits, and then only through the outbox.

The run graph is `prefilter -> gate -> knowledge -> agent <-> tools -> validate`
(`application/graph/build.py`; diagram, node table and budgets in
[architecture.md section 4](architecture.md#4-the-run-graph)). Around it, the runtime
(`application/runtime.py`) runs three steps: a short transaction **claims** the event and
reads its context; the graph **thinks** outside any transaction, with a 240-second deadline;
a second transaction **commits** the approved actions (ledger, timers, stage, outbox), the
run record and the event's completion. Delivery follows from the outbox, with the
idempotency key `{event_id}:{index}`.

**The agent's tools** (`application/agent_tools.py`):

- **Read:** `search_sop`, `read_sop_section`, `get_history`, `get_ledger`, `get_load`,
  `read_document`, `list_document_requirements`, `recall_facility`. Message and document
  text comes back quoted, as untrusted data.
- **Action:** `send_sms`, `send_email`, `post_broker_slack`, `write_tms_note`,
  `create_hero_task`, `post_internal_slack`, `update_stage`, `schedule_timer`,
  `defer_obligation`, `close_obligation`, `open_obligation`. Each builds the action, checks
  it against the rules and the actions accepted so far, and executes nothing. It answers
  `accepted as action #n` or `rejected by <rule>: <why>`, then how to correct it ("its
  requests are used up. No further message may carry requests: eta_request ...").
- **`finish(rationale, intent, no_procedure)`** ends the loop.

**Guardrails that hold whatever the model does:**

- **The model cannot act.** Action tools only propose; the executor applies the approved
  actions in the commit. The rules (`domain/rules.py`, full list in
  [decisions.md](decisions.md#run-graph-and-budgets)): the recipient is a contact of the
  load, on a channel that contact has; at most 3 SMS per contact per hour; an attempt cap per
  obligation kind; obligation kinds from the client's catalog only; one deferral of at most
  15 minutes; the stage only moves forward; no duplicate action; no phone number or email
  address in outgoing text. A rule refuses an action and never edits it, so the next round
  can correct it. `e1_prompt_injection` checks that an email asking for the rate
  confirmation at an outside address gets nothing sent there.
- **Budgets per client** (`BrokerLimits`): 8 rounds and $0.05 of model cost per run; then
  the next round must call `finish`. More than 3 refused action calls stop the run and hand
  it to a Hero; the accepted actions still run.
- **No event is lost.** A model failure after the fallbacks, a run past its deadline, a
  blocked action, or "no procedure" with nothing accepted: code creates a Hero task
  (`application/escalation.py`) and completes the event.

**Trade-offs.** The loop sends its prompt again every round: an agent run reads about 16,200
input tokens over 2.7 rounds on average ([RESULTS](../evals/RESULTS.md#the-k--3-round)),
against about 4,700 for a single structured plan. Its path through the tools also varies
between runs. Both are bounded (budgets, rules) and measured (trajectory checks, cost per
run, pass^3).

| Alternative | Why not here |
|---|---|
| A chat model writes the gate's decision (GPT-4.1 Nano) | Measured on the core suite at k = 3: the same reading of every message but one and the same 9 runs ended at the gate, at about twice the latency (p95 1.24 s against 0.80 s) and twice the cost per call. Kept as `GATE_ENGINE=llm`. |
| One structured plan per event (one call, no tools) | Cheaper per run, and measured: 22/22 core scenarios on the same model. But it cannot search the SOP when the selected sections miss the situation, read a document on demand, or correct an action a rule refused. |
| A ReAct agent whose tools act directly | An injected email can trigger a send, and a loop that fails halfway leaves half-applied state. Here tools only propose, and the commit applies all or nothing. |
| The whole SOP and history in every prompt | A 100-page SOP is about 50,000 to 70,000 tokens. At about 45 runs per load that is 2 to 3 million tokens per load: over $1 on any model above about $0.40 per million, and the model pays less attention to the rule that matters. |
| The SOP compiled into a state machine | Every SOP change needs an engineer. A state machine is kept only where it pays: obligations and timers. |
| Several agents (per stage, per channel, a supervisor) | Each handoff adds a model call and loses context. The stage already selects the SOP sections. |
| A durable workflow per load (Temporal) | A way to run this pipeline at scale (5.2), not a different agent design. |

### 1.2 How does the agent read documents, find their type and get their key values?

**Short answer.** A pipeline reads every attachment before the agent sees it. It saves the
file, reads the pages, and makes one `extract` model call that picks the document type from
the client's list and fills that type's fields. Code checks the values against the load.
The result becomes a `document_processed` event, whose run reads the document and follows
the SOP. A failed check sends the document to a Hero for review.

The flow ([architecture.md section 7](architecture.md#7-documents)):

1. Each attachment is saved as a `pending` document in the message's transaction; the bytes
   go to a blob store, named by their SHA-256 hash (`adapters/documents/fs_blobs.py`).
2. The next tick reads it before it runs the queue (`application/documents.py`): the text
   of each page, or a PNG image of a page with little text, such as a scan or a phone photo
   (`adapters/documents/pdfium.py`, pypdfium2).
3. One `extract` call, page images included, answers the type, a confidence and the fields
   (`prompts/extract.yaml`, `google/gemini-2.5-flash`). Code checks each field
   (`domain/documents.py`).
4. Status `processed` (every check passed), `needs_review` (a failed check, a missing field,
   confidence under 0.7, POD exceptions) or `failed` (a file or model error). A clean
   document of a required type closes its obligation as `satisfied` in the same transaction.
5. One `document_processed` event (`doc:{document_id}`) runs the agent, which reads the
   document and follows the SOP: a clean POD moves the stage to `delivered` and writes a TMS
   note; `needs_review` and `failed` create a Hero task (`sops/standard/documents.md`,
   `sops/clients/acme/proof_of_delivery.md`).

The document types are catalog data (`domain/broker.py`); a client adds or replaces types
and fields in its `broker.yaml`.

| Type | Key values | Checked against the load |
|---|---|---|
| `bol` | BOL number, shipper, consignee, PO numbers, seal, weight, pickup date, signatures | shipper and consignee are stops; PO; the seal is the one the driver reported; date in the pickup window; signed |
| `pod` | receiver (the facility), PO numbers, signature, delivery time, exceptions | receiver is a stop; PO; date in the delivery window; signed; no exceptions |
| `rate_confirmation` | carrier and MC number, rate, line items, stops, appointments | MC number is the carrier's; stops |
| `receipt` | kind (lumper, scale, fuel), amount, date, vendor | date in a window |

- **An attachment is handled once.** The message's own run does not act on it (the prompt
  and `documents.md` say so); the `document_processed` run does, and it queues behind the
  message.
- **Documents the load owes.** Acme requires a `pod` within 120 minutes of `at_delivery`.
  When the agent moves the stage there, an obligation opens with a timer, which asks the
  driver for the POD and then hands the case to a Hero (`p3_pod_overdue_follow_up`).
- **Measured.** The fixtures are real PDFs (`evals/fixtures/docs/`: a clean POD, a POD with
  exceptions and a mistyped PO, a scanned POD with no text layer, a BOL), each with its
  expected values. Reading a one-page POD takes about 1,810 input and 113 output tokens,
  $0.0008 and 1.3 s. The clean POD and the POD with exceptions were read correctly in every
  live trial; the scanned POD and the BOL are covered only by unit tests with the fake model.
- **Trade-off.** One multimodal call per document is simple and handles phone photos. OCR
  (for example AWS Textract) plus a text model would be cheaper on clean digital PDFs, and a
  layout model would do better on dense rate confirmations. The pipeline's interface
  (`DocumentProcessor`) allows either without touching the agent.

## 2. Instructions

### 2.1 How do you organize the SOPs of many clients (standard until a client changes it)?

**Short answer.** In three layers of Markdown files, one section per file, each with a
stable `id`. A client folder holds only the sections the client changes; each replaces or
extends the standard section with the same id. A load-type folder does the same for one
client's loads of one type. A client's `broker.yaml` holds only what it changes in the
standard catalog.

```
sops/
  standard/<topic>.md                              every client
  clients/<client_id>/<topic>.md                   one client: replace or extend by id
  clients/<client_id>/types/<load_type>/<topic>.md one client's loads of one type
  clients/<client_id>/broker.yaml                  the client's catalog changes
```

The layers merge per id, from general to specific (`domain/sop.py`): `replace` (the
default) swaps the text and the header; `extend` adds its text at the end and keeps the
inherited header.

| Client | What it writes | Effect |
|---|---|---|
| Northline (Client A, load 481207) | `delay_risk_high` (replace) | After two SMS with no ETA: email the dispatcher, then a Hero task |
| Keystone (Client B) | `delay_risk_high` (replace); `types/reefer/driver_departed` (extend) | One SMS, then the broker's Slack, never the dispatcher; reefer loads also ask for the trailer temperature and seal |
| Acme | five sections and a `broker.yaml` | Its own `detention_request` situation, ETA every 20 minutes up to 3 times, a POD due 120 minutes after arrival |
| Any other client | nothing | The standard layer and catalog |

The catalog (`domain/broker.py`; format in [decisions.md](decisions.md#client-catalog)) is
what code must know about a client's procedure: its **intents** (with the text the gate
reads, examples, and `must_act`: the gate may not end the run); its **obligations** (minutes
between follow-ups, the attempt cap, who gets the case when attempts run out); its
**documents** (types, fields and checks, and which stage requires which document, within how
long); its **limits** (the rules' limits and the run's budgets). The standard catalog has 12
intents, the `eta_request` obligation (30 minutes, 2 attempts, then the dispatcher email and
a Hero task) and four document types. What all clients share is written once, and one
client's change cannot reach another client.

**A 100-page SOP** becomes about 100 to 150 sections of 10 to 40 lines each; only the
matching ones reach the prompt (2.2). **Designed, not built:** onboarding from the client's
own document: upload the PDF, turn it into draft sections whose text is the document's own
words, with proposed intents and stages, review, publish. An earlier build is at tag
`full-platform`. A live run on a 3-page SOP gave a usable draft, but its intent links varied
between runs, which is why review and the publish lint are part of the design.

### 2.2 How does the agent find the correct SOP files for the current event? Advantages and disadvantages.

**Short answer.** Rules first, then search. The event's intent, the load's stage, the client
and the load types select the sections whose `intents` and `stages` match. Those go into the
prompt in full, with one line for every other section. When they do not cover the event, the
agent searches the client's SOP (`search_sop`) and opens a section (`read_sop_section`). If
nothing covers it, the agent answers `no_procedure` and the run goes to a Hero.

1. **By rules** (`SopIndex.by_applies`, in the `knowledge` node): a section applies when its
   `intents` include the intent (or are empty) and its `stages` include the stage (or are
   empty). So `communication_rules` and `escalation` apply to every event.
2. **Procedures in progress:** for each open obligation, the section that owns its follow-up
   is added as context, so a question at 07:13 sees how the ETA request goes on (3.2).
3. **By search:** word overlap over the sections the client can see, title and description
   counted twice (`adapters/knowledge/memory.py`). Another client's sections are never visible.
4. **No procedure:** `finish(no_procedure=true)` with nothing accepted escalates with "No SOP
   for this situation" (`t2_gap_hero_without_promise`, `d4_lumper_no_sop`).

The run record keeps the sections chosen by rules, those the agent searched or read, and the
SOP version (a hash of the SOP folder), so every action traces back to the text it followed.

| Advantages | Disadvantages |
|---|---|
| The common path is fixed: the same event and state select the same sections, so a scenario can assert the procedure | A misread message starts from the wrong sections. Reduced by: system events skip the model, `must_act` intents always reach the agent, and the agent sees the list of sections and can search |
| Small prompts: three to five sections, not 100 pages, so low cost and latency and no unrelated rules | The editor must tag each section with intents and stages; the lint checks that the tags exist, not that they are right |
| A situation no tag names is still found by search before it goes to a Hero | Word search misses synonyms. **Designed, not built:** hybrid search (pgvector embeddings plus full-text rank; built earlier at tag `full-platform`) |
| No leak between clients: the index filters by client before it ranks | Search results are not exact, so evals assert what a run used (tools, sections read), not a ranking |

### 2.3 Non-technical people write and change the SOPs: how does the design make this safe?

**Short answer.** The editor writes plain English in a small fixed format, and every safety
property that matters is in code, not in the text. A change is checked by the lint (messages
written for an editor), tested by the live evals that name the section, reviewed by a person
who knows the client, and recorded as a version every run names. A client's change cannot
reach another client. The editor screen and the versioned rollout are designed, not built.

| Safeguard | State |
|---|---|
| Fixed format: a header of six settings, a body in plain English; [`sops/README.md`](../sops/README.md) is the editor's guide | Built |
| Lint (`agent sop lint`, a CI step, `adapters/sop/lint.py`): unreadable header, unknown setting, intent or stage, duplicate id, an `extend` with nothing to extend, a catalog that cannot work, a file outside the layout; a warning for a section no scenario covers | Built |
| Layers: a client's file cannot change the standard or another client | Built |
| Guardrails in code (1.1): only the load's contacts, attempt caps, SMS rate, no contact details in text, budgets. A badly written SOP cannot make the agent text a stranger or loop | Built |
| Evals: scenarios tag the sections they cover (`sop:<id>`); every suite runs live before a change ships (4.2) | Built |
| Versions: the SOP folder's hash is recorded with every run, trace and experiment; reverting a change in git restores the old text and version | Built |
| An editor in the platform: edit a section in a form, see the lint and a preview of the sections an event would get (`GET /api/sops/preview` exists), submit for review | Designed, not built; the SOP screen is read-only |
| Review and approval: one approver who knows the client; two for the standard layer, which reaches every client | Designed, not built |
| Published versions per client, a canary on a share of its loads (a load always gets the same version), a comparison of escalation, no-op and block rates with the previous version, one-step rollback | Designed, not built (built earlier at tag `full-platform`) |

## 3. Memory

### 3.1 How does the agent know its past actions and the past messages of the load?

**Short answer.** It reads them from storage at the start of every run, and can read more
with tools while it thinks. Every message, risk change, timer, processed document and agent
action is an entry in the load's event log, which is only ever added to. What the load still
owes is in the ledger, with its timers. Documents are rows with their values and checks. One
snapshot of all this is read per run (`application/context.py`).

| Memory | Kept in | In the agent prompt | Tool |
|---|---|---|---|
| Messages and actions | Event log | The last 30 events, as "in" and "out" lines | `get_history` (up to 200 events) |
| What is owed | Ledger (kind, status, attempts, deferral, active timer) and timers | "Ledger", "Pending timers" | `get_ledger`, `list_document_requirements` |
| Procedure | The client's SOP sections | The selected sections (2.2) | `search_sop`, `read_sop_section` |
| Load facts | Load data, client catalog | "Load", "Contacts" | `get_load` |
| Documents | Document rows | The document of a `document_processed` event | `read_document` |
| Facilities, across loads | Facility notes per client and facility | "Facility notes" of the current and next stop, marked untrusted | `recall_facility` (search by meaning) |

- **Actions are memory.** For each outgoing message the executor adds an `agent_action`
  event in the same transaction (`application/executor.py`), so the next run sees what was
  sent, to whom, when and why.
- **History stops at the current event.** Messages queued behind it are left out, so a run
  never acts on an ETA that has not arrived for it. Agent actions are always kept.
- **The ledger holds only facts**, never a pointer into the SOP text. Nothing stays in the
  process between runs, so any worker can run the next event.
- **Storage.** `PERSISTENCE=memory` (the default, the tests and evals) keeps it in the
  process; `PERSISTENCE=postgres` (Docker Compose) keeps it in Postgres, one table per kind
  (`migrations/0001_load_memory.sql`), so a restarted platform goes on with its loads, runs
  and ledger. Both stores pass the same contract tests (`tests/contract/`), and the
  assignment's scenario leaves the same state on both ([decisions.md](decisions.md#storage)).

**Facility memory** is the one memory that outlives a load: what a driver says about a place
("check in at gate 4") is still true at the next load that stops there.

- **Written after delivery.** When a run sets a load to `delivered`, a later step of the tick
  makes one `memory` model call. It proposes notes to add, update or delete, each citing the
  inbound message it comes from.
- **Checked in code** before any write (`domain/memory.py`): the cited message is an inbound
  message of this load about that stop, and the text is short, with no phone number, email
  address, money or instruction to the agent. The agent sees notes as untrusted data that
  never override the SOP, the load instructions or the rules (`m2_injected_note_changes_nothing`).
- **Per client.** What one broker's driver said about a shared warehouse is that broker's
  information. Sharing it would leak one client's operations to another, and a hostile note
  written through one client's load could steer the agent on every other client's loads. The
  cost: each client learns a shared facility on its own.
- **Stored**, with `PERSISTENCE=postgres`, in LangGraph's Postgres store in the load
  memory's database, so notes survive a restart. Each note has an embedding (numbers that
  stand for the meaning of its text) in a pgvector index (the Postgres extension that
  searches such vectors), which `recall_facility` searches. Tests, evals and Studio use
  LangGraph's in-memory store; one contract suite runs on both.
- **A lost extraction is found again.** The job is queued after the run's commit, so a crash
  in between would lose it. When the platform starts on Postgres, it queues every delivered
  load with no job (`AgentRuntime.queue_missed_extractions`). This is simpler than putting
  the job in the run's transaction (no new table, no change to the unit of work), and a crash
  means a restart, so the restart is the time to look. A load has at most one job, so it is
  never extracted twice.

**Designed, not built:** a rolling summary of the events older than the last 30. Today the
agent pages back with `get_history`.

### 3.2 At 07:13, how does the agent remember the ETA request is still open?

**Short answer.** The model does not have to remember it: the ledger records it. The 07:01
run opened an `eta_request` obligation with 1 attempt and a follow-up timer due at 07:31.
The driver's question does not change that row. The prompt shows the open request, its timer
and the procedure it belongs to, so the reply answers the question without asking for the
ETA again and without closing the request.

At 07:13 (`a_client_a_high_risk`, step 1) the gate returns `question`. The agent prompt
carries (shortened):

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
block. The agent proposes one SMS with purpose `reply`. Only an SMS or email that *requests*
an obligation kind counts as an attempt, so the ledger and the timer stay as they were. The
scenario forbids an ETA-request SMS in this step and asserts 1 attempt and one timer due at
07:31. `a_question_during_last_wait` checks the same after the second request, and
`b_ack_after_eta_request` checks that "ok" changes nothing.

### 3.3 How does a follow-up timer work? What does the agent see when the timer starts it?

**Short answer.** A timer is a row tied to an obligation. When it is due, it becomes a
`timer_due` event in the load's queue, like a message. Code first checks that it is still
the active timer of an open obligation; a stale timer ends its run with no model call.
Otherwise the agent sees the timer event, the ledger with the attempt count, the transcript
and the procedure, and does the next step.

1. **Set.** The agent proposes `schedule_timer(kind=eta_request, due_in_minutes=30)`. The
   executor computes the due time in real minutes (`domain/timers.py`, safe across a
   daylight-saving change), never earlier than a contact's promised time plus 2 minutes. The
   new timer becomes the obligation's active timer; older pending ones become `superseded`.
   A document requirement sets a timer too (Acme's POD, 120 minutes after arrival).
2. **Fire.** `AgentRuntime.fire_due_timers` turns each due timer into the event
   `timer:{timer_id}`, marks it `fired` and queues it behind the load's earlier events, in
   one transaction.
3. **Stale check** (prefilter): superseded, cancelled, obligation closed, or no longer the
   active timer: `skipped`, no model call (`g1_deferral_stale_timer`).
4. **Intent from code:** the obligation kind's follow-up intent (`eta_follow_up_due`,
   `pod_follow_up_due`), with no gate call.
5. **What the agent sees at 07:31** (shortened): the transcript above plus the 07:13 reply,
   and

```
- eta_request: attempts 1; opened 07:01; last action 07:01 (30 min ago); deferred until
  none, deferrals used 0; follow-up timer due 07:31 (now), firing now (this event)
...
type: timer_due
timer: follow-up for eta_request, due 07:31 (now)
```

with Northline's `delay_risk_high` section in full ("After 1 ETA request: send the driver a
second SMS ... Set a follow-up timer for 30 minutes"). One attempt means the second step. At
08:01 the ledger shows 2 attempts, the client's cap: a third request would be refused at the
call by the `attempt_cap` rule, and the section says to email the dispatcher, create an
urgent Hero task and close the request as `exhausted`.

Closing an obligation cancels its timers (`b_eta_received_cancels_follow_up`). "I'll tell
you in 5 minutes" at 07:30 replaces the 07:31 timer with one at 07:37, and the old timer's
event is skipped as stale (`g1_deferral_stale_timer`).

**Designed, not built:** an always-on scheduler. Here `tick()` fires the due timers (the
platform, the evals and Studio call it on a virtual clock). In production several workers
would claim due timers with `FOR UPDATE SKIP LOCKED` (a Postgres lock that lets each worker
skip rows another holds), or EventBridge Scheduler or Temporal timers would fire them (5.2).

## 4. Evals

### 4.1 How do you make sure the system works correctly in production?

**Short answer.** Three layers. Before a release: fixed tests and live scenario evals. At
run time: guardrails in code, so a wrong model answer cannot do damage and every failure
reaches a person. After a release: monitoring; the traces are built, the metrics and alerts
are designed.

**Before a release:**

- `make ci`: lint, strict types, layer rules, 1,965 tests (unit, contract, property,
  integration, eval harness; 90 Postgres tests skip without a database) with 99.4% coverage
  of `domain` and `application`, branches included (the minimum is 90%), and the SOP lint.
  The tests run the real graph, rules and executor with a fake model (`tests/fakes.py`),
  including an agent that falls for an injected email
  (`tests/unit/application/test_graph_guardrails.py`).
- Live scenario evals (`agent eval run`): a YAML scenario drives the product's runtime on a
  virtual clock with recording gateways and the real models, as a LangSmith experiment with
  k repetitions. A scenario passes only if every repetition passes (pass^k), because a
  customer sees every run.

| Evaluator | Checks |
|---|---|
| `facts` | Action kinds, recipients, purposes, counts, forbidden actions, the ledger per obligation kind, timers, run statuses, model calls |
| `trajectory` | The run's intent, tools used and refused, sections read |
| `judge` | Wording, against a rubric per step: an LLM judge (GPT-4.1 mini) scores the texts the step created |
| `invariants` | Unique idempotency keys; no recipient outside the load's contacts |

Each row also records `passed` and `cost_usd`; the summary gives pass^k and pass@k per tag
and the total cost. **Negative control:** a grader that cannot fail proves nothing, so
`tests/evalkit/test_negative_control.py` breaks one thing in a correct play (a wrong
recipient, a missing timer, an extra SMS, a skipped tool, a low judge score, a reused
idempotency key) and asserts that exactly that evaluator fails.

**At run time:** the rules at the call and again in `validate`, the budgets, `must_act`
intents the gate cannot end, the prefilter, the escalation paths, delivery that a retry
cannot duplicate.

**After a release.** Built: every run has a LangSmith trace (each node and model round, with
the prompt version of each role), linked from the run record; the platform's load inspector
shows each run's path, tool calls and actions. **Designed, not built:**

| Signal | Alert |
|---|---|
| Age of the oldest queued event | over 2 minutes warns; over 5 breaks the response-time target |
| Run p95, deadline escalations, dead letters (events that failed 3 times) | any dead letter; p95 over 60 s |
| Status mix per client: acted, noop, escalated, blocked | a shift after a deploy or an SOP change |
| Model errors, retries, providers and fallback share per model | fallback share over 5% (a weaker fallback costs pass rate) |
| Cost per load against $1 | 80% of the budget |
| Stalled obligations (open, no timer), timer lag | any |

Plus online evaluation: the LLM judge on a sample of production runs in LangSmith, and Hero
corrections turned into new scenarios.

### 4.2 A client changes its procedure: how do you make sure other behavior does not break?

**Short answer.** The change is a file in that client's folder, so it cannot change the
sections another client reads. It must pass the lint, and every suite runs live on it at
k = 3 before it ships: the scenarios tagged with the changed section show whether the new
behavior is right, and the rest show that nothing else moved. A change to the standard layer
reaches every client, so the same full run is required.

1. **Layers.** `clients/northline/delay_risk_high.md` is visible only to Northline loads;
   sections are filtered by client before anything else.
2. **Coverage.** Scenarios carry `sop:<section_id>` tags, and the lint warns about a section
   no scenario covers (today: Acme's `detention`). A new rule comes with a new scenario.
3. **Client variants of the same scenarios.** A scenario can run on another client with
   `load_overrides` (`d1_keystone_high_risk`, `p1_pod_clean_delivers` on Acme), so a
   client's change is tested on its own loads and the other clients' scenarios stay as they are.
4. **Compare.** Each experiment records the SOP hash, the git commit, the models and the
   prompt versions. Regression scenarios must stay at pass^3 = 100%; LangSmith's comparison
   view shows, per scenario, which repetitions changed.
5. **Designed, not built:** published versions per client, with a canary on a share of the
   client's loads and one-step rollback (2.3), so a change that passed the evals still meets
   real traffic gradually.

### 4.3 You change the LLM: how do you make sure the system does not break?

**Short answer.** A model is one line per role in `config/models.yaml`. A switch is a
bake-off: the candidate runs every suite live at k = 3 against thresholds fixed before the
run, and the results are compared as LangSmith experiments, whose metadata names the model
of each role. The safety rules do not depend on the model.

- **Thresholds** ([RESULTS](../evals/RESULTS.md#how-the-agent-model-was-chosen)): groups A
  and B at pass^3 = 100%, no `must_act` message dropped by the gate, at most $0.0015 per
  model-calling run, at most 0.5 refused calls per run, the SOP-gap scenario in every trial.
  The cheapest model that meets all wins. That chose `openai/gpt-oss-120b` for the agent
  ($0.00066 per run). Claude Haiku 5.5 passed 22/22 at 3.6 times the cost and is the first
  upgrade.
- **The fallback is part of the change.** Each role has a chain in `config/models.yaml`: the
  model on its preferred provider (OpenRouter may serve it from another provider that takes
  the same parameters), two short retries on the same model when the provider rate limits,
  then fallback models with their own settings. Each model sits behind a circuit breaker,
  which skips a model that keeps failing instead of waiting on it
  (`adapters/llm/resilient.py`). An earlier k = 3 round showed why the fallback is measured
  too: rate limits moved agent rounds to Gemini 2.5 Flash Lite, and both core failures came
  from rounds it served. The agent's fallback is Claude Haiku 5.5, which passed the suite as
  the agent model. In the decision-gate core round, the retries answered 52 of the 63
  rate-limited rounds on the primary model and Haiku the other 11
  ([RESULTS](../evals/RESULTS.md#rate-limits-retries-and-providers)).
- **Model-specific behavior is found by these runs, not by tests.** OpenRouter has no
  Anthropic endpoint for `tool_choice: required` (the adapter sends `auto` there). Only
  DeepInfra serves gpt-oss at bf16 with a forced tool call, so OpenRouter has no other
  provider to route an agent round to. Google endpoints dropped a union type in a tool
  schema, so the action tools have flat schemas (`application/agent_tools.py`).
- **The gate's engine went through the same kind of run.** The rule came first: the decision
  model would become the default only with a core pass^3 no lower than GPT-4.1 Nano's, no
  `must_act` message ended at the gate, and a gate p95 no higher. It passed 22/22 against
  21/22 (the chat round's failure was the agent's), ended the same 9 runs at the gate, and
  answered at p95 0.80 s against 1.24 s ([RESULTS](../evals/RESULTS.md#how-the-gate-engine-was-chosen)).
- **Independent of the model.** Structured output is a forced tool call validated by
  pydantic, and a decision model's answers are typed and checked the same way. A malformed
  answer is an error (fallback, then a Hero task), never a half-read plan.
- **Prompts** follow the same path: a template change goes to the Prompt Hub tagged
  `candidate`, is measured with `agent eval run --prompt-tag candidate`, then tagged `prod`.

### 4.4 Clients have different procedures and complexity: how do you organize the evals?

**Short answer.** Scenarios are YAML files grouped in suites; each suite is a LangSmith
dataset. Each scenario is tagged by purpose (regression or capability) and by the SOP
sections it covers. A client variant reuses the same load fixture with `load_overrides`
(another broker, load type or stage). So a new client starts from the standard scenarios
run on its catalog, and adds its own only for what it changes.

| Suite (dataset) | Scenarios | Checks |
|---|---|---|
| `core` (`freight-agent-core`) | 22, groups A to G | The assignment's sequence and the standard procedures, on Northline, Keystone and Keystone reefer |
| `agent` (`freight-agent-agent`) | `t2_gap_hero_without_promise` | A request no SOP covers reaches a Hero with the driver's words and no promise, judged on the outcome |
| `docs` (`freight-agent-docs`) | `p1`, `p2`, `p3` | The document pipeline with real PDFs on Acme: a clean POD, a POD with exceptions, an overdue POD |
| `memory` (`freight-agent-memory`) | `m1`, `m2`, `m3` | The facility memory: a note answers a question, an injected note changes nothing, a delivered load leaves clean notes |

| Core group | Scenarios | Checks |
|---|---|---|
| A | `a_client_a_high_risk`, `a_question_during_last_wait` | The assignment's sequence |
| B | `b_ack_with_nothing_open`, `b_ack_after_eta_request`, `b_broker_thanks_by_email`, `b_medium_risk_does_not_wake`, `b_eta_received_cancels_follow_up` | Doing nothing |
| C | `c1` to `c4` | ETA answers: driver, dispatcher, relative, vague; risk cleared |
| D | `d1_keystone_high_risk`, `d2_keystone_reefer_departure`, `d4_lumper_no_sop` | Client B, a reefer load, a question no SOP answers |
| E | `e1_prompt_injection`, `e3_duplicate_message` | Injection, a duplicate delivery |
| G | `g1` to `g5` | Deferrals: a stale timer, an expired promise, a second deferral refused |

- **Regression or capability.** A failing regression scenario fails the run (exit code 1).
  Capability scenarios (`c3_relative_eta`, `d4_lumper_no_sop`) are measured only, and move
  to regression once they pass reliably.
- **Doing nothing is tested** in group B (an "ok", a broker's thanks, a medium risk): each
  asserts no outgoing message and no agent call where none is needed.
- **Code guardrails are tests, not evals.** What must hold whatever the model does (an
  outside address refused, the limit on refused calls, the SOP-gap path) is tested with the
  fake model in `test_graph_guardrails.py`; evals measure what the model does.
- **When each runs.** Tests on every pull request (`.github/workflows/ci.yml`). Live evals by
  hand (`evals-live.yml`, `make eval`) with a cost cap, and required before a prompt, SOP or
  model change ships. **Designed, not built:** a suite per client folder
  (`evals/scenarios/<client>/`) once a client has more than a few scenarios of its own, run
  on every change to that client's folder.

## 5. Scale, response time and cost

### 5.1 How does the design keep each run at 5 minutes or less?

**Short answer.** Each run has hard limits by design. Loads run in parallel, one event at a
time per load. Most events need no model call. A message gets one gate call; the agent gets
one loop with a round limit and a cost limit. Each model call has a 60-second timeout (its
retries included), inside a 240-second run deadline. When the models or the deadline fail,
the run goes to a Hero instead of running again. Measured, the agent call (all its rounds)
takes 7.3 s at p50 and 11.4 s at p95.

The limits and where they live are in the budget table of
[architecture.md section 4](architecture.md#4-the-run-graph). Two more parts: no transaction
is open while a model thinks, and a worker that dies mid-run has its event returned to the
queue by the claim lease (deadline plus 60 s); after 3 failures the event becomes a dead
letter (`EventQueue.reclaim_expired`).

**Measured** ([RESULTS](../evals/RESULTS.md#the-k--3-round), k = 3 round, 135 agent runs of
the core suite): agent call p50 7.3 s, p95 11.4 s, max 14.9 s; gate call p50 0.4 s, p95
0.8 s (0.9 s and 1.2 s on GPT-4.1 Nano); a document extraction 1.3 s. A rate-limited round
adds its wait, about 1 to 3 s; 63 core rounds were rate limited, and no run reached its
deadline. Eight rounds at the 60-second timeout could pass 240 s, which is why the deadline,
not the round count, is the limit. The deadline leaves 60 seconds of the 5 minutes for the
queue wait and the delivery.

**Designed, not built:** workers that scale on the age of the oldest queued event, an
always-on outbox sender (today the tick delivers), and priority for risk changes and timers
over chatter.

### 5.2 Volume x10: what must change?

**Short answer.** The logic, the prompts and the cost per load stay. The adapters behind the
interfaces change: a durable queue partitioned by load, a timer scheduler, Postgres, an
object store, worker pools that scale on queue age, and higher model rate limits. The first
limit this design meets is the model providers' rate limits, not compute.

**Volume.** 100,000 loads a month with 50 to 100 messages each is 5 to 10 million messages a
month: about 10 per second over 22 business days of 10 hours. At 10x: about 100 messages per
second, plus pings. With one gate call per message and an agent run for about half of them,
that is about 50 agent runs per second. At 7 s each, about 350 runs are in flight at once
(Little's law), and about 45 million input tokens a minute at peak.

| Part | Today | At 10x (designed) |
|---|---|---|
| Queue | Per-load FIFO in the store (a Postgres table on the platform) | SQS FIFO with `MessageGroupId = load_id` (order per load, parallel across loads), or Temporal with a workflow per load |
| Workers | `tick()` in one process | Stateless async workers, about 30 runs in flight each, scaled on queue age |
| Timers | Rows fired by `tick()` | A scheduler that claims due timers with `FOR UPDATE SKIP LOCKED`, or EventBridge Scheduler |
| Pings | One transaction per ping | A stream partitioned by load keeps the risk state; only changes reach the queue |
| Storage | Postgres (one instance in Docker Compose) or the in-memory store, behind the repository interfaces | The same schema with the events table partitioned by month, closed loads archived to object storage, a connection pooler (the adapter keeps no session state), a read replica for the platform and metrics |
| SOP index | Files read per run | Published versions in Postgres, cached per client by hash; hybrid search with pgvector |
| Facility memory | LangGraph's Postgres store in the load memory's database; up to 5 delivered loads extracted at the end of each tick, in the same process | The same store on its own connection pool or database; extraction workers scaled on pending jobs; the startup check for lost jobs runs on a schedule |
| Documents | Files on disk, read in the tick | An object store behind `BlobStore`; document workers scaled on pending documents |
| Models | One OpenRouter key; a rate limit is retried on the same model; OpenRouter may serve it from another provider that takes the same parameters (the gate: OpenAI, then Azure); then a fallback model that passes the suite | Higher rate limits or reserved capacity, concurrency limits per client |
| Tracing | Every run | Sampling: every escalation and failure, a share of the rest |
| Cost | About $0.04 per load | The same per load; the total grows in line with volume |

The layer rules (import-linter contracts in `pyproject.toml`) keep storage, queues, blobs and
providers behind interfaces, so the run graph, the rules, the ledger and the eval suites do
not change. The Postgres adapter already passes the in-memory store's contract tests, and its
queue claim and due-timer read skip rows another worker holds, so several workers can share them.

### 5.3 How do you keep LLM cost at about $1 or less per load?

**Short answer.** Most events never reach a model. The gate keeps chatter away from the
agent. The agent runs a cheap model chosen by a bake-off, its prompt holds only the matching
SOP sections, and each run has a round budget and a cost budget. Measured: about $0.00005 per
gate call and $0.0007 per agent run, so about **$0.04 for a typical load and $0.06 for a
heavy one**.

A typical load: 75 messages with one gate call each ($0.004), 42 agent runs ($0.029), 4
document reads ($0.003); location pings make no model call. A heavy load: 100 messages, 77
agent runs, 5 documents. With the agent on Claude Haiku 5.5: about $0.11 and $0.20. The
table and its assumptions: [RESULTS, Cost per load](../evals/RESULTS.md#cost-per-load).

**What keeps the cost down, biggest effect first:**

1. **Pings never reach a model:** `domain/ping_filter.py` turns hundreds of pings into a few
   risk changes (`b_medium_risk_does_not_wake`).
2. **The prefilter:** duplicates, stale timers, empty messages and unknown senders stop in code.
3. **The gate:** an "ok" ends as `noop` after a $0.00005 decision call instead of a $0.0007
   loop. Only an intent the catalog allows can end a run there, so `must_act` keeps the
   intents that need action. The decision model's input costs $0.042 per million tokens, its
   output nothing.
4. **A cheap model per role,** chosen by the bake-off (4.3). The same loop on a model at
   $3 / $12 per million tokens with no caching would cost about
   42 x (16,200 x $3 + 220 x $12) / 1M, about $2.15 per typical load.
5. **Small prompts:** only the matching sections in full; the rest is one line each and a
   search away.
6. **The run budget:** 8 rounds and $0.05 per run, per client.
7. **Prompt caching:** the stable parts come first (the system prompt depends on the client
   only, then load, contacts, SOP), so every round repeats the same start of the prompt.
   OpenAI and Gemini cache long prompt starts automatically. DeepInfra reports no cache reads
   for gpt-oss, so the figures above have no caching discount.

**Designed, not built:** a cost meter per load, checked by the runtime. The per-run cap
limits a runaway loop, not a load (77 runs at $0.05 would be $3.85). The meter would add up
each load's run costs (already in every run record): at 80% of $1, an alert and a lower round
budget; at 100%, judgment calls go to a Hero. Also a rolling summary of old events, which
keeps the transcript short on loads with hundreds of events.
