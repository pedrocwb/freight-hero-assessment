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

Without the backend, `pnpm dev:mock` runs the page on an in-memory mock (`VITE_MOCK=1`)
seeded with the assignment's story (load 481207, steps A1 to A4) and a Keystone load.
The mock records messages and pings but runs no agent; the header says "Mock data".

| Command | What it does |
|---|---|
| `pnpm dev` | Dev server on :5173 with the `/api` proxy |
| `pnpm dev:mock` | Same, on the mock backend |
| `pnpm build` | Type-check, then build to `dist/` |
| `pnpm preview` | Serve `dist/` on :4173 (same proxy) |
| `pnpm typecheck` | `tsc --noEmit` |
| `pnpm test` | Vitest unit tests |

## Screens

| Screen | What you do there |
|---|---|
| Control room | Pick or create a load (fixture JSON, editable), send a low, medium or high ping, set the inbound latency, see the load card, the TMS notes and every load |
| Driver phone | The SMS thread; you write as the load's driver |
| Main email thread | The thread with the dispatcher, the broker and the agent; write as either contact |
| Broker Slack | The broker's channel with the agent's alerts; write as the broker |
| Hero console | Tasks across loads, urgent first; filter open, done or all; complete with a note |
| Agent inspector | Each run: trigger, status, gate intent and reason, SOP sections, plan, validator drops, skip or escalation reason, tokens, cost, latency; the ledger, the timers and the spend per load against $1 |
| SOP viewer | Sections that apply for a client, load types, intent and stage, the catalog, the SOP version |
| Evals | Scenarios with tags; run the suite or one scenario in scripted or replay mode, k trials; results with pass^k, failures and cost |
| Health | Status and reasons, queue age against the 5-minute SLA, run p95, spend per load, outbox, settings (secrets only as set or unset) |

The clock sits beside every screen: +1, +5 and +30 minutes, "To next timer" (advances to
`next_timer_due_at`), tick and reset. Every time is shown in the current stop's timezone
with its abbreviation, for example "07:31 PDT".

## How it is built

- `src/api.ts`: the typed client. Shapes mirror the API contract and the Python models it
  serializes. `createApi(fetch)` takes the fetch function, which the tests and the mock use.
- `src/hooks/useEventStream.ts`: `EventSource` on `/api/stream` (`run`, `outbox`, `clock`,
  `load`). While the stream is down, every panel refreshes every 5 seconds instead.
- `src/lib/bus.ts` and `src/hooks/useResource.ts`: panels subscribe to refresh topics;
  stream events, polling and the page's own writes publish them, coalesced per burst.
- `src/lib/`: pure helpers (time formatting, conversation merge, eval summary), unit tested.
- `src/screens/`: one file per screen. `src/mocks/`: the mock backend, loaded only when
  `VITE_MOCK=1`, so it is not in the normal build.

Errors from the API (`{"detail": ...}`) show as toasts; a panel whose data fails to load
shows the error inline with a retry button.
