# Live broker workspaces

The platform UI now runs every scenario through Temporal and real-clock services.
Select a broker, edit its SOP/configuration, and start a scenario. Each start creates
an isolated load and pins the complete broker catalog and layered SOP in that load.
An initial location ping is always ingested; it preserves quiet scenarios' risk and
uses the first risk ping for risk scenarios. Only Temporal Activities run LangGraph.

Seeded workspaces:

| Broker | Assignment role | Default ETA procedure |
| --- | --- | --- |
| Northline Logistics | Client A | Two SMS, 30 minutes after each; dispatcher email then Hero |
| Keystone Freight | Client B | One SMS, 30 minutes; broker Slack, no dispatcher |
| Aurora Transportes | Extreme demonstration | Ten SMS, one minute after each; Hero |
| Acme Logistics | Additional document cases | Its catalog, including POD requirements |

The screen contains all 29 existing eval scenarios plus Aurora. These are interactive
live operations: sample messages, pings and document fixtures are sent **when the
operator clicks**, not by an automatic scenario player. Wait steps cannot advance time.
The operator can also send custom SMS/email/Slack as the load's contacts, new location
pings, and stop execution. Communications use recording gateways, while models and
Temporal timers are real. Automated grading and virtual-clock regression playback
remain available through the eval CLI; the live UI does not claim pass/fail eval scores.

## SOP editing and persistence

`broker_profiles` stores the broker catalog, layered SOP sections and revision in the
durable Postgres database. The first access seeds files from `sops/`. Saved profiles
survive API/worker restarts and are independent of the simulator database and browser.
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

Scenario appointment windows are shifted into live time. Sample absolute ETAs retain
their original offset from the sample input time. The bundled clean/exception PODs are
generated for the live load and receipt time; the original eval fixtures stay intact.
Memory scenarios use load-specific test facility addresses to avoid contaminating
other live operations. Document extraction uses the load's pinned broker catalog too.

## Running locally

```sh
docker compose -f compose.temporal.yml up -d --build
docker compose up -d --build app
```

Open `http://127.0.0.1:8000/#/scenarios?broker=northline`.
The frontend bridge targets the durable ingress on port 8001; that service, worker and
relay use the separate Postgres database on port 5433. Temporal listens on 7233 and its
UI on 8233. Apply all migrations, including `0004_broker_profiles.sql`, before serving.
Existing Aurora links without a broker query parameter remain supported.

Workspace API routes are under `/workspace/brokers` on the durable ingress and
`/api/durable/workspace/brokers` through the frontend bridge. Configuration writes and
operator inputs belong here; the older simulator endpoints remain for compatibility
and automated tests, but the UI no longer invokes them.
