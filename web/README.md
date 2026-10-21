# Platform (web)

The page for testing the load agent: a load is a conversation. Write as its driver,
dispatcher or broker, press Send and wait, and read what the agent answered; click an
answer for why. Save a conversation as an eval scenario, and play the scenarios live.
LangSmith keeps the deep traces and the eval experiments; the page links to them.

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
| Conversation | One load (picked in its header when there are several): one line with its id, client, stage, delay risk and next stop, then the chat. Messages from the driver, dispatcher and broker on the left, the agent's SMS, emails and broker Slack messages on the right, each with its channel and local time; system events (a risk change, a timer that fired, a document read) and the agent's quiet work (a Hero task, a TMS note, a stage change, a run that did nothing) as small markers. The composer writes as one of the load's contacts on one of their channels, with a file if you like: Send and wait sends it, lets the latency pass, runs the agent and shows its answer, or says it decided to do nothing. Quieter: a location ping (low, medium, high) and "Skip to next timer". Clicking an agent message or marker opens "why" on the right: what the run understood (the event, the intent, the gate's call and its probability), the SOP sections it followed and the facility notes it saw, each action with a check or a cross and the rule's reason, what it left open, and its own one-line answer. Details, folded: the graph path and the run graph, the tool calls, the model calls, tokens, cost, latency and the LangSmith trace. Load facts (a drawer) has the requests, timers, documents, facility notes and the load. Save as scenario drafts the recorded steps with expectations from what happened; uncheck some, add forbid lines, edit the notes, save to `evals/scenarios/<suite>/<id>.yaml`. |
| Scenarios | The scenarios of `evals/scenarios/` by suite, one line each with Run. Run plays one live in a reset world and opens its load's conversation, where each step's checks show as a pass or fail line (a failure opens to the checks that failed). |
| SOPs | The sections that apply for a client, load types, intent and stage, the client's catalog, and the agent's SOP search. Read-only. |

The clock in the top bar is the simulation's, in the current stop's timezone. Times are
local to the stop ("07:31 PDT"). Inputs wait while a scenario plays, since it moves the
clock.

Links carry their target in the hash: `#/load?load=481207&run=run_5626026aac1c` opens the
conversation on that run's explanation (old `#/inspector` links still work).

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
  (`lib/testFixtures.ts`): the chat assembled from the event log and the outbox
  (`conversation.ts`), the "why" of a run (`why.ts`), the send-and-wait state
  (`sendFlow.ts`), the scenario drafted from a recording (`scenarioDraft.ts`), the run's
  path on the graph (`graph.ts`: Mermaid with the highlight), the scenarios
  (`scenarios.ts`), time formatting and the hash routes.
- `src/screens/`: one file per screen; `src/components/`: their parts.

Errors from the API (`{"detail": ...}`) show as toasts; a panel whose data fails to load
shows the error inline with a retry button.
