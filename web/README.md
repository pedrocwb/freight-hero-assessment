<!-- generated-by: gsd-doc-writer -->
# Platform (web)

The broker workspace for testing live freight agents. Select Northline or Keystone (the
assignment clients), Aurora (the ten-SMS stress case), or Acme (document procedures).
Edit that broker's SOP, start a scenario, send driver/dispatcher/broker inputs manually,
and inspect the agent's decisions, actions and real-time follow-ups. Each operation runs
in a Temporal workflow and continues when the page closes.

React 18, TypeScript and Vite. No UI kit and no runtime CDN or web font: one stylesheet
with CSS variables. The older simulator components still include the Mermaid graph
inspector; the current application mounts `BrokerWorkspace` instead of `SimProvider`.

## Run it

The current UI needs both APIs: the platform on port 8000 and the durable ingress on
port 8001, plus its worker, relay, Postgres and Temporal. The fastest complete setup from
the repository root is:

```sh
docker compose -f compose.temporal.yml up -d --build
docker compose up -d --build app
```

Open `http://127.0.0.1:8000` for the built UI. Configure `OPENROUTER_API_KEY` in `.env`
before starting live operations. The LLM is live; outbound communication is recorded by
the local gateways. See [development](../docs/development.md) for host processes and
prompt-source setup.

For React development, keep those backend services running, then:

```sh
cd web
pnpm install
pnpm dev            # http://localhost:5173, /api is proxied to http://localhost:8000
```

`API_URL=http://localhost:8765 pnpm dev` proxies to a platform API on another port. The
browser calls `/api/durable/*`; the platform's fixed `DURABLE_API_URL` forwards those
requests to ingress. Running `uv run agent serve` alone serves the UI/legacy simulation
API but does not start the Temporal services.

| Command | What it does |
|---|---|
| `pnpm dev` | Dev server on :5173 with the `/api` proxy |
| `pnpm build` | Type-check, then build to `dist/` (the API serves it when it exists) |
| `pnpm preview` | Serve `dist/` on :4173 (same proxy) |
| `pnpm typecheck` | `tsc --noEmit` |
| `pnpm test` | Vitest unit tests |

## Screens

| Screen | What it shows |
|---|---|
| Cenários | Broker-filtered scenarios from `evals/scenarios/` (including `grounding`), plus the Aurora stress case. Search by title, description, suite or load type. The header shows the saved ETA attempts, wait and escalation channels. **Iniciar ao vivo** creates a new load, freezes the current SOP revision, submits a location ping and opens the operation. |
| Operações ao vivo | The broker's operation history and each load's detail. The detail shows stage, risk, SMS progress, pending timer, obligation ledger, inbound/outbound conversation, agent decisions/tools, trace links, the pinned SOP and scenario reference inputs. Send as a load contact on an allowed channel, inject another location ping, or click a scripted input (including supported attachment fixtures). **Parar execução** requests a durable stop and waits for confirmation in subsequent snapshots. |
| SOPs e configurações | Edit broker obligation settings, replace or extend inherited sections, and add sections scoped to the broker or a load type. Preview the effective SOP. Standard sections stay inherited; profile saves use revision checks. Edits apply to new operations, while existing loads retain their frozen SOP. |

Driver responses are manual. Scenario wait steps cannot advance time, and their original
timestamps are reference text. The server adapts example ETA timestamps and supported
document dates to the live load. Timers execute in Temporal after gateway acceptance;
the browser's one-second clock only updates the displayed countdown. Displayed action
times use the browser's local timezone and the `pt-BR` locale.

The workspace does not automatically grade a scenario's `expect`/`forbid` assertions.
Use `agent play` or `agent eval run` to evaluate those checks. The old simulator's reset,
time skip, arbitrary file upload and recording-to-scenario controls are not mounted in
the current UI; their backend routes remain documented in [API](../docs/api.md).

## Routes and state

Links carry broker/load selection in the hash:

```text
#/scenarios?broker=keystone
#/sops?broker=keystone
#/durable?broker=keystone
#/durable?broker=keystone&load=keystone-example
```

An omitted broker defaults to `northline`, except legacy links with an `aurora-` load
ID, which infer `aurora`. `#/load` without a load displays scenarios; with a load it
opens the durable view. The parser also accepts the old `#/inspector` alias, but the
current workspace does not restore the old run-specific explanation panel.

`BrokerWorkspace` fetches the broker catalog, profile, scenarios and health when the
broker changes or the operator reloads. It polls the broker's load list five seconds
after each completed request. `Durable` polls its snapshot two seconds after each
completed request and refreshes it after a write. Neither view uses `/api/stream`.
Aborted requests are discarded on navigation.

An interrupted start retains its load ID in session storage, scoped to broker, scenario
and profile revision, for a safe retry. Message and ping IDs are reused when retrying an
unchanged input in the mounted operation. SOP drafts live in local storage per broker
and are restored only for the matching server revision. The API rejects stale saves
instead of overwriting another window's changes.

## How it is built

- `src/App.tsx`: hash routing and broker selection; mounts one `BrokerWorkspace` per broker.
- `src/screens/BrokerWorkspace.tsx`: sidebar, tabs, catalog, start requests and operation history.
- `src/screens/BrokerSops.tsx`: editable profile, broker/load-type overrides, draft persistence
  and revision-aware save.
- `src/screens/Durable.tsx`: polling, manual inputs, stop, conversation, timers and pinned SOP.
- `src/lib/durable.ts`: `durableRequest<T>(path, options)` calls `/api/durable${path}`;
  snapshot/health types, progress derivation and conversation ordering.
- `src/lib/workspace.ts`: profile/scenario types, paths, labels, effective-section merge
  and template rendering for the editor preview. The backend remains authoritative.
- `src/lib/route.ts`: hash parsing/link helpers. Shared domain wire types remain in
  `src/api.ts`.

Legacy modules remain for their contracts, tests and reusable helpers:
`src/screens/Conversation.tsx`, `Scenarios.tsx`, `SopViewer.tsx`, `src/state/sim.tsx`,
`src/hooks/useEventStream.ts`, `src/lib/bus.ts` and `src/hooks/useResource.ts`.
The legacy `createApi(fetch)` client in `src/api.ts` supports injectable fetch for tests;
its SSE/resource refresh model is separate from live workspace polling.

Workspace errors appear inline. `durableRequest` extracts string or validation-list
`detail` values from failed API responses. A failed operation poll preserves the last
snapshot, displays the connection error and retries; input controls stay disabled while
the snapshot is unavailable, stopping or stopped.

## Tests

`pnpm test` runs Vitest against `src/**/*.test.ts` in the Node environment. The tests
exercise pure helpers and API contracts, including `workspace.test.ts` and
`durable.test.ts`; they are not browser end-to-end tests. `pnpm build` runs TypeScript
checking before bundling. For agent behavior, use the backend's `grounding` eval suite
and Temporal tests described in [development](../docs/development.md).
