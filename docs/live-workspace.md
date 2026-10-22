<!-- generated-by: gsd-doc-writer -->
# Live broker workspaces

The platform UI now runs every scenario through Temporal and real-clock services.
Select a broker, edit its SOP/configuration, and start a scenario. Each start creates
an isolated load and pins the complete broker catalog and layered SOP in that load.
An initial location ping is always ingested. It uses the first step's risk when that
step is a ping; otherwise it preserves the fixture's risk. Only Temporal Activities
run LangGraph for these loads.

Seeded workspaces:

| Broker | Assignment role | Default ETA procedure |
| --- | --- | --- |
| Northline Logistics | Client A | Two SMS, 30 minutes after each; dispatcher email then Hero |
| Keystone Freight | Client B | One SMS, 30 minutes; broker Slack, no dispatcher |
| Aurora Transportes | Extreme demonstration | Ten SMS, one minute after each; Hero |
| Acme Logistics | Additional document cases | Its catalog, including POD requirements |

The catalog contains 36 YAML eval scenarios plus the generated Aurora case: 37 across
the four brokers, including seven stop-grounding cases. These are interactive
live operations: sample messages, pings and document fixtures are sent **when the
operator clicks**, not by an automatic scenario player. Wait steps cannot advance time.
The operator can also send custom SMS/email/Slack as the load's contacts, new location
pings, and stop execution. Communications use recording gateways, while models and
Temporal timers are real. Automated grading and virtual-clock regression playback
remain available through the eval CLI; the live UI does not claim pass/fail eval scores.

## Operator flow

1. Select the broker in the sidebar. **Cenários** shows only its scenarios and the
   saved ETA attempt limit, wait and escalation channels.
2. To change behavior, open **SOPs e configurações**, select the procedure and scope
   (broker or load type), then replace or extend its inherited text. Select **Salvar
   para novas execuções** before starting; an unsaved browser draft is not executed.
3. Return to **Cenários** and select **Iniciar ao vivo**. The new load opens in
   **Operações** with its initial ping already sent.
4. Use **Enviar esta entrada** to submit a sample input, or choose a contact and
   channel and write a custom message. **Enviar location ping** changes the observed
   risk. Example times in the script are references; operator inputs arrive when sent.
5. Inspect recorded communications, **Ver decisão do agente**, obligations and timers.
   **Consultar SOP usado** shows the load's rendered procedures, version and revision.
   Reopen loads from **Operações**; closing a browser tab does not stop their workflows.
6. **Parar execução** records a durable stop request. Wait for the stopped status;
   an Activity or delivery already in progress may finish before confirmation. To use
   an updated SOP or resume after stopping, start a new load.

Northline and Keystone use 30-minute ETA waits by default. Aurora uses one-minute
waits. A deadline becoming due makes work eligible; queue, worker and model latency
can delay its processing. The UI countdown is a display, not the scheduler.

## SOP editing and persistence

`broker_profiles` stores the broker catalog, layered SOP sections and revision in the
durable Postgres database. The first access seeds files from `sops/`. Saved profiles
survive API/worker restarts and are independent of the simulator database and browser.
Editing the seed files does not overwrite an already saved broker profile; use the
workspace editor/API to update that profile.
Conversely, `make eval` reads the file-backed SOPs; it does not automatically test a
profile or unsaved draft edited in the UI. See [SOP authoring](../sops/README.md).
A stale browser save returns a conflict instead of overwriting a later revision.
Local browser drafts are scoped to broker and saved revision. The standard layer is
read-only; client and load-type sections can replace or extend it. New procedures can
be added and associated with catalog intents. The server validates scope, inheritance,
limits, variables and section IDs before saving.

The ETA template uses `{{eta_request.max_attempts}}`,
`{{eta_request.follow_up_minutes}}` and `{{eta_request.on_exhausted}}`. The runtime
receives rendered text and the same catalog limits. Other obligation kinds expose the
same variables. Attempt configuration also reserves SMS rate capacity for a reply.

Each load stores the resolved configuration version, profile revision, scenario and
start time. A later save changes only new loads. Start IDs and scenario input IDs are
idempotent. A retry of an accepted start keeps its original snapshot even after edits.
Stop requests use the existing durable stop control: settle the current Activity,
cancel pending work and preserve history.

The snapshot pins SOP sections and the broker catalog, not an entire application or
model build. Runtime prompt changes still apply to existing loads. In particular, the
[stop-grounding context](stop-grounding.md) resolves the operational stop from the
load stage and explicit stop pointer, including for older saved SOPs.

Scenario appointment windows are shifted into live time. Sample absolute ETAs retain
their original offset from the sample input time. The bundled clean/exception PODs are
generated for the live load and receipt time; the original eval fixtures stay intact.
Memory scenarios use load-specific test facility addresses to avoid contaminating
other live operations. Document extraction uses the load's pinned broker catalog too.

## Running locally

```sh
# Configure OPENROUTER_API_KEY in .env first; see the root README for prompt settings.
docker compose -f compose.temporal.yml up -d --build
docker compose up -d --build app
```

Open `http://127.0.0.1:8000/#/scenarios?broker=northline`.
The frontend bridge targets the durable ingress on port 8001; that service, worker and
relay use the separate Postgres database on port 5433. Temporal listens on 7233 and its
UI on 8233. Apply all migrations, including `0004_broker_profiles.sql`, before serving.
Both Compose stacks run their migration job automatically before application services.
Existing Aurora links without a broker query parameter remain supported.

Use `docker compose down` and `docker compose -f compose.temporal.yml down` to stop
both stacks while retaining their named volumes. See [the Temporal guide](temporal.md)
for host-run services, retry behavior and delivery guarantees.

## API surfaces

Workspace API routes are under `/workspace/brokers` on the durable ingress and
`/api/durable/workspace/brokers` through the frontend bridge. Configuration writes and
operator inputs belong here; the older simulator endpoints remain for compatibility
and automated tests, but the UI no longer invokes them.

Paths below are relative to `/workspace/brokers/{broker_id}`:

| Method | Path | Purpose |
| --- | --- | --- |
| GET / PUT | `/profile` | Read or save the broker profile using its current revision |
| GET | `/scenarios` | List this broker's interactive scenarios |
| GET | `/loads` | List existing operations |
| POST | `/loads/{load_id}/start` | Start with `scenario_id` and saved `revision` |
| GET | `/loads/{load_id}` | Read state, run records, timers, outbox and processing status |
| POST | `/loads/{load_id}/messages` | Submit a known contact's message on an allowed channel |
| POST | `/loads/{load_id}/pings` | Submit a location ping |
| POST | `/loads/{load_id}/steps/{index}` | Submit a sample input; wait-only steps are rejected |
| POST | `/loads/{load_id}/stop` | Request a durable stop |

The lower-level `/loads`, `/brokers/{broker_id}/loads/{load_id}` and `/demos/aurora`
routes remain available for integrations and older demos. They are not the broker
profile editor used by the current UI. See <http://localhost:8001/docs> after startup
for request schemas; these local endpoints have no caller authentication.
