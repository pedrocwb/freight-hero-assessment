# Answers to the assignment's questions

Each answer starts with the short answer, then the details and the trade-offs. The
diagrams are in `docs/architecture.html`, and the shared contracts are in
`docs/decisions.md`.

**Conventions.** Python paths leave out `src/freight_agent/`: `application/runtime.py`
means `src/freight_agent/application/runtime.py`. Other paths start at the repository
root. Scenario ids name files in `evals/scenarios/<suite>/`. **Not built yet** marks a part
that is designed but not in the code. Measured numbers of the agent loop come from the
wave 3 bake-off of 2026-10-20 (`evals/RESULTS.md`, "Wave 3: the agent loop").

| Built | Not built yet |
|---|---|
| Brokers as data (catalog, versions, canary by load), the knowledge base (SOP upload, PDF pages, compiled drafts, review, publish, versions, hybrid search), the agent's tool loop inside the guardrails, the document pipeline (blobs, pages, extraction, checks, `document_processed`), generic obligations, domain rules and the validator, the run graph and `AgentRuntime` with a claim lease, the in-memory and Postgres stores (one contract suite per port), the OpenRouter, cassette, resilient, scripted and per-role models, the composition root, the LangSmith tracer, operational metrics, the eval harness (spec, runner, fact and trajectory graders, LLM judge, k-trial suites, per-broker suites, scenario drafts, negative control), the core, agent and docs suites, the `agent` CLI, the simulation platform (API and web UI, broker and SOP screens), Docker compose | The always-on worker and scheduler, an object store for blobs (files today), the automated comparison of a canary with its previous version, enforcement of the per-load cost cap in the runtime, a rolling summary of old events, a Telegram SMS channel |

`make eval` runs every suite (core, agent, docs) in scripted mode; `evals/RESULTS.md` has
the live rounds on real models that chose the models in `config/models.yaml`.

## 1. Agent architecture

### 1.1 What is the architecture of your agent? What are the alternatives?

**Short answer.** One stateless run per event, with a per-load FIFO queue. Code does every
deterministic step: it reads the load's memory, drops what needs no thought, finds the
event's intent when the event says it, selects the SOP sections by rules and checks every
action. The model works inside one bounded tool loop: it reads what it needs (SOP search
and sections, history, ledger, load, documents) and proposes actions one at a time. Each
proposal is checked by the rules at the call and answered "accepted" or "refused by
<rule>"; nothing runs until the run commits, and then only through the outbox. Memory is in
our storage, and each broker's procedures, intents and limits are data.

| Step | Code | Model call | Exits |
|---|---|---|---|
| load context | `application/context.py` (also picks the broker's config version, 2.3) | none | |
| prefilter | `application/prefilter.py` | none | `skipped`, or `escalated` (unknown sender) |
| triage | `application/gate.py` | small model, inbound messages only; none when `Broker.triage` is off | `noop` |
| knowledge | `application/graph/nodes.py`, `SopIndex.by_applies` | none | |
| agent | `application/agent.py`, tools in `application/agent_tools.py` | one call on the `agent` role, at most `BrokerLimits.max_steps` tool rounds | `escalated` (no procedure, too many refusals, model failure) |
| validate | `domain/validation.py`, `domain/rules.py` | none | `noop`, or `escalated` (high-severity violation) |
| commit and execute | `application/executor.py` | none | `acted` |

`AgentRuntime.process_next` (`application/runtime.py`) claims the event and reads its
context in one short transaction, runs the LangGraph graph (`application/graph/build.py`)
outside any transaction under the run deadline, then applies the approved actions and
writes ledger, timers, outbox and run record in a second transaction. Delivery happens
later from the outbox, with the idempotency key `{event_id}:{index}`.

**The agent's tools** (`application/agent_tools.py`):

| Kind | Tools | What they do |
|---|---|---|
| Read | `search_sop`, `read_sop_section`, `get_history`, `get_ledger`, `get_load`, `read_document`, `list_document_requirements` | Return text from our storage and the broker's published SOP. Untrusted text (messages, documents, search results) is marked as such in the prompt. |
| Action | one per action type: `send_sms`, `send_email`, `post_broker_slack`, `write_tms_note`, `create_hero_task`, `post_internal_slack`, `update_stage`, `schedule_timer`, `defer_obligation`, `close_obligation`, `open_obligation` | Build the action and run `domain.validation.check_action` on it against the actions staged so far, with the broker's limits. Answer `accepted as action #n` or `rejected by <rule>: <reason>`. Execute nothing. |
| End | `finish(intent, summary, no_procedure)` | Ends the loop; the final answer is an `AgentOutcome` (a plan, the agent's intent, `no_procedure`). |

**The staged plan is a fold over the transcript.** The model's answer carries the whole
conversation of the loop (`LlmResponse.transcript`). `stage_from_transcript`
(`application/agent.py`) replays its tool calls through the same rules, in order, and that
result is the plan, in every mode. Scripted and replayed models never run tools, so this
is what makes replay exact: a recorded loop stages the same actions as the live one, with
no side effect. The trajectory in the run record (`RunRecord.trajectory`: tool, arguments,
result, accepted or refused, cost) is that transcript.

**Guardrails that hold whatever the model does:**

- **The model cannot act.** Action tools only stage; the executor applies the plan in the
  commit. Every action passes the rules in `domain/rules.py` (recipient is a contact,
  channel allowed, SMS rate limit, attempt cap per obligation kind, no phone or email in
  outbound text, ...) at the call and again in `validate`, which is the merge point of the
  staged actions and the answer's own actions. A rule refuses an action, never edits it.
  In `e1_prompt_injection` an email asks for the rate confirmation at an outside address;
  in `n1_outside_email_refused` the agent stages that email and the tool refuses it.
- **Budgets per broker** (`BrokerLimits`): `max_steps` tool rounds (8), `max_cost_usd` per
  run (0.05, checked by the adapter after each round, which then forces the answer) and
  `max_rejections` (3; over it the run escalates with `BLOCKED_TITLE`, and the accepted
  actions still run). `max_steps = 0` is the pre-wave-3 behaviour (one plan in the answer,
  no action tools), a per-broker safety switch.
- **No event is lost.** A model failure after fallbacks, a run past its deadline, too many
  refusals, or `finish(no_procedure=true)` with nothing accepted creates a Hero task and
  completes the event (`application/escalation.py`).
- **Deterministic where possible.** The prefilter, the intent of system events, the
  sections selected by rules, the rules themselves and the executor are code; the same
  event and state give the same selection and the same prompt text.

**Why the loop replaced the single plan.** Until wave 3 the model made two fixed calls (the
gate, then one structured "decide" plan with at most two read-only tool rounds), on a
vocabulary fixed in code. That broke on the goal of onboarding a broker without code:

| Single plan (before) | Tool loop (now) |
|---|---|
| The sections are chosen only by intent and stage; a situation no tag names is a gap. | `by_applies` still gives the starting sections; the agent searches the broker's SOP when they do not cover the event (2.2). |
| A rule dropped a bad action after the answer, silently: the run could only escalate. | The tool answers at the call ("refused by recipient_is_contact"), so the model can correct itself inside the run; the budget stops one that keeps trying. |
| Documents were a stub that created a Hero task. | The agent reads a processed document with its checks (`read_document`) and acts on it (1.2). |
| Intents, obligations and limits were constants. | They are the broker's catalog (2.1), rendered into the prompts. |

The price is more tokens per run (each round resends the prompt) and less predictable
trajectories. Both are bounded (budgets, the rules) and measured (trajectory checks and a
judge in the evals, 4.1; cost per run in the bake-off, 5.3).

| Alternative | Why not |
|---|---|
| The single structured plan | Kept as `max_steps = 0` per broker. As the default it cannot search, read a document on demand or learn from a refusal. |
| A ReAct agent whose tools execute | The model acts directly: an injected email can trigger a send, and a half-finished loop leaves half-applied state. Our tools stage; the commit applies all or nothing. |
| The full SOP and history in every prompt | A 100-page SOP is about 50,000 to 70,000 tokens; on about 80 events per load it breaks the $1 budget. |
| SOPs compiled into a state machine | Every SOP change needs an engineer. We keep a state machine only for obligations. |
| Several agents (per stage or role, or a supervisor) | Each handoff adds a model call and loses context; the stage is already an SOP key. |
| A durable workflow per load (Temporal) | A way to deploy this pipeline (5.2), not another agent design. |

### 1.2 How does the agent read documents, find their type and get their key values?

**Short answer.** A pipeline reads every attachment before the agent sees it. The file is
stored as a blob, its pages are read (the text, or the page as an image when the text is
too thin, as on a scan or a phone photo), and one `extract` model call classifies it and
fills the schema of its type from the broker's catalog. Code checks the values against the
load. The result is saved and becomes a `document_processed` event; that run's agent reads
the document and follows the SOP. A failed check sends the document to review.

| Step | Code |
|---|---|
| 1. A message with attachments is ingested: each attachment is saved as a `Document` with status `pending`, in the message's transaction. The platform's upload stores the bytes first (`POST /api/loads/{id}/attachments`). | `AgentRuntime.ingest_message`, `adapters/documents/fs_blobs.py` (`BLOBS_DIR`; content-addressed `sha256:<hex>`) |
| 2. Each `tick` reads pending documents before it runs the queue. | `DocumentProcessor.process_pending`, `application/documents.py` (`DocumentPipeline`) |
| 3. Pages: the text of each page; a page with little text is rendered as a PNG. | `adapters/documents/pdfium.py` (`PdfiumPageReader`, pypdfium2: Apache/BSD, not the AGPL PyMuPDF) |
| 4. One `extract` call classifies and extracts with the broker's `DocumentTypeDef` schemas, page images included. | `application/prompts_documents.py`; role `extract` in `config/models.yaml` |
| 5. Checks in code, per `FieldDef.check`. | `application/documents.py` |
| 6. Status: `processed` (every check passed), `needs_review` (a check failed, a required field is missing or the confidence is low) or `failed`. One `document_processed` event (`doc:{document_id}`) is appended and queued, once. | `domain/documents.py`, `domain/events.py` |
| 7. A clean `processed` document of a requirement's type closes its obligation as `satisfied` in the same transaction (a POD closes `pod`); `needs_review` leaves it open. | `application/documents.py` |
| 8. The `document_processed` run: the agent reads the document with `read_document` and follows the SOP (a clean POD moves the stage to `delivered` and writes a TMS note; `needs_review` creates a Hero task with the values). | `sops/standard/documents.md` |

The standard catalog's document types (`standard_broker()` in `domain/broker.py`); a broker
adds its own types and fields as data:

| Type | Key values |
|---|---|
| `bol` | BOL number, shipper, consignee, PO numbers, seal number, weight, pickup date, signatures |
| `pod` | receiver (the consignee facility), PO numbers, signature, delivery time, exceptions (shortage, damage, refused) |
| `rate_confirmation` | carrier and MC number, rate and line items, stops and appointments |
| `receipt` | kind (lumper, scale, fuel), amount, date, vendor |

Checks against the load (`FieldDef.check`): `po_number` (on load 481207 the PO is in the
instructions, `PO 55821`), `mc_number` (the carrier's), `stop_name` (a stop, the shipper or
the consignee), `date_in_window` (the appointment's day or window), `seal_number` (the one
the driver reported), `signature` (present). A POD with the wrong PO goes to review
(`p2_pod_exceptions_needs_review`).

**An attachment is handled once.** The message's own run never acts on the document (the
prompt and `sops/standard/documents.md` say so); the `document_processed` run does. Since
the tick reads documents before it runs the queue, that event queues behind the message.

**Documents the load owes.** A `DocumentRequirement` (for example `pod` at `at_delivery`
within 120 minutes, from the Acme SOP) opens its obligation when the agent moves the stage
there, with a follow-up timer; `pod_follow_up_due` then asks the driver for the POD, and
`p3_pod_overdue_follow_up` checks it.

The fixtures are real PDFs (`evals/fixtures/docs/`: a clean POD, a POD with exceptions and
the wrong PO, a scanned POD with no text layer, a BOL) with the expected values in
`*.expected.json`. The `extract` model (`google/gemini-2.5-flash`, fallback
`openai/gpt-4.1-mini`) read the clean POD and the POD with exceptions correctly in every
live trial of the docs suite (`p1`, `p2`, both bake-off rounds); the scanned POD and the
BOL are covered by the unit tests with a scripted extraction only. OCR (AWS
Textract) plus a text model would be cheaper on digital PDFs but weaker on phone photos.

## 2. Instructions

### 2.1 How do you organize the SOPs of many clients?

**Short answer.** Per broker, as data. A broker is a record with its catalog (intents,
obligations, document types and requirements, limits) and its published SOP version; the
sections inherit from a standard layer, and a broker writes only what it changes. Both are
versioned, so a change reaches a share of the broker's loads first.

**The catalog** (`domain/broker.py`, stored by `ports/brokers.BrokerRepository`):

| Part | What it holds |
|---|---|
| `IntentDef` | What an event can mean: id, title, description (the gate prompt's text), examples, `priority`, `must_act`, `noop_eligible`, `system` (only from code) |
| `ObligationDef` | What the load owes: `kind` (`eta_request`, `pod`, ...), `follow_up_minutes`, `max_attempts`, its follow-up intent, `on_exhausted` (dispatcher email, Hero task, broker Slack) |
| `DocumentTypeDef`, `DocumentRequirement` | The documents the broker reads, their fields and checks; which stage requires which document, within how long |
| `BrokerLimits` | The validator's limits and the loop's budget (1.1) |
| `Rollout` | The share of loads on the current version (2.3) |

Every broker starts as a copy of `standard_broker()`: 12 intents (8 for messages, with the
gate prompt's descriptions word for word, and 4 from code: `delay_risk_high`,
`delay_risk_low`, `eta_follow_up_due`, `document_processed`), the `eta_request` obligation
(30 minutes, 2 attempts, then the dispatcher email and a Hero task) and the 4 document
types. A load whose broker was never saved runs on the standard catalog.

**Intents per broker.** A broker's new situation is a new `IntentDef`, not an engineer's
change: the triage prompt lists the catalog's message intents, `must_act` decides whether
the agent always runs, and the SOP sections name the intents they cover. The Acme SOP
(`evals/fixtures/sops/`) brings `detention_request`; its draft proposes it (2.3).

**The sections** keep three layers, now per published version: standard, broker, broker
plus load type. Each section has a stable `id`; `effective_sections` in `domain/sop.py`
merges the layers per id, from general to specific:

- **`replace`** swaps body and header. Northline (Client A) and Keystone (Client B) each
  replace the standard `delay_risk_high`.
- **`extend`** appends its body and keeps the header. Keystone's reefer layer adds "ask for
  the trailer temperature and the seal number" to `driver_departed`
  (`d2_keystone_reefer_departure`).

A run reads only the standard layer and its own broker's layers; the index never returns
another broker's sections (`ports/knowledge.SopIndex`). The 60 to 70% that brokers share is
written once.

**Where they live.** Published versions are rows (`SopStore` on the unit of work:
documents, the broker's one draft, immutable versions keyed by content hash; Postgres
tables from migration 0004, sections with their embeddings). The `sops/` folder is the seed
and the editor's plain-text format: `agent sop import sops/` turns it into the standard,
`northline` and `keystone` versions, idempotently. With `PERSISTENCE=memory` the runs read
the `sops/` files directly, as before.

### 2.2 How does the agent find the correct SOP files for the current event? What are the advantages and disadvantages of your method?

**Short answer.** Rules first, then search. The event's intent (from code for system
events, from the triage model for messages), the load's stage, the broker and the load
types select the sections whose `intents` and `stages` match, as before wave 3, and the
agent gets them in full with a one-line catalog of the rest. When they do not cover the
event, the agent searches the broker's SOP with `search_sop` and opens a section with
`read_sop_section`. If nothing covers it, it finishes with `no_procedure` and the run goes
to a Hero.

1. **By rules** (`SopIndex.by_applies` = `resolve` in `domain/sop.py`, in the `knowledge`
   node): merge the broker's layers; keep the sections whose `intents` include the intent
   and whose `stages` include the stage (empty means all), so `communication_rules` and
   `escalation` always apply. Add, as context, the sections of procedures still in
   progress (the follow-up intent of each open obligation).
2. **By search** (`SopIndex.search`, the agent's `search_sop` tool): hybrid retrieval over
   the sections the broker sees. In Postgres, pgvector cosine distance on the section
   embeddings (`openai/text-embedding-3-small`, 1536 dimensions, computed when a version is
   published) and full-text rank (`tsvector`), fused by reciprocal rank
   (`adapters/knowledge/postgres.py`); in memory, word overlap. A query is embedded only on
   a live search.
3. **No procedure:** `finish(no_procedure=true)` with no accepted action escalates ("No SOP
   for this situation", `sop_gap` in `application/escalation.py`), as `d4_lumper_no_sop`
   and `t1_gap_search_then_hero` check.

The run record keeps `sop_section_ids` (selected by rules) and `sections_searched` (found
or read by the agent), so each decision shows what it followed.

| Advantages | Disadvantages |
|---|---|
| The common path is deterministic: the same event and state select the same sections, so a scenario can assert the procedure. | A misclassified message gets the wrong starting sections. Mitigated: system events skip the model, `must_act` intents always reach the agent, and the agent can search. |
| Small prompts (about three sections, not 100 pages): low cost and latency, no unrelated rules. | Search ranking is not exact, so it is not asserted by score: trajectory checks assert which tools and sections a run used (`tools_used`, `sections_read`, 4.1). |
| A situation no tag names is searched before it is escalated. | The editor still tags sections with the broker's intents and stages; the publish lint checks them (2.3). |
| No leaks between brokers: the index filters by broker before it ranks. | Embeddings need a live call at publish; offline runs use a deterministic fake (`FakeEmbedder`), since tools never run there. |

### 2.3 People without a technical background write and change the SOPs. How does your design make this safe?

**Short answer.** The editor uploads the broker's own document as it is (a PDF in no
particular format), reviews a draft the system compiles from it, and publishes. Publishing
lints the draft, stores an immutable version and gives the broker a new config version,
which can reach a share of its loads first. The broker's eval suite runs on it, every run
records the version it followed, and the guardrails in code hold whatever the text says.

**Onboarding without code** (`application/sop_ingestion.py`, `api/routes/knowledge.py`,
the SOP Studio screen of the web UI):

1. **Upload** (`POST /api/brokers/{id}/sops`): the file is stored as a blob and its pages
   are read (`PageReader`; a thin page goes as an image).
2. **Compile** (role `compile`, one structured call): sections with a title, a one-line
   description, the body **verbatim** from the document, proposed `intents` and `stages`,
   the source pages; plus proposed new intents (with the evidence), document requirements
   and obligations. From `acme_sop.pdf` the draft proposes `detention_request` and a `pod`
   requirement at delivery within 120 minutes.
3. **Review** (`GET` and `PUT .../sops/draft`): the editor accepts or rejects each proposal
   and edits the sections; the draft shows the problems a publish would report now.
4. **Publish** (`POST .../sops/publish`): the lint refuses a section whose `applies` name
   an unknown intent or stage, and an intent with no section; the sections are embedded
   and hashed into an immutable `SopVersion`; the broker is saved with the accepted
   intents, requirements and obligations, `sop_version`, `config_version + 1` and a rollout.
5. **Evals and canary** (below).

| Safeguard | State |
|---|---|
| The broker's document as it is, compiled into a draft a person reviews; section bodies are verbatim, so the agent follows the broker's words, not a summary. | Built |
| Lint with plain-English messages. For the files: bad header, unknown setting, intent or stage, a duplicate id, an `extend` with nothing to extend, a section no scenario covers (`adapters/sop/lint.py`, `agent sop lint`, a CI step). For a draft: the same rules plus every intent with a section. | Built |
| Layers: a broker's change cannot touch the standard or another broker. | Built |
| Versions: a `SopVersion` is immutable and keyed by its content hash; every run records the broker, its `broker_config_version` and the `sop_version` it read, and the LangSmith trace carries them. | Built |
| **Canary per broker:** `Rollout.percent` of the broker's loads run on the new config version, the rest on the previous one; `choose_version` uses a sha256 of the `load_id`, so a load keeps its version across runs. Rollback is `percent: 0` (`POST /api/brokers/{id}/rollout`). | Built; comparing the canary's escalation, no-op and block rates with the previous version's is a query over the run records, **not built yet** as a report |
| Required evals: the broker's suite on its new version (`POST /api/brokers/{id}/evals/run`, `agent eval run --broker-file`); `agent eval lint` fails on an intent without a scenario; `agent eval draft` drafts scenarios from the sections for the editor to complete. | Built as commands; making a passing suite a condition of publish is a setting, **not built yet** |
| Review: a person who knows the broker approves the draft. A standard-layer change reaches every broker, so it needs a second approver. | Designed; today the platform has no login |
| Guardrails in code (`domain/rules.py`): attempt caps per obligation kind, an SMS rate limit, only the load's contacts on their channels, no contact details in outbound text, the loop's budgets. | Built |

## 3. Memory

### 3.1 How does the agent know its past actions and the past messages of the load?

**Short answer.** It reads them from storage at the start of every run, and can read more
with tools while it thinks. Every message, risk change, timer event, processed document and
agent action is an entry in the load's append-only event log; open obligations and timers
are rows in a ledger; documents are rows with their extracted values and checks.
`load_run_context` (`application/context.py`) reads one snapshot that every step uses.

| Memory | Store | In the agent prompt | Read with a tool |
|---|---|---|---|
| Episodic | Event log: messages, risk changes, timer events, processed documents, the agent's actions | Transcript of the last 30 events (`recent_events_limit`), "in" and "out" lines | `get_history`: further back, on demand |
| Prospective | Ledger: obligations of every kind (status, attempts, deferral, active timer) and timers | "Ledger" and "Pending timers" | `get_ledger`, `list_document_requirements` |
| Procedural | The broker's published SOP version | The sections selected by rules (2.2) | `search_sop`, `read_sop_section` |
| Reference | Load metadata, the broker's catalog | "Load", "Contacts" and the intents and obligations of the broker | `get_load` |
| Documents | `Document` rows: type, extracted values, checks, status | The processed document named by a `document_processed` event | `read_document` |

- **Actions are memory.** For each outbox item the executor appends an `agent_action`
  event (`act:{event_id}:{index}`) in the same transaction (`application/executor.py`),
  so the next run sees what was sent, to whom and why.
- **History stops at the current event.** Messages queued behind it are left out, so a run
  never acts on an ETA that has not arrived for it.
- **The ledger holds only facts** (`domain/obligations.py`), never a pointer into the SOP
  text, and the process keeps nothing between runs.
- **What the agent did is memory for people.** The run record keeps the trajectory (every
  tool call, its arguments, its result, accepted or refused, its cost), the agent's intent
  and the versions it followed; the inspector shows them.

The Postgres adapter (`adapters/persistence/postgres/`) and the in-memory store pass the
same contract suites (`tests/contract/`: unit of work, brokers, SOP store and index,
documents), and every scenario of every suite leaves the same state on both at every step,
also when the runs read the imported brokers and sections from Postgres
(`tests/integration/test_postgres_scenarios.py`). **Not built yet:** a rolling summary of
events older than the last 30 (the agent can page back with `get_history`).

### 3.2 At 07:13, how does the agent remember that the ETA request is still open?

**Short answer.** The model does not have to remember it: the ledger records it. The
07:01 run opened an `eta_request` obligation with 1 attempt and a timer due at 07:31. The
driver's question does not change that row, and the 07:31 timer will ask again unless a
run closes the request. The prompt shows the open request, so the reply neither asks
again nor closes it.

At 07:13 (`a_client_a_high_risk`, step 1) the ledger holds `eta_request` open, attempts
1, last action 07:01, no deferral, active timer due 07:31. The event log holds the 07:00
risk change, our 07:01 SMS with `requests: eta_request`, and the 07:12 "What's the PO
number?". The triage model sees the open obligation and the recent events and returns
`question`. The prompt carries these blocks (shortened; the excerpt is the pre-wave-3
rendering, and the agent prompt keeps the same sections, ledger and transcript blocks,
rendered from the broker's catalog):

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
are". The PO comes from the load's `instructions` (the agent may also call `get_load`).

The plan is one SMS with purpose `reply`, so it requests nothing. The executor counts only
an SMS that `requests` the obligation's kind as an attempt, so the ledger and the timer do
not change. The scenario forbids an `eta_request` SMS in this step and asserts attempts 1
and one timer due at 07:31. `a_question_during_last_wait` checks the same after the second
request, and `b_ack_after_eta_request` checks that "ok" changes nothing.

### 3.3 How does a follow-up timer work? What does the agent see when the timer starts it?

**Short answer.** A timer is a row tied to an obligation. When due, it becomes a
`timer_due` event in the load's queue, like a message. Code first checks that it is still
the active timer of an open obligation; a stale timer ends its run with no model call.
Otherwise the agent sees the timer event, the ledger with the attempt count, the
transcript and the SOP section, and does the next step.

1. **Set.** The agent stages `schedule_timer {kind: eta_request, due_in_minutes: 30}`. The
   executor computes `due_at` in real minutes (`domain/timers.py`, safe across DST), never
   before a promised time plus 2 minutes of grace. Older pending timers of the obligation
   become `superseded`. A document requirement sets one too: moving the stage to
   `at_delivery` on an Acme load opens `pod` with a timer 120 minutes later.
2. **Fire.** `AgentRuntime.fire_due_timers` turns each due timer into the event
   `timer:{timer_id}` (firing time, `data.due_at`, obligation kind), marks it `fired` and
   queues it behind the load's earlier events, in one transaction.
3. **Stale check** (prefilter, `is_stale`): superseded, cancelled, obligation closed, or
   no longer the active timer: `skipped` with no model call.
4. **Intent from code:** the kind's follow-up intent (`ObligationDef.follow_up_intent`,
   by default `<kind>_follow_up_due`: `eta_follow_up_due`, `pod_follow_up_due`), no
   triage call; the broker's SOP sections list it.
5. **Agent.** At 07:31 the model sees the event line `timer: follow-up for eta_request,
   due 07:31 (now)`, the ledger line `eta_request: attempts 1; ... firing now (this
   event)`, the transcript with the 07:12 question and the 07:13 reply, and the Northline
   section. Attempts 1 means the second step: SMS again and a new 30-minute timer. At
   08:01, attempts 2 (the kind's `max_attempts`; a third request is refused at the call):
   email the dispatcher, create an urgent Hero task, close the request as `exhausted`.
   Evals fire timers with zero latency, so these happen at 08:01, not 08:02.

Closing an obligation cancels its timers (`b_eta_received_cancels_follow_up`). "I'll tell
you in 5 minutes" at 07:30 replaces the 07:31 timer with one at 07:37, and the old timer's
event is skipped as stale (`g1_deferral_stale_timer`).

**Not built yet:** the always-on scheduler process. In this repository `tick()` fires
timers (the platform and the eval runner call it on a `VirtualClock`); the Postgres
adapter already claims due timers with `SKIP LOCKED`, so several workers can run it.
Production would call it on a schedule, or use EventBridge Scheduler or Temporal timers.

## 4. Evals

### 4.1 How do you make sure that the system works correctly in production?

**Short answer.** Three layers: tests and scenario evals before a release, which check
facts, the agent's trajectory and, with an LLM judge, the wording; code guards at run time,
so a wrong model answer cannot do damage and every failure reaches a person; and
monitoring of our own data after the release.

**Before a release.** Unit, contract and property tests (`make test`), layer rules
(`make arch`) and strict pyright. Then scenario evals: a YAML scenario (`evalkit/spec.py`)
drives the product's `AgentRuntime` on a virtual clock with recording gateways
(`evalkit/runner.py`).

| Grader | Checks |
|---|---|
| Facts (`evalkit/graders.py`) | Action kind, recipient, purpose, `requests`, counts, `forbid` lists, ledger per obligation kind, timers, run status, model calls, the agent's intent; invariants such as unique idempotency keys and no recipient outside the load's contacts |
| Trajectory | Over `RunRecord.trajectory`: `tools_used` (for example `search_sop` on a gap), `sections_read`, `max_rejections` |
| Judge (`evalkit/judge.py`) | Wording, on a rubric per step (`judge {rubric, min_score}`): a model on the `judge` role scores the agent's messages (`JudgeVerdict(score, reasons)`). It runs on the uncounted model, so it never changes `llm_calls`; in scripted mode it plays `script.judge`, or is skipped with a warning |

| Mode | Model | Use |
|---|---|---|
| scripted | `ScriptedModel` returns answers written in the scenario; `script.agent` entries may carry the loop's `tool_calls` | Tests the pipeline, the transcript fold and the graders, deterministically; not the model |
| replay | `CassetteModel` (`adapters/llm/cassette.py`), keyed by a sha256 of model id, role, prompts, images, tools, schema, tool-round limit and cost cap | Every pull request, offline. The fold makes a replayed loop stage exactly what the live one did. A changed prompt, SOP or model misses its recording and fails with `CassetteMissing`; a stale answer is never replayed. |
| live | `OpenRouterModel` behind `ResilientModel` | Releases and model changes: k = 3 trials, and a scenario passes only if all three pass (pass^3), because a customer sees every run |

**Negative control.** A grader that never fails proves nothing: each check has a case
where it must fail (`tests/evalkit/test_graders.py`), the runner fails a step with an
unused or missing script entry, and `tests/evalkit/test_negative_control.py` runs the real
suites with deliberately wrong agents (an SMS for "ok", a third ETA request, an email to
Client B's dispatcher, an ignored deferral, an agent that stages an email to an outside
address and must be refused at the call and end in `BLOCKED_TITLE`, a rude SMS the judge
must score below the rubric's minimum) and asserts exactly which scenarios fail.

**At run time:** the rules at the call and in `validate`, the loop's budgets, `must_act`
intents the triage cannot skip, the prefilter, escalation paths and idempotent delivery.

**After the release:** `application/metrics.py` computes, from our own data, queue age
against the SLA, run p95, the status mix, validator blocks, errors per model, cost per
load, timer lag, stalled obligations and dead letters, and `health_summary` gives OK,
WARN or CRITICAL. A LangSmith trace per run (`adapters/observability/langsmith.py`)
carries the broker, its config version and the SOP version, and fails open; an eval run
with `--langsmith` adds the feedback `judge_score`, `rejections` and `tools_used`.

`uv run agent eval run` runs k trials per scenario, reports pass@k and pass^k per scenario
and tag, stops at a cost cap and writes a report with the git sha, SOP version, models,
prompt hash and a comparison with a baseline (`evalkit/suite.py`, `evalkit/report.py`).
**Not built yet:** replay of logged production loads, and turning Hero corrections into
scenarios.

### 4.2 A client changes its procedure. How do you make sure that other behavior does not break?

**Short answer.** The change is a new published version of that broker only, so it cannot
change another broker's sections. It passes the publish lint, its suite runs on the new
version before it serves loads, and it reaches a share of that broker's loads first, with
a one-call rollback.

1. **Layers.** A Northline change is a Northline version; the index never returns another
   broker's sections. A change to the standard layer reaches every broker, which is why
   the whole suite runs, for every broker.
2. **A scenario per section and per intent.** Scenarios carry `sop:<section_id>` tags; the
   lint warns about an uncovered section, and `agent eval lint --broker-file` fails on an
   intent with no scenario. Each of the 11 standard sections is tagged by at least one of
   the 22 core scenarios. `agent eval draft` drafts the missing ones.
3. **The broker's suite on the new version** (`POST /api/brokers/{id}/evals/run`): the
   standard suite runs with the broker's catalog and published sections
   (`load_overrides.broker`), plus its own folder `evals/scenarios/<broker>/`.
4. **Replay sees the change.** A changed SOP text changes the agent prompt, so the affected
   cassettes miss; replay cannot pass without a new live recording.
5. **Compare.** Regression scenarios must stay at pass^3 = 100%; capability scenarios are
   compared with the baseline. The SOP hash and the config version tie each result to its
   text.
6. **Canary and rollback** (2.3): `percent` of the broker's loads, sticky per load;
   `percent: 0` sends every load back to the previous version.

### 4.3 You change the LLM. How do you make sure that the system does not break?

**Short answer.** A model is one line in `config/models.yaml`. Cassettes are keyed by
model id, so a new model must be recorded with a live run of the suites. A switch is a
bake-off: each candidate runs the live suites against thresholds fixed before the round,
reported as a pass^k matrix per model and scenario group.

- **Per-role config** (`gate`, `agent`, `judge`, `driver_sim`, `extract`, `compile`, and
  the `embedding` model), with fallbacks from another provider family, a pinned provider
  for reproducible evals, and `require_parameters` (only endpoints that honour
  `tool_choice`).
- **Bake-off of the agent role on the loop** (`make bakeoff`, 2026-10-20): `gptoss`,
  `haiku` and `gemini` screened at k = 1 on the core, agent and docs suites with
  `--langsmith`, then k = 3 on the best two. Thresholds fixed before the round: groups A
  and B at pass^3 = 100%, no `must_act` case skipped by triage, cost per run within
  budget, at most 0.5 refused tool calls per run on average, and `t1_gap_search_then_hero`
  searches the SOP. The cheapest set that meets them wins.
- **Safety that does not depend on the model:** structured output is a forced tool call
  that pydantic validates, so a malformed answer is an `LlmError` (fallback, then a Hero
  task), never a half-read plan. The rules hold for any model, at the call and in
  `validate`.
- **Canary** on a share of loads, compared on the metrics of 4.1. **Result:** `openai/gpt-oss-120b` is the only set that met every threshold
  (A and B at pass^3, $0.00066 per model-calling run, 0.00 to 0.04 refused calls per run);
  Claude Haiku 5.5 passed 22/22 but at $0.00241 per run, over the cost threshold, and
  stays the first upgrade; Gemini 2.5 Flash missed a group A scenario at k = 1. The round
  also moved three guardrail controls to scripted mode only and replaced a path check with
  an outcome check (`evals/RESULTS.md` explains why). $0.96 in total.

**The wave 2 bake-off ran on the single plan** (`evals/RESULTS.md`, 17 rounds on
2026-10-19, $0.58 in total). Each round changed one thing and ran live on real models:

| Decide model (gate on GPT-4.1 Nano) | k | pass^k on 22 | Cost per run | Decide p50 / p95 |
|---|---|---|---|---|
| gpt-4.1-nano | 1 | 7 | $0.00021 | 1.1 / 1.4 s |
| gemini-2.5-flash-lite | 1 | 18 | $0.00034 | 1.1 / 1.6 s |
| mistral-small-3.2 | 1 | 16 | $0.00052 | 3.2 / 5.2 s |
| qwen3-30b-a3b | 1 | 19 | $0.00158 | 4.9 / 14.9 s |
| **gpt-oss-120b** | 3 | **22** | **$0.00021** | 4.7 / 8.2 s |
| claude-haiku-5.5 | 3 | 22 | $0.00097 | 2.7 / 4.6 s |

That rule picked **gate `openai/gpt-4.1-nano`, decide `openai/gpt-oss-120b`**, which
`config/models.yaml` keeps on the `agent` role until the wave 3 bake-off names its winner.
The rounds found model-specific problems that a code test could not: Gemini returned empty
plans because the action union was dropped on the way to Google (now sent as one flat
object for `google/` models), Anthropic endpoints refuse a forced tool choice, and two
prompt rules (no second deferral on a timer event, an acknowledgement never acts) closed
the last failures. `adapters/llm/router.py` routes each request to its role's model, and
the cassettes in `evals/cassettes/` replay the chosen models offline in CI.

### 4.4 Clients have different procedures and different complexity. How do you organize the evals?

**Short answer.** Scenarios are YAML files in suites, tagged by purpose (regression or
capability) and by the SOP sections they cover. The standard suite runs once per broker
configuration, and a broker with a more complex SOP adds its own folder; a broker variant
reuses one load fixture with `load_overrides`, so a new broker starts with the standard
suite, not a new harness.

| Suite | Scenarios | Checks |
|---|---|---|
| `core` (22) | Groups A to G below | The assignment's sequence and the standard procedures; replayed in CI |
| `agent` | `n1_outside_email_refused`, `n2_rejection_budget`, `t1_gap_search_then_hero` | The loop's guardrails: a refusal at the call, the refusal budget, a gap that is searched before it goes to a Hero |
| `docs` | `p1_pod_clean_delivers`, `p2_pod_exceptions_needs_review`, `p3_pod_overdue_follow_up` | The document pipeline end to end, with the PDF fixtures as attachments |
| `<broker>` | `evals/scenarios/<broker>/`, run with `--broker-file evals/brokers/<broker>.json` | A broker's own situations; `agent eval draft` writes drafts tagged `draft` |

| Group (core) | Scenarios | Checks |
|---|---|---|
| A | `a_client_a_high_risk`, `a_question_during_last_wait` | The assignment's sequence |
| B | `b_ack_with_nothing_open`, `b_ack_after_eta_request`, `b_broker_thanks_by_email`, `b_medium_risk_does_not_wake`, `b_eta_received_cancels_follow_up` | Doing nothing |
| C | `c1_eta_from_driver`, `c2_eta_from_dispatcher`, `c3_relative_eta`, `c3_vague_eta`, `c4_risk_cleared` | ETA answers |
| D | `d1_keystone_high_risk`, `d2_keystone_reefer_departure`, `d4_lumper_no_sop` | Client B, reefer, no SOP answer |
| E | `e1_prompt_injection`, `e3_duplicate_message` | Injection, duplicates |
| G | `g1_deferral_stale_timer` to `g5_ping_during_deferral` | Deferrals |

- **Regression or capability.** Regression scenarios (20 of the 22 core ones) must stay at
  100%. Capability ones (`c3_relative_eta`, `d4_lumper_no_sop`) measure what we are
  improving and move to regression once they pass pass^3 in consecutive rounds.
- **The core scenarios were not edited for wave 3:** a plan given in the answer
  (`script.decide`, now an alias of `script.agent`) is validated as before, so the 22 pass
  in scripted mode as written.
- **When each mode runs:** every pull request in scripted and replay mode (`make eval`,
  `make eval-replay`, every suite), a broker's publish on its suite, a model change as a
  bake-off.

## 5. Scale, response time and cost

### 5.1 How does your design keep each agent run at 5 minutes or less?

**Short answer.** Each run is bounded by construction: a per-load FIFO with stateless
workers in parallel, a triage call for messages and one agent call whose loop has a round
limit and a cost limit, model timeouts inside a 240-second run deadline, and escalation to
a Hero instead of endless retries. Queue age is measured against the 5-minute SLA from the
time the event happened.

| Mechanism | Where |
|---|---|
| Per-load FIFO: one event per load in flight, loads in parallel; stateless workers scale horizontally | `EventQueue` in `ports/repositories.py` |
| No transaction open while a model thinks; the SOP index is read outside the unit of work | `application/runtime.py`, `RuntimeDeps.knowledge` |
| No model call for pings, stale timers and duplicates; triage only for messages; the agent loop stops at `max_steps` rounds or `max_cost_usd` | `application/gate.py`, `BrokerLimits` |
| 60 s per model call, 240 s per run | `RuntimeConfig.llm_timeout_s`, `RuntimeConfig.run_deadline_s` |
| Documents are read in the tick, before the queue, under the same model timeout | `application/documents.py` |
| Fallback chain and circuit breaker: a failing model is skipped, not waited on | `adapters/llm/resilient.py` |
| Model failure or deadline: one urgent Hero task, the event completes; other errors retry, then dead-letter | `application/escalation.py`, `AgentRuntime.process_next` |
| Age of the oldest queued event against 5 minutes, run p95, timer lag | `queue_metrics`, `run_metrics`, `timer_lag_metrics` in `application/metrics.py` |

The 240-second deadline leaves 60 seconds for the queue wait and delivery. The step
ceilings in `docs/architecture.html` ("Budgets") are targets: a loop round is one model
round trip (about 3 to 5 seconds on the wave 2 models), so 8 rounds fit inside the agent's
ceiling. Measured (`Usage.latency_ms`, gpt-oss-120b, bake-off round 2): the agent call, all
its rounds, takes 5.1 s at p50 and 9.6 s at p95 on the core suite, and 16.5 s at p95 on the
docs suite, far inside the 240-second deadline.

A claim lease (`EventQueue.reclaim_expired`, `RuntimeConfig.claim_lease_s`) returns an
event whose worker died mid-run to the queue, counted as a failed attempt.
**Not built yet:** the worker process with autoscaling on queue age, an always-on
delivery dispatcher (today `tick()` calls `dispatch_outbox`), and per-broker concurrency
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
| SOP index | Published sections with their embeddings in Postgres, an HNSW index (pgvector) and a GIN full-text index; embedded once, at publish | The same tables; a read replica for search; a cache of each broker's published version by hash |
| Documents | Blobs on the filesystem (`BLOBS_DIR`); the pipeline in the worker's tick | S3 behind the same `BlobStore` port; the pipeline as its own worker pool, scaled on pending documents |
| Models | About 5 million tokens per minute in business hours on the single plan; about four times that on the loop (the core suite used 2.34 million tokens at k = 3, against 0.59 million on the single plan) | About 10 times that: higher rate limits or provisioned throughput, more provider families in the fallback chain, per-broker limits, priority for risk and timer events |
| Tracing | One trace per run, a bounded pool | Sampling (all escalations and failures, a share of the rest), batched export or self-hosting |
| Cost | Under $1 per load (5.3) | The same per load; total spend grows linearly |

The layer rules (import-linter contracts in `pyproject.toml`) keep storage, queues,
blobs and providers behind ports, so the pipeline, the ledger and the eval suites do not
change.

### 5.3 How do you keep the LLM cost at approximately $1 or less for each load?

**Short answer.** Most events never reach a model, triage keeps chatter away from the
expensive call, the agent loop has a round and a cost limit per run, prompts stay small,
and a meter watches each load. The single-plan design measured about $0.0002 per agent run
and about $0.03 per load. The loop resends its prompt on every round, so it costs more per
run. The plan below put a typical load at about $0.07 on the current agent model; the
bake-off measured about $0.04 (last row).

**Measured on the single plan** (characters / 4, over the 67 requests of the 22 core
scenarios, rendered by `application/prompts.py` with the seed SOPs, before wave 3):

| Call | System | User | Tool schemas | Input tokens | Output tokens |
|---|---|---|---|---|---|
| Gate (22) | 2,679 chars | 319 to 771 chars | 535 chars | 945 median, 1,000 max | about 25 |
| Decide (45) | 6,391 chars | 6,455 to 8,590 chars | 5,220 chars | 4,720 median, 5,050 max | 85 median, 275 max |

**Planning figures for the loop** (to be replaced by the bake-off's measurement):

- Triage: 1,000 in, 50 out, as before.
- Agent: the single plan's 11,000 input tokens per call (the measured 4,700 plus a full
  transcript, a catalog of about 120 sections and longer sections) plus about 1,000 per
  round for the action tools' schemas and the tool results. A typical run takes 2.5
  rounds: one that stages the actions (several tool calls in one round), one that reads
  their answers and finishes, and a search on some events. So about 30,000 input and 600
  output tokens per run.
- Documents: one `extract` call per attachment, about 3,000 in (more with page images)
  and 300 out.
- Embeddings: only at publish, about $0.02 per million tokens: a 100-page SOP costs well
  under a cent. A live `search_sop` embeds one short query.

**Prices** per million input / output tokens (`config/models.yaml`): triage
`openai/gpt-4.1-nano` $0.10 / $0.40; agent `openai/gpt-oss-120b` $0.037 / $0.17 (the wave 2
choice); candidates `anthropic/claude-haiku-5.5` $0.10 / $0.50 (Anthropic counts the same
prompt as about 1.7 times the tokens) and `google/gemini-2.5-flash` $0.30 / $2.50; extract
`google/gemini-2.5-flash`.

- Triage: 1,000 × $0.10/M + 50 × $0.40/M = **$0.00012** per call.
- Agent on gpt-oss-120b: (30,000 × $0.037 + 600 × $0.17)/M = **$0.0012** per run.
- Agent on Claude Haiku 5.5: (51,000 × $0.10 + 900 × $0.50)/M = about $0.0056 per run.
- Agent on Gemini 2.5 Flash: (30,000 × $0.30 + 600 × $2.50)/M = about $0.0105 per run,
  less with its automatic prompt caching (a cache read costs a quarter of the input).
- Document: (3,000 × $0.30 + 300 × $2.50)/M = about $0.0017 per attachment.

| Per load | Typical | Heavy |
|---|---|---|
| Inbound messages (one triage call each) | 75 | 100 |
| Messages that reach the agent (40% / 60%) | 30 | 60 |
| Risk changes into or out of high, timers, processed documents (agent only) | 4 + 4 + 4 | 6 + 6 + 5 |
| Location pings | hundreds, $0 | hundreds, $0 |
| Triage | 75 × $0.00012 = $0.009 | 100 × $0.00012 = $0.012 |
| Agent on gpt-oss-120b | 42 × $0.0012 = $0.050 | 77 × $0.0012 = $0.092 |
| Documents | 4 × $0.0017 = $0.007 | 5 × $0.0017 = $0.009 |
| **Total against $1** | **about $0.07** | **about $0.11** |
| Agent on Claude Haiku 5.5 instead | about $0.25 | about $0.45 |
| Agent on Gemini 2.5 Flash instead | about $0.46 | about $0.83 |
| **Measured on the loop** (gpt-oss-120b: triage $0.000099 and agent $0.000656 per call, bake-off round 2; documents as planned) | **about $0.04** | **about $0.07** |
| Measured with the agent on Claude Haiku 5.5 ($0.0025 per agent call) | about $0.12 | about $0.21 |

The margin comes from cheap models and few rounds: the same loop on a model at $3 / $12
per million and no caching would cost about 42 × (30,000 × $3 + 600 × $12)/M = about $4
per typical load. The bake-off's cost threshold (about $0.0015 per model-calling run, a
long load under about $0.25) is what keeps the chosen model inside the budget.

**Levers:**

1. **Pings never reach a model:** `domain/ping_filter.py` turns them into rare
   transitions (`b_medium_risk_does_not_wake`).
2. **The prefilter costs no model call:** duplicates, stale timers, empty messages and
   unknown senders stop in code; the stale timer in `g1_deferral_stale_timer` makes zero
   calls. Risk changes, timers and processed documents also skip triage.
3. **Triage skips noise:** an "ok" ends as `noop` after a 1,000-token call and never pays
   for an agent loop, while `must_act` keeps required intents.
4. **The loop's budget per run:** `max_steps` rounds and `max_cost_usd` (the adapter stops
   the loop and forces the answer when the running cost passes it), per broker; refused
   calls are capped by `max_rejections`. `max_steps = 0` turns a broker back to the
   single plan.
5. **Small prompts:** only the matching sections go in full; the rest is a one-line
   catalog and a search away.
6. **Prompt caching of the stable prefix:** stable parts come first (schemas, system
   prompt, load, contacts), and every round of a loop repeats the same prefix. OpenAI and
   Gemini models cache long prefixes automatically, and a cache read costs 25% (Nano,
   Gemini Flash) or 10% (Flash Lite) of the input price. The adapter records
   `Usage.cached_tokens`, priced at `cached_input` (`adapters/llm/models_config.py`).
7. **A cheap model per role,** chosen by a bake-off (4.3).
8. **A cost cap per load:** `cost_metrics` (`application/metrics.py`) reports each load's
   spend against $1 and warns at 80%; `Budget` in `evalkit/graders.py` fails a scenario
   over its cost or tokens. **Not built yet:** enforcement per load in the runtime (at
   80%, `max_steps = 0` and no model above the cheap tier; at 100%, judgment calls go to a
   Hero). The per-run cap bounds a runaway loop, not a whole load: 77 runs at the $0.05
   cap would be $3.85.
