# Freight load agent

An agent that watches a truckload's events (driver SMS, dispatcher and broker email and
Slack, location pings with a delay risk, its own follow-up timers, attachments such as a
POD) and acts on each one as the client's SOP says: it asks for an ETA, answers from the
load data, reads a document, follows up, escalates to the dispatcher, the broker or the
internal Hero team, and does nothing when nothing should be done. A LangGraph run graph
holds the model inside one bounded tool loop; every action is checked by code at the call
and happens only when the run commits. Each inbound message is first read by a decision
model (TypeSafe's Jev, on OpenRouter) that answers its intent and whether it needs the
agent as probabilities, not text; code decides from them, and a doubtful message goes on
to the agent (`GATE_ENGINE=llm` reads it with a chat model instead). A facility memory
keeps what drivers said about a pickup or delivery place ("check in at gate 4") for the
next load of the same client there: extracted after delivery, checked in code, and shown to
the agent as untrusted notes.

| Read | For |
|---|---|
| [`docs/answers.md`](docs/answers.md) | The answers to the assignment's questions, numbered as asked |
| [`docs/architecture.md`](docs/architecture.md) | The design in eleven Mermaid diagrams |
| [`docs/decisions.md`](docs/decisions.md) | The contracts: SOP format and catalog, intents, run graph and budgets, executor, event ids, prompts |
| [`evals/RESULTS.md`](evals/RESULTS.md) | Live eval results from LangSmith: pass^3, cost, latency, the model choice |
| [`docs/api.md`](docs/api.md) | The platform API |
| [`sops/README.md`](sops/README.md) | How a non-technical editor writes and changes an SOP |
| [`web/README.md`](web/README.md) | The platform UI |

## The assignment's situation: load 481207

Client A (`northline`), delay risk goes high at 07:00. The agent texts the driver for the
ETA at 07:01 and sets a 30-minute follow-up. At 07:12 the driver asks for the PO; at 07:13
the agent answers "55821" from the load's instructions, and the ETA request stays open with
its 07:31 follow-up. At 07:31 it asks again; at 08:01, with no ETA after two requests, it
emails the dispatcher in the main thread and creates a Hero task. The scenario is
[`evals/scenarios/core/a_client_a_high_risk.yaml`](evals/scenarios/core/a_client_a_high_risk.yaml);
one play costs about $0.003 on OpenRouter. Three ways to watch it:

1. **The platform.** Start it (below), open http://localhost:5173, choose
   `a_client_a_high_risk` on the Scenarios screen and press Run. Each step shows its
   checks; each run links to the Load inspector (its path through the graph, its tool calls,
   its actions, the ledger and timers) and to its LangSmith trace. The inspector's free
   play lets you write as the driver instead.
2. **LangGraph Studio.** `make studio`, open the Studio URL it prints, and run the graph
   `scenario` with the input `{"scenario_id": "a_client_a_high_risk"}`. Each run of the
   load is a visit of the `run` node, with the run graph inside it.
3. **An eval.** `uv run agent eval run --suite core --scenario a_client_a_high_risk --k 3 --cost-cap-usd 0.05`
   plays it three times as a LangSmith experiment and prints pass^3.

## Requirements

- Python 3.13 and [uv](https://docs.astral.sh/uv/) (0.8 or later).
- For the UI only: Node 22 (20.19 or later works) and pnpm 10.
- Optional: Docker with Compose 2.24 or later.
- Keys, for anything that calls a model or LangSmith: an OpenRouter key and a LangSmith key
  (see "What needs which key").

## Setup

```bash
uv sync --python 3.13                    # or `make setup` on macOS with Homebrew's python3.13
cp .env.example .env                     # then fill in OPENROUTER_API_KEY and LANGSMITH_API_KEY
(cd web && pnpm install)                 # only for the UI
make hooks                               # optional: the pre-commit hooks (they refuse a committed .env)
```

**Storage.** `PERSISTENCE` chooses where each load's memory lives (the event log, the
obligations ledger, timers, outbox, run records, the event queue, documents): `memory`, the
default, is gone when the process stops; `postgres` keeps it in the database at
`DATABASE_URL`, so loads, runs and the ledger survive a restart. `docker compose up` runs
the platform on Postgres by itself. To run `uv run agent serve` on Postgres from the host:

```bash
docker compose up -d postgres            # Postgres 17 with pgvector on 127.0.0.1:5432
export PERSISTENCE=postgres DATABASE_URL=postgresql://agent:agent@localhost:5432/agent
uv run agent db migrate                  # creates the tables; a rerun applies only new files
uv run agent serve
```

A reset of the simulation (and a scenario, which resets first) empties the tables. The
evals and LangGraph Studio always play each scenario in a fresh in-memory world, whatever
`PERSISTENCE` says.

**Facility memory.** What a driver says about a place ("check in at gate 4") is kept for
the client's next load there. On `PERSISTENCE=postgres` the notes live in the same database,
in LangGraph's Postgres store with a pgvector index on their text (embedded with
`openai/text-embedding-3-small` on the OpenRouter key, or an offline hash without it);
`agent db migrate` creates its tables. In memory otherwise. To look at them:

```bash
uv run agent memory list --client northline   # every note of a client, by facility
uv run agent memory list --load 481207        # the notes of each stop of a load
```

The platform shows them per stop in the load inspector (`GET /api/loads/{id}/facility-memory`).

**With your own LangSmith workspace, publish the prompts once.** The runtime reads its
prompt templates from the LangSmith Prompt Hub at the pins of `config/prompts.yaml`
(`freight-agent-<role>:prod`). A new workspace does not have them, and the process refuses
to start with a pin it cannot resolve (the message says so). Either publish the reviewed
copy in `prompts/` to your workspace:

```bash
uv run agent prompts push --tag prod     # creates freight-agent-gate, -agent, -extract, -judge, -gate_decision
uv run agent prompts status              # every role: "matches"
```

or run the repository copy with `PROMPTS_SOURCE=local` in `.env`. Without a LangSmith key
the runtime reads `prompts/` on its own.

## What needs which key

| Works with no key | Needs `OPENROUTER_API_KEY` | Needs `LANGSMITH_API_KEY` |
|---|---|---|
| `make test`, `make ci`, `uv run agent eval list`, `uv run agent sop lint`, `pnpm test` in `web/`; the platform and Studio start, and a run that needs the model escalates to a Hero saying the key is missing | Every run that calls a model: the platform's scenarios and free play, Studio plays, the evals | `agent eval run` (datasets and experiments), `agent prompts ...`, the Prompt Hub pins, tracing (with `LANGSMITH_TRACING=true`) |

Keys are read from `.env` or the environment as secrets: never printed, logged or returned
by the API. `.env.example` lists every setting with its default.

## Commands

| Command | What it does | Cost |
|---|---|---|
| `make test` | Unit, contract, property and integration tests with a fake model (`tests/fakes.py`) | free, about 20 s |
| `make ci` | What CI runs: ruff, pyright (strict on the core), import-linter layer rules, tests with the 90% coverage gate, SOP lint. The Postgres tests skip: no database needed | free, about 1 min |
| `make integration` | The Postgres tests: every contract suite on Postgres too, the migrations, a platform restart, the assignment's scenario on both stores. Needs `docker compose up -d postgres` (or `TEST_DATABASE_URL=...`); each test uses a schema or database of its own and drops it | free, about 10 s |
| `uv run agent db migrate` | Applies the pending files of `migrations/` to `DATABASE_URL`, each once (recorded in `schema_migrations`) | free |
| `uv run agent eval list` | The scenarios of each suite with their tags | free |
| `uv run agent sop lint` | Checks every SOP file and catalog; warns about sections no scenario covers | free |
| `uv run agent eval run --suite core --k 3 --cost-cap-usd 0.15` | One suite live, as a LangSmith experiment: pass^k table and the experiment's link. Suites: `core`, `agent`, `docs`, `memory`. `--scenario ID` for one; `--prompt-tag candidate` to measure a prompt candidate | core $0.03 to $0.04 per repetition; the cap stops it (exit 3) |
| `make eval` | Every suite at k=1 (`EVAL_K`, `EVAL_COST_CAP_USD` per suite, default $0.10) | about $0.04 |
| `uv run agent memory list --client C` / `--load L`, `extract --load L` | The facility notes of a client or of a load's stops; run a load's extraction now (one live `memory` call) | extract: about $0.001 |
| `uv run agent prompts status` / `push --tag T` / `pull` | Compare `prompts/` with the Hub pins; publish changed templates and move tag T; write a Hub version back | free |
| `uv run agent serve` | The platform API on http://127.0.0.1:8000 (`/docs` for OpenAPI) | runs spend on the model |
| `cd web && pnpm dev` | The UI on http://localhost:5173, proxying `/api` to port 8000 | |
| `cd web && pnpm build`, then `uv run agent serve` | The API serves the built UI on port 8000 | |
| `make studio` | The local LangGraph Agent Server for Studio, on port 2024 (`STUDIO_ARGS=--tunnel` for Safari) | each play spends on the model |
| `docker compose up --build` | Postgres, a one-shot `migrate` job, the UI and API on http://localhost:8000 (on Postgres: a restart keeps the loads), and the Agent Server for Studio on port 2024 (open `https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024`); keys from `.env`. `docker compose up --build app` for the platform and its database only; `docker compose down -v` also drops the database | runs and plays spend on the model |

`agent eval run` exit codes: 0 every regression scenario passed pass^k, 1 one failed, 2 the
run could not start (a bad scenario, a missing key, LangSmith down), 3 the cost cap stopped
it. Scenarios tagged `capability` are measured but do not fail the run. Live evals never
run in CI; `.github/workflows/evals-live.yml` runs them by hand.

## Repository layout

```
src/freight_agent/
  domain/        entities and rules, no I/O: load, events, obligations, timers, ping filter,
                 actions, validation rules, SOP merge, client catalog, documents, run records
  interfaces/         interfaces: repositories, SOP index, language model, decision model,
                 prompts, gateways, blobs, page reader, clock, tracer
  application/   run context, prefilter, gate, agent and its tools, the LangGraph run graph
                 (graph/), executor, escalations, runtime, document pipeline, facility
                 memory extraction, prompt variables
  adapters/      memory and Postgres stores, facility memory on LangGraph's store (in
                 memory or Postgres with pgvector) with embeddings, OpenRouter with retries, fallbacks and circuit breakers,
                 OpenRouter's Decisions API (the gate's decision model), SOP files and
                 lint, Prompt Hub and local prompts, pypdfium2 pages, blobs, LangSmith
                 links, recording gateways
  evalkit/       scenario spec, player, graders, judge, LangSmith experiments, metadata
  api/           FastAPI platform: simulation, ingress, scenarios, SOPs, documents, stream
  bootstrap.py   the composition root: the only place that wires adapters to interfaces
  cli.py         the `agent` command (cli_sop.py, cli_prompts.py, cli_memory.py)
  studio.py      LangGraph Studio's scenario graph (langgraph.json)
sops/            the SOPs: standard, client and load-type sections, client catalogs
prompts/         the reviewable copy of the six prompt templates
migrations/      the Postgres schema, numbered SQL files (`agent db migrate`)
config/          models per role with prices and the gate's decision model (models.yaml),
                 prompt pins (prompts.yaml)
evals/           scenarios (core, agent, docs, memory), fixtures (load 481207, POD and BOL PDFs),
                 RESULTS.md
web/             React and Vite platform UI
tests/           unit, contract, property, integration, eval harness
```

## What is mocked or not built

- **Integrations are recording gateways.** SMS, email, Slack, the TMS and the Hero task
  tool record what they would send; nothing leaves the process.
- **Storage is in memory by default, or Postgres** (`PERSISTENCE=postgres`, the compose
  setup), behind repository interfaces whose contract suites both stores pass; the facility
  notes follow the same setting. Attachment bytes are files under `BLOBS_DIR` on Postgres,
  in memory otherwise.
- **No always-on worker or scheduler.** `tick()` fires timers, reads documents, runs the
  queue and delivers the outbox; the platform, the evals and Studio call it on a virtual
  clock.
- **Not built, designed in [`docs/answers.md`](docs/answers.md):** a durable queue and
  workers at scale (5.2; the Postgres queue claims with `SKIP LOCKED`, but one process
  drives it), an SOP editor with review and approval, SOP versions
  with canary and rollback (2.3), hybrid search over SOPs (2.2), metrics and alerts (4.1),
  the per-load cost cap and a rolling summary of old events (5.3). An earlier build of
  several of these is at the git tag `full-platform`.
