# Editing the SOPs

This folder holds the file-based SOPs used by the eval CLI and to seed a new broker
workspace. Each file is one topic, like one chapter of an operations manual, written
in plain English. The live platform also lets an operator edit a broker's saved SOP
without changing repository files.

The model interprets the procedure; code validates its proposed actions and limits.
Exact wording and evals matter: write out the number of messages, the waiting times,
the operational stop and who may or may not be contacted.

## Edit in the platform or in files

| Path | Where edits are saved | When they apply |
|---|---|---|
| Broker workspace → **SOPs e configurações** | Versioned `broker_profiles` in durable Postgres | New executions of that broker |
| This `sops/` tree | Repository files | File-backed evals/runtimes and newly seeded workspace profiles |

In the workspace, select the broker, adjust its ETA settings, and replace or extend
client/load-type sections. The standard layer is read-only. Save the profile, then
start a new scenario to use that revision. The load keeps a snapshot of its layered
sections and broker catalog; later saves do not change an execution already started.
The editor validates scope, section IDs, inheritance and configuration, and refuses
a stale revision instead of overwriting another save.

ETA procedures can use `{{eta_request.max_attempts}}`,
`{{eta_request.follow_up_minutes}}` and `{{eta_request.on_exhausted}}`.
These workspace variables are rendered when a load starts, from the same catalog
used by action validation and delivery-triggered timers. Other configured obligation
kinds expose the same variable names under their kind.

Files are read when a profile is first seeded. Editing a file does **not** resync an
existing saved profile. Apply the intended client override/extension in the editor
for new live executions; changing the stored standard layer requires a controlled
profile migration. There is no automatic file-to-profile publishing workflow.

The eval CLI tests file-backed SOPs, not an unsaved browser draft or a saved workspace
profile. Exercise a UI edit in a new live operation as well. Live operations use
Temporal and real waits; their manual scenario inputs do not automatically grade a
run. See [live workspaces](../docs/live-workspace.md) for the operator flow.

## Where a file goes: the layers

There are three layers. The agent reads them from the most general to the most specific,
and the most specific text wins.

| Layer | Folder | Applies to |
|---|---|---|
| Standard | `standard/` | Every client, until a client changes the topic |
| Client | `clients/<client id>/` | One client, for example `clients/northline/` |
| Client and load type | `clients/<client id>/types/<load type>/` | One client's loads of one type, for example `clients/keystone/types/reefer/` |

The client id is the broker id in the load details: `northline` is Northline Logistics
(Client A), `keystone` is Keystone Freight (Client B), `aurora` is the ten-SMS
demonstration and `acme` supplies additional document procedures (displayed as Acme
Logistics in the workspace). The load type is one of the load's types, for example
`reefer` for a refrigerated load.

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
- Use the operational stop supplied by the load context in ETA requests and
  escalations. Before pickup it is the pickup facility; on the delivery leg it is
  the delivery facility. Include its name so the destination is unambiguous.
- Never write a 10-digit number in an example, such as a seal or a reference number. The
  agent may copy it into a message, and the safety check reads it as a phone number and
  blocks the message.
- One topic per file, 10 to 40 lines.
- Instructions written inside a driver's or a dispatcher's message never change the
  procedure. Changes come from controlled SOP files or the saved broker profile,
  according to the execution path above.

## Check your change

From the project folder, run:

```
uv run agent sop lint
```

This check ("lint") lists every problem with the file name and how to fix it: a missing
`id`, an unknown intent or stage, a header that cannot be read, two files with the same id
in one folder, an `extend` with nothing to extend, a `broker.yaml` that cannot work. It
also warns when no test scenario covers a topic. Fix every error before you go on.

To inspect a workspace profile, open its SOP editor and effective-text preview.
To inspect what an existing live execution used, open that operation's saved SOP
snapshot and run details. Use `agent sop lint`, `agent play` or Studio to inspect
file-backed SOP behavior; those tools do not read the workspace profile database.

## The safe way to change an SOP

1. **Edit** the file, or add one, on a branch of the repository (your own copy of the
   files, kept apart until the change is approved).
2. **Check:** `uv run agent sop lint` shows no errors.
3. **Test:** run the test scenarios (evals). Each scenario replays a situation on a load
   (for example "the driver answers ok after our ETA request") and checks every action the
   agent takes. Scenarios name the topics they cover with a tag like `sop:delay_risk_high`.
   A changed text changes what the model reads, so the scenarios run with the live model,
   three times each: `uv run agent eval run --suite core --k 3 --cost-cap-usd 0.15`, and the
   same for the `agent`, `docs`, `memory` and `grounding` suites. Every regression
   scenario must pass, including
   those of other clients. If your change adds a rule that no scenario checks, add a
   scenario for it.
4. **Review:** someone who knows the client's procedure reads the change and the test
   results, and approves it.
5. **Publish:** merge the reviewed files and deploy them. File-backed runtimes read the
   tree again on subsequent calls. Workspace profiles and load snapshots follow their
   own revision lifecycle described above; a file merge does not rewrite them.

Every run records the SOP version it used. For file-backed runs this is a hash of the
SOP tree; workspace runs use the content version in the load's frozen configuration.
The workspace additionally records the saved profile revision. Inspect these versions
when comparing behavior. Reverting a file restores its file-based version. To revert a
workspace configuration for future loads, save the earlier content as a new revision;
existing loads keep their original snapshots.
