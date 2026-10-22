# Legacy flow cleanup

Baseline: `a99613f` on `main`. The supported interactive product is the broker
workspace: every live load starts through Temporal, with a pinned SOP and an initial
location ping. This cleanup removes the previous interactive simulator.

## Findings and changes

| Finding | Evidence | Change |
|---|---|---|
| Unmounted simulator UI | Starting at `web/src/main.tsx`, imports reach only App, BrokerWorkspace, BrokerSops, Durable and their live helpers. Conversation, Scenarios and SopViewer have no application entry point. | Remove those screens and their exclusive components, hooks, state and helpers: 28 unreachable production modules. |
| Unused frontend HTTP client | The live UI calls `durableRequest`; the old `createApi` only serves the removed screens. | Remove the client and its obsolete tests; retain domain wire types in `web/src/domain.ts`. |
| Duplicate execution model in the HTTP server | `api/app.py` constructed SimulationService and ScenarioRunner at startup, although every mounted screen uses `/api/durable`. | Remove the virtual-clock HTTP API, SSE bus, clock controls, recording/YAML writer, upload routes and simulator service. The server now only serves assets, liveness and the durable proxy. |
| Aurora-specific start path | The workspace already starts Aurora with the same profile/revision flow as other brokers. | Remove `/demos/aurora*`, the original Aurora proxy routes, `start_extreme_demo` and policy authoring. Retain the snapshot reader and UI fallback for existing loads. |
| Unused presentation dependencies | Mermaid and clsx were imported only by unmounted UI modules; multipart was used only by the retired upload route. | Remove direct dependencies and update both lockfiles. Remove 241 CSS rules with no remaining class consumers. |
| Obsolete deployment dependencies | The UI no longer needs its own Postgres, migration job or attachment volume. | Default Compose starts only the UI/proxy. Studio and a database for integration tests remain explicitly selectable. The live stack continues to own data and timers. |
| Obsolete image write paths | Only the retired recording endpoint wrote generated eval YAML and uploaded fixtures. | Remove those writable directories; only the durable blob directory needs write permission. |

The remaining nonempty Python modules are reachable from CLI, UI server, durable
ingress/worker or Studio entry points in an import scan that includes literal dynamic
adapter references. This is evidence about module reachability, not a claim that every
function can be proven used by static analysis.

## Deliberately retained

- VirtualClock, ScenarioPlayer, eval fixtures and Studio: these run repeatable model
  evaluations and scripted tests independently of live Temporal operations.
- Recording gateways and in-memory stores: the assessment uses simulated channel
  delivery, and offline tests depend on these adapters.
- Both gate engines, document processing, facility memory, fallback models and prompt
  adapters: each has an active configuration or execution path.
- Generic durable registration and broker/load ingress: integration clients use these
  independently of scenario fixtures.
- `pinned_index`, old snapshot fields and load/inspector bookmark redirects: existing
  operations must keep their original SOP and remain readable and stoppable.
- The shared persistence builders: the durable worker still uses their connection
  lifecycle. No data migration or volume deletion is part of the cleanup.
- Acme's detention SOP: a section may be retrieved dynamically even without a named
  scenario. SOP lint still reports its pre-existing missing eval tag; lack of a fixture
  is not sufficient evidence to delete a broker policy.

## Compatibility and deployment

The retired HTTP routes return 404; see [API replacements](api.md#platform-liveness-and-retired-routes).
External scripts using reset/advance, multipart uploads or recording-to-YAML need to
move to durable ingress or CLI eval fixtures. Live workspace URLs and request contracts
remain intact. Docker liveness now uses `/api/health`; `/api/durable/health` checks the
separate ingress. Existing named volumes and saved profiles are retained.

Rebuild the UI and durable images when deploying this branch. Running containers keep
their existing images until replaced; no operation was stopped as part of the audit.

## Validation

- Backend suite: **1,964 passed**, 102 skipped without explicit infrastructure flags;
  **99.07%** branch-aware coverage for `domain` and `application` (minimum 90%).
- Temporal time-skipping suite: **8 passed**, including ten one-minute SMS intervals,
  escalation after the last wait, retries and cooperative stop.
- Postgres suite: **94 passed** using isolated temporary schemas/databases.
- Frontend: **13 passed**; TypeScript and production bundle passed. Tests for removed
  screens/helpers were removed with their code. Docker builds now run the remaining
  frontend tests before bundling.
- Formatting, lint, Python types and all five architecture contracts passed.
- SOP lint: zero errors; the existing Acme detention coverage warning remains.
- Browser read-only check: broker catalog, SOP editor and an existing Aurora load from
  the original demo render through the new UI/proxy; the saved two-attempt SOP and
  escalation history remain visible.
- Docker image build and isolated container smoke passed: liveness/UI, retired-route
  404, unavailable-ingress 503, OpenAPI, read-only eval fixtures and writable blobs.
- All 61 local documentation links resolve.
- No paid model evaluation or external channel delivery was needed for this cleanup.
