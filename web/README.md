# Platform (web)

The page for testing the load agent: play the eval scenarios live and watch their checks,
then open any load to read its story, event by event, with each run's path through the
graph, its tool calls, its answer and the actions it took. LangSmith keeps the deep traces
and the eval experiments; the page links to them.

React 18, TypeScript and Vite. No UI kit and no runtime CDN or web font: one stylesheet
with CSS variables, light and dark through `prefers-color-scheme`. Mermaid draws the run
graph and loads on demand, in chunks of its own.

## Run it

Start the API on port 8000 first (`uv run agent serve`, see the repository README). Then:

```sh
cd web
pnpm install
pnpm dev            # http://localhost:5173, /api is proxied to http://localhost:8000
```

`API_URL=http://localhost:8765 pnpm dev` proxies to an API on another port.

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
| Scenarios | The scenarios of `evals/scenarios/` by suite, with their tags, description and steps. Run plays one live on OpenRouter in a reset world; each step shows its time, note, status and checks (with the detail of a failure), the run's spend, and links to the runs it caused in the inspector. Disabled, with the reason, while the model has no key or another scenario plays. A link to the LangSmith project when tracing is on. |
| Load inspector | One load (picked in the top bar): a timeline in load time where each inbound event (a message, a ping, a timer, a document) is followed by the run it caused. The open run shows its status, intent, gate, SOP sections, cost and tokens; its path on the run graph (visited nodes and edges highlighted, visit counts such as `agent ×3`); its trajectory (each tool call with its arguments and result, accepted or refused, in order); its answer; its actions by channel (SMS, email, broker Slack, TMS note, Hero task) with their delivery, the ledger changes and what the validator dropped; and its LangSmith trace. Beside it: free play (write as one of the load's contacts on one of their channels, attach a file, send a delay-risk ping, set the inbound latency), the ledger, the timers, the documents and the load's facts. New load creates one from a fixture. |
| SOPs | The sections that apply for a client, load types, intent and stage, the client's catalog, and the agent's SOP search. Read-only. |

The clock sits beside every screen: +1, +5 and +30 minutes, "To next timer", tick and
reset. It waits while a scenario plays, since the scenario moves it. Every time is shown
in the current stop's timezone with its abbreviation, for example "07:31 PDT".

Links carry their target in the hash: `#/inspector?load=481207&run=run_5626026aac1c`
opens the inspector on that run, `#/scenarios?id=a_client_a_high_risk` on that scenario.

## How it is built

- `src/api.ts`: the typed client. Shapes mirror the API contract and the Python models
  it serializes. `createApi(fetch)` takes the fetch function, which the tests use.
- `src/hooks/useEventStream.ts`: `EventSource` on `/api/stream` (`run`, `outbox`,
  `clock`, `load`, `scenario`). While the stream is down, every panel refreshes every
  5 seconds instead; while a scenario plays, its progress is also read every second.
- `src/lib/bus.ts` and `src/hooks/useResource.ts`: panels subscribe to refresh topics;
  stream events, polling and the page's own writes publish them, coalesced per burst.
- `src/state/sim.tsx`: the clock, the loads, the selected load's snapshot, and the
  scenario that plays or played last.
- `src/lib/`: pure helpers, unit tested on typed fixtures of the contract
  (`lib/testFixtures.ts`): the timeline (`timeline.ts`: events, their runs, the actions
  by channel), the run's path on the graph (`graph.ts`: Mermaid with the highlight), the
  scenarios and a run's progress (`scenarios.ts`), the trajectory table, time formatting
  and the hash routes.
- `src/screens/`: one file per screen; `src/components/`: their parts.

Errors from the API (`{"detail": ...}`) show as toasts; a panel whose data fails to load
shows the error inline with a retry button.
