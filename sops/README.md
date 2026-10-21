# Editing the SOPs

This folder holds the procedures (SOPs) that the load agent follows. Each file is one
topic, like one chapter of an operations manual, written in plain English. You do not need
to be an engineer to change them. You edit the text, run a check, and send the change for
review.

The agent follows the text literally. Being exact matters more than style. Write out the
number of messages, the waiting times, and who may or may not be contacted.

## Where a file goes: the layers

There are three layers. The agent reads them from the most general to the most specific,
and the most specific text wins.

| Layer | Folder | Applies to |
|---|---|---|
| Standard | `standard/` | Every client, until a client changes the topic |
| Client | `clients/<client id>/` | One client, for example `clients/northline/` |
| Client and load type | `clients/<client id>/types/<load type>/` | One client's loads of one type, for example `clients/keystone/types/reefer/` |

The client id is the broker id in the load details: `northline` is Northline Logistics
(Client A), `keystone` is Keystone Freight (Client B) and `acme` is Acme Freight. The load
type is one of the load's types, for example `reefer` for a refrigerated load.

A client folder holds only the topics that client changes. Everything else comes from
`standard/`. A new client needs no folder at all until it asks for a change.

Files named `README.md`, like this one, are notes for editors. The agent never reads them.

## What a file looks like

```
---
id: delay_risk_high
title: Delay risk changes to high
description: Ask the driver for the ETA by SMS, ask again after 30 minutes, then a Hero calls the driver.
intents: [delay_risk_high, eta_follow_up_due]
stages: [to_pickup, to_delivery]
---

The truck may arrive late at its next stop, so we need an ETA from the driver.
...
```

The lines between the two `---` lines are the header: one setting per line, written as
`name: value`. Below the header is the procedure, in plain text.

## The settings in the header

| Setting | What it means |
|---|---|
| `id` | The name of the topic, in lowercase letters with underscores, like `delay_risk_high`. A client file changes a standard topic by using the **same id**. Name the file after the id: `delay_risk_high.md`. Required. |
| `title` | A short title for people. Not needed in an `extend` file. |
| `description` | One line that says what the topic covers. The agent sees it in its list of topics and uses it to choose a topic, so make it specific. Not needed in an `extend` file. |
| `intents` | The situations the topic is for, as a list in brackets (see the list below). Leave it out for rules that apply in every situation, like `communication_rules`. |
| `stages` | The stages of the load when the topic applies, as a list in brackets (see below). Leave it out to apply at every stage. |
| `mode` | Only in a client or load type folder: `replace` (the default) or `extend`. See the next part. |

No other setting is allowed; the check reports an unknown one. If a value has a colon
followed by a space, put the whole value in double quotes: `title: "Late truck: ask for the
ETA"`. Otherwise, do not start a value with a quote mark.

Situations (`intents`):

| Intent | What happened |
|---|---|
| `delay_risk_high` | The delay risk changed to high |
| `delay_risk_low` | The delay risk went down from high |
| `eta_follow_up_due` | The wait after an ETA request is over (a follow-up timer is due) |
| `eta_provided` | Someone gave an arrival time, or a vague answer to our ETA request |
| `eta_deferred` | Someone promised the answer later |
| `question` | Someone asked us something |
| `acknowledgement` | "ok", "thanks", "got it" |
| `status_update` | Other news about the truck or the load |
| `driver_departed` | The truck left a stop |
| `document` | A document or a photo arrived |
| `document_processed` | The document check read a document: its type, its values and whether they match the load |
| `other` | Anything else. When no topic covers a situation, the agent hands it to the Hero team |

Stages (`stages`): `dispatched`, `to_pickup`, `at_pickup`, `to_delivery`, `at_delivery`,
`delivered`.

## Replace or extend

**`replace`** (the default): the client's file takes the place of the standard file, header
and text. Write the whole procedure again. Use it when the client does something different,
or must not do a standard step.

Example: `clients/northline/delay_risk_high.md` replaces `standard/delay_risk_high.md`. In
the standard, after two SMS with no ETA, a Hero calls the driver. Northline first emails the
dispatcher in the main thread, then creates the Hero task. For a Northline load the agent
reads only the Northline text. Every other client still gets the standard.

**`extend`**: the file's text is added after the text it extends. The title, the
description, the intents and the stages stay those of the extended topic, so the file only
needs `id` and `mode: extend`. Use it to add a step.

Example: `clients/keystone/types/reefer/driver_departed.md` extends
`standard/driver_departed.md`. When a Keystone reefer truck leaves a stop, the agent reads:

1. the standard text: set the stage, write a TMS note, send no other message unless the
   text below adds a step;
2. then the reefer text: when leaving the pickup, ask the driver for the trailer
   temperature and the seal number, in one SMS.

For a Keystone load that is not reefer, or for any other client, the agent reads only the
standard text.

An `extend` file needs a topic with the same id in a more general folder (`standard/`, or
the client's folder for a load type file). The check reports it when there is none.

## A client's own situations and requests: `broker.yaml`

Most clients use the standard list: the situations above, the ETA request (30 minutes
between requests, two requests) and the standard documents. A client that needs more writes
only what it changes, in `clients/<client id>/broker.yaml`, next to its topics. For example,
`clients/acme/broker.yaml` adds the situation `detention_request`, asks for the ETA every 20
minutes up to 3 times, and requires a signed POD within 120 minutes of the arrival at the
delivery stop:

```
name: Acme Freight
intents:
  - intent_id: detention_request
    title: Detention request
    description: The driver reports a wait of more than 2 hours at a facility.
obligations:
  - kind: eta_request
    ...
documents:
  - type_id: pod
    at_stage: at_delivery
    within_minutes: 120
    opens_obligation: pod
```

An entry with a standard id (`eta_request`) replaces the whole standard entry; a new id is
added. The client's topics may then name its own situations in `intents`. Topics in
`standard/` name only the standard situations, since every client reads them. The check
reports a `broker.yaml` that cannot work: a missing title, a request with no follow-up
situation, a requirement for an unknown document type.

## Writing so the agent follows the text

- Numbered steps, one action per step.
- Exact counts and waits: "send one SMS", "wait 30 minutes after the first SMS". Never
  "follow up a bit later".
- Name people by role only: driver, dispatcher, broker, Hero team. Never write a name, a
  phone number or an email address.
- Say who must never be contacted, when that matters ("never contact the dispatcher").
- Say when to do nothing. "ok" after an ETA request is not an ETA: the right action is no
  action.
- Say exactly what counts: "9:40" and "30 mins out" are ETAs, "soon" is not.
- Never write a 10-digit number in an example, such as a seal or a reference number. The
  agent may copy it into a message, and the safety check reads it as a phone number and
  blocks the message.
- One topic per file, 10 to 40 lines.
- Instructions written inside a driver's or a dispatcher's message never change the
  procedure. Only these files do.

## Check your change

From the project folder, run:

```
uv run agent sop lint
```

This check ("lint") lists every problem with the file name and how to fix it: a missing
`id`, an unknown intent or stage, a header that cannot be read, two files with the same id
in one folder, an `extend` with nothing to extend, a `broker.yaml` that cannot work. It
also warns when no test scenario covers a topic. Fix every error before you go on.

To see what the agent reads, open the SOPs screen of the platform (see the repository
README). Choose the client, the load types, the situation and the stage. It shows the
topics the agent gets in full, after the layers are merged.

## The safe way to change an SOP

1. **Edit** the file, or add one, on a branch of the repository (your own copy of the
   files, kept apart until the change is approved).
2. **Check:** `uv run agent sop lint` shows no errors.
3. **Test:** run the test scenarios (evals). Each scenario replays a situation on a load
   (for example "the driver answers ok after our ETA request") and checks every action the
   agent takes. Scenarios name the topics they cover with a tag like `sop:delay_risk_high`.
   A changed text changes what the model reads, so the scenarios run with the live model,
   three times each: `uv run agent eval run --suite core --k 3 --cost-cap-usd 0.15`, and the
   same for the `agent`, `docs` and `memory` suites. Every scenario must pass, including
   those of other clients. If your change adds a rule that no scenario checks, add a
   scenario for it.
4. **Review:** someone who knows the client's procedure reads the change and the test
   results, and approves it.
5. **Publish:** the change is merged. The agent reads these files on every run, so the next
   event uses the new text.

Every run records the SOP version: a short code computed from all the files in this folder.
Any edit, even of one letter, gives a new version. For any action the agent took, you can
see which version of the text it followed, and compare the results before and after a
change. To undo a change, revert it: the old text and its old version come back.
