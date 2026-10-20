# Simulation platform (web)

The page for evaluating the load agent by playing every role: move the virtual clock,
send delay-risk pings, text as the driver, email as the dispatcher or the broker, post in
the broker's Slack, work the Hero queue, and inspect every agent run.

React 18, TypeScript and Vite. No UI kit and no runtime CDN or web font: one stylesheet
with CSS variables, light and dark through `prefers-color-scheme`.

## Run it

Start the API on port 8000 first (see the repository README). Then:

```sh
cd web
pnpm install
pnpm dev            # http://localhost:5173, /api is proxied to http://localhost:8000
```

| Command | What it does |
|---|---|
| `pnpm dev` | Dev server on :5173 with the `/api` proxy |
| `pnpm build` | Type-check, then build to `dist/` |
| `pnpm preview` | Serve `dist/` on :4173 (same proxy) |
| `pnpm typecheck` | `tsc --noEmit` |
| `pnpm test` | Vitest unit tests |

## Screens

| Screen | What you do there |
|---|---|
| Control room | Pick or create a load (fixture JSON, editable), send a low, medium or high ping, set the inbound latency, see the load card, the TMS notes and every load |
| Driver phone | The SMS thread; you write as the load's driver, and can attach a file (a POD or BOL photo) |
| Main email thread | The thread with the dispatcher, the broker and the agent; write as either contact, with an attachment if you like |
| Broker Slack | The broker's channel with the agent's alerts; write as the broker |
| Hero console | Tasks across loads, urgent first; filter open, done or all; complete with a note |
| Agent inspector | Each run: trigger, status, intent, the broker and config version it ran on, gate intent and reason, SOP sections, the trajectory (every tool call with its arguments, result, accepted or rejected, cost), rejections and sections searched, the staged plan, validator drops, tokens, cost, latency; the ledger, the load's documents with their checks, the timers and the spend per load against $1 |
| SOP viewer | Sections that apply for a broker, load types, intent (from the broker's catalog) and stage, the catalog, the SOP version, and the agent's SOP search |
| Evals | Scenarios with tags; run the suite or one scenario in scripted or replay mode, k trials, or a whole suite on one broker (also live, with a cost cap); results with pass^k, failures, cost and the LangSmith link of a live run |
| Health | Status and reasons, queue age against the 5-minute SLA, run p95, spend per load, outbox, settings (secrets only as set or unset) |
| Brokers | Each broker's profile: intents (system ones read-only), obligation types, document types and requirements, limits and the agent's budget (`max_steps: 0` is the single-plan mode); create one as a copy of the standard broker; save as a new config version; the rollout (share of loads on the current version, rollback to 0%) and the version list |
| SOP studio | Upload a broker's SOP (PDF or Markdown), review the compiled draft (sections with their intents, stages, mode and source pages; proposed intents with evidence, document requirements and obligations), see the lint, publish (optionally as a canary), list the published versions, run the broker's eval suite |

The clock sits beside every screen: +1, +5 and +30 minutes, "To next timer" (advances to
`next_timer_due_at`), tick and reset. Every time is shown in the current stop's timezone
with its abbreviation, for example "07:31 PDT".

## How it is built

- `src/api.ts`: the typed client. Shapes mirror the API contract and the Python models it
  serializes (the wave 3 ones `api/schemas.py`: brokers, knowledge, documents, broker
  evals; each names the backend card that serves it). `createApi(fetch)` takes the fetch
  function, which the tests use. Intents and brokers are data from the API
  (`GET /api/brokers`, `GET /api/brokers/{id}`); only the stages are fixed in the page.
- `src/hooks/useEventStream.ts`: `EventSource` on `/api/stream` (`run`, `outbox`, `clock`,
  `load`). While the stream is down, every panel refreshes every 5 seconds instead.
- `src/lib/bus.ts` and `src/hooks/useResource.ts`: panels subscribe to refresh topics;
  stream events, polling and the page's own writes publish them, coalesced per burst.
- `src/lib/`: pure helpers (time formatting, conversation merge, eval summary, the
  trajectory table, broker catalog edits and checks, the SOP draft's pre-publish lint),
  unit tested.
- `src/screens/`: one file per screen.

Errors from the API (`{"detail": ...}`) show as toasts; a panel whose data fails to load
shows the error inline with a retry button.
