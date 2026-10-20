# Broker SOP fixture: Acme Freight

A broker's own operating procedure, used to test onboarding without code: upload the PDF,
compile it into a draft (sections, proposed intents, proposed document requirements),
review, publish (card KNW, plan step "Verificação fim a fim" 2 and 3).

| File | What it is |
|---|---|
| `acme_sop.md` | The source text: Acme Freight procedure CO-07, "In-Transit Communication", revision 3. Written like a broker's document, not in our SOP format: no front matter, one numbered `##` heading per topic, numbered steps, a document-control table and a contact table. |
| `acme_sop.pdf` | The same text laid out as a printed procedure: 3 US Letter pages, Helvetica, a text layer on every page. Each page also carries a running header ("Acme Freight, Inc.", "CO-07 In-Transit Communication, Revision 3") and a footer ("Controlled document...", "Page n of 3"), which are not in the Markdown. |
| `make_fixtures.py` | Rebuilds the PDF from the Markdown. |

## Rebuild

```
uv run python evals/fixtures/sops/make_fixtures.py
```

No dependency: the script uses the small PDF writer in `../docs/pdfmini.py` (standard
Helvetica fonts, plain content streams, a correct xref table). The output is deterministic:
a rebuild gives the same bytes. Edit `acme_sop.md`, then rebuild, and commit both files.
The script reads only the Markdown the file uses: `#`/`##` headings, paragraphs, items
numbered `1.`, lettered `a.` or dashed `-` (three spaces of indent per level) and pipe
tables.

## What a compiler should find

Five topics, one per `##` heading. The text says when each one applies ("When this
applies: ...") so a compiler can map it to intents and stages.

| # | Heading | Suggested section id | Intents | Stages |
|---|---|---|---|---|
| 1 | Late truck: asking the driver for an ETA | `delay_risk_high` (replaces the standard one) | `delay_risk_high`, `eta_follow_up_due` | `to_pickup`, `to_delivery` |
| 2 | Detention: truck held at a facility | `detention` | **`detention_request` (new)** | `at_pickup`, `at_delivery` |
| 3 | Proof of delivery (POD) | `proof_of_delivery` | `pod_follow_up_due` (the follow-up of the new `pod` obligation), `document_processed` | `at_delivery` |
| 4 | Questions from the driver | `driver_questions` (replaces the standard one) | `question` | every stage |
| 5 | Communication rules | `communication_rules` (replaces the standard one) | none: applies to every event | every stage |

A compiler may also tag section 3 with `status_update` (its first step says what to do on
arrival at the delivery stop). Tests should not depend on that.

**Proposed intent** (not in today's catalog):

```
intent_id: detention_request
title: Detention request
description: The driver reports that the truck has waited more than 2 hours at a shipper
  or a receiver, or asks us to start detention.
must_act: true
examples: ["Been at the dock since 7, still not unloaded", "We've been here 3 hours, can you start detention?"]
```

A wait of 2 hours or less is not detention: section 2 treats it as a status update.

**Proposed document requirement:**

```
type_id: pod
at_stage: at_delivery
within_minutes: 120
opens_obligation: pod
```

**Broker settings a reviewer sets.** `SopDraft` proposes intents and requirements, not
obligations, so these come from the broker profile (card BRK), not from the compiler:

| Obligation | follow_up_minutes | max_attempts | on_exhausted | From |
|---|---|---|---|---|
| `eta_request` | 20 | 3 | `dispatcher_email`, `hero_task` | section 1: every 20 minutes, at most 3 SMS, then email the dispatcher and open an urgent Hero task |
| `pod` (new; follow-up intent `pod_follow_up_due`) | 30 | 1 | `hero_task` | section 3: the POD is due 120 minutes after arrival (the requirement's timer), one reminder SMS, then a Hero task 30 minutes later |

## What the agent should do on an Acme load

| Situation | Expected actions |
|---|---|
| Delay risk becomes high, no ETA request open | 1 SMS to the driver asking for the ETA; timer 20 min |
| Follow-up due after request 1, then after request 2 | 1 SMS each (requests 2 and 3); timer 20 min each |
| Follow-up due after request 3 | Email the dispatcher in the load thread, then an urgent Hero task to call the driver; close the ETA request as exhausted. No 4th SMS |
| "ok" or "on my way" while an ETA request is open | Nothing; the follow-up stays as scheduled |
| Load 481207, driver at 11:45: "checked in at Valley DC at 09:10, still waiting" (appointment 09:30, so 2 h 15 min of wait) | TMS note with the times; Hero task "Request detention at Valley DC" (not urgent); 1 SMS acknowledging the wait. No promise of detention pay |
| The same driver writes again about the same wait | A new TMS note only |
| A wait of 2 hours or less | A TMS note only |
| Truck arrives at the delivery stop | No POD request yet (the `pod` obligation opens with a 120 min timer) |
| 120 min after arrival, no POD | 1 SMS asking for a photo of the signed POD (`requests: pod`); timer 30 min |
| 30 min after the reminder, no POD | Hero task to get the POD from the carrier (not urgent); close as exhausted |
| A clean POD is processed | Set the stage to delivered; TMS note |
| A POD with an exception or a mismatch | Hero task with the values; stage unchanged; nothing to the driver |
| "What's the PO?" | Reply with the PO from the load instructions (55821 on load 481207) |
| "What does this load pay?" | No reply about the rate; Hero task |

The text follows `sops/README.md` where it matters to the agent: roles only (no person,
phone number or email address), exact counts and waits, and no 10-digit number.
