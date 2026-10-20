# Freight load agent

An agent that monitors the events of a freight load (messages, location pings, its own
follow-up timers) and follows each broker's SOP: it asks the driver for an ETA, answers
questions from the load data, follows up, escalates to the dispatcher, the broker or our
Hero team, and does nothing when nothing should be done. Every action it takes is checked
by code before it happens.

| Read | For |
|---|---|
| [`docs/architecture.html`](docs/architecture.html) | The design, in 8 diagrams drawn from the code (open it in a browser) |
| [`docs/answers.md`](docs/answers.md) | Answers to the assignment's questions, with pointers to the code and the evals |
| [`docs/decisions.md`](docs/decisions.md) | The contracts every part follows: SOP format, intents, run pipeline, executor semantics |
| [`sops/README.md`](sops/README.md) | How a non-technical editor writes and changes an SOP |
| [`docs/api.md`](docs/api.md) | The platform API (simulation, ingress, Hero console, inspector, evals) |
| [`evals/RESULTS.md`](evals/RESULTS.md) | Live model rounds: models tried, pass^k, cost |

## Quick start (no keys, no network)

Requirements: Python 3.13 and [uv](https://docs.astral.sh/uv/). Node 22 and pnpm only for
the web UI. Docker is optional.

```bash
make setup     # uv sync (on macOS with Homebrew Python; elsewhere: uv sync --python 3.13)
make eval          # the 22 eval scenarios, in scripted mode
make eval-replay   # the same scenarios on the real models' recorded answers, offline
make test          # unit, contract and property tests
make ci            # everything CI runs: lint, types, layer rules, coverage gate, evals, SOP lint
```

`make eval` runs every scenario through the real agent runtime with a virtual clock and
recording gateways, and grades each step. It ends with a line like:

```
22 scenarios: 22 passed, 0 failed, 0 errors; regression 20/20 pass^k, capability 2/2 pass^k
verdict: passed (exit code 0)
report: evals/results/<timestamp>-core-scripted/report.md
```

The assignment's situation is `evals/scenarios/core/a_client_a_high_risk.yaml`. Run it
alone with:

```bash
uv run agent eval run --scenario a_client_a_high_risk
```

## Evals: three ways to supply the model

| Mode | Model answers come from | Needs | Use |
|---|---|---|---|
| `scripted` | the scenario file's `script` | nothing | the default; tests the agent's code paths (gate rules, SOP selection, validator, ledger, timers, outbox) deterministically, on every PR |
| `replay` | recorded answers in `evals/cassettes/` | nothing | offline, deterministic runs of the real model's answers; a missing recording fails loudly |
| `live` | OpenRouter, the models in `config/models.yaml` | `OPENROUTER_API_KEY` | measures the real model; spends money |
| `record` | live, and saves the answers for replay | `OPENROUTER_API_KEY` | refresh the cassettes after a prompt or model change |

```bash
uv run agent eval run --mode replay
uv run agent eval run --mode live --k 3 --cost-cap-usd 1   # pass^3: a scenario passes only if all 3 trials pass
uv run agent eval list                                    # scenarios and tags
uv run agent eval report evals/results/<run>/results.json --compare evals/results/<baseline>/results.json
uv run agent sop lint                                     # SOP files + "is every section covered by a scenario?"
```

Exit codes: 0 pass, 1 a regression scenario failed, 2 the harness could not run (bad
scenario, missing cassette or key), 3 the cost cap stopped the run. Scenarios tagged
`capability` may fail without failing the run; `regression` ones may not.

Live runs need the key in `.env` (copy `.env.example`; `.env` is gitignored and a
pre-commit hook refuses to commit it). Always pass `--cost-cap-usd`.

**LangSmith experiments.** `--langsmith` records a live (or record) run as a LangSmith
experiment; `make eval-langsmith` runs the core suite at pass^3 that way:

```bash
uv run agent eval run --suite core --mode live --k 3 --cost-cap-usd 0.10 --langsmith
```

- The suite's scenarios are synced to the dataset `freight-agent-<suite>` first: one example
  per scenario (inputs: the load, the clock, each step's message or ping; reference outputs:
  each step's `expect` and `forbid`), rewritten only when a scenario file changes. The
  dataset gets the workspace's `Agent` resource tag named after `LANGSMITH_PROJECT`, so it
  and its experiments show on that agent's Datasets & Experiments page.
- The run is one experiment on that dataset, named `<suite>-<mode>-<model set>-<UTC time>`,
  with the run's metadata (models, git sha, SOP version, prompt and config hashes, k).
- Each trial is one row, linked to its example. Under it are the scenario's steps, the
  agent's LangGraph nodes and the real model calls (`gate · openai/gpt-4.1-nano`), with
  their prompts, answers and tokens.
- The graders' verdicts are feedback on each row: `passed` (the failed checks are its
  comment), `checks_failed`, `cost_usd`, `model_calls` and `pass_hat_k`. The experiment
  itself gets pass^k and pass@k, overall and per group, plus cost and exit code.

The graders and the exit code stay the harness's own (`results.json` and `report.md` are
written as always); LangSmith is where runs are compared side by side. Scripted and replay
runs are refused: they measure the code, not the model. The flag needs
`LANGSMITH_API_KEY`, and the dataset and experiment are created before the first trial, so
a run never spends model money for an experiment LangSmith cannot store. After that,
LangSmith errors are logged and never change the verdict.

**Models.** The gate runs on `openai/gpt-4.1-nano` and decide on `openai/gpt-oss-120b`
(`config/models.yaml`), chosen by a live bake-off of six cheap models against thresholds
fixed before the rounds (`evals/RESULTS.md`): 22/22 scenarios at pass^3, about $0.0002 per
agent run, about $0.03 per load. Claude Haiku 5.5 also passed 22/22 at about five times the
cost; try it with `--model-set haiku`. `make eval-replay` replays the chosen models'
recorded answers offline; after a prompt, SOP, scenario or model change, re-record with
`uv run agent eval run --mode record --cost-cap-usd 0.20` (about a cent).

## The simulation platform

A web page to drive a load through time and play every role: the driver's phone, the main
email thread (dispatcher, broker), the broker's Slack, the Hero console. The agent
inspector shows, for each run, the gate's decision, the SOP sections it read, the plan,
what the validator dropped, the ledger and the cost.

```bash
uv run agent serve                       # API on http://localhost:8000
cd web && pnpm install && pnpm dev       # UI on http://localhost:5173 (proxies /api to :8000)
```

Or build the UI once and let the API serve it on port 8000:

```bash
cd web && pnpm install && pnpm build && cd .. && uv run agent serve
```

The agent's model in the platform comes from `LLM_MODE`. `replay` (the default) only
answers prompts that were recorded from eval scenarios, so free-form play needs
`LLM_MODE=live uv run agent serve` and spends from the OpenRouter key; the platform never
switches to the live model on its own. The reset dialog can also pick the mode per session.

To replay the assignment: create load 481207 from the fixture, send a `high` delay-risk
ping, then from the driver's phone ask "What's the PO number?", advance the clock to 07:31
and to 08:01, and watch the SMS, the email to the dispatcher and the Hero task appear.

### Docker

```bash
docker compose up --build                # UI + API on http://localhost:8000, Postgres, migrations
LLM_MODE=live docker compose up          # the live model (reads OPENROUTER_API_KEY from .env)
docker compose run --rm app eval run --suite core --mode replay
docker compose down                      # add -v to drop the database volume
```

Ports are published on 127.0.0.1 only.

## Postgres

The agent's state (event log, obligations ledger, timers, outbox, runs, per-load queue)
lives behind ports with two implementations: in memory (tests, evals, the platform) and
Postgres (production; works through Neon's pooler). Both pass the same contract suite.

```bash
docker compose up -d postgres
DATABASE_URL=postgresql://agent:agent@localhost:5432/agent uv run agent db migrate
DATABASE_URL=postgresql://agent:agent@localhost:5432/agent uv run pytest -m "integration or contract"
```

`PERSISTENCE=postgres` makes the runtime use it. The integration tests also run all 22
scenarios on Postgres and check the state matches the memory store at every step.

## Configuration

Copy `.env.example` to `.env`. Every value is optional for the quick start.

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | unset | live and record modes |
| `LLM_MODE` | `replay` | `scripted`, `replay`, `live` or `record` |
| `PERSISTENCE` | `memory` | `memory` or `postgres` |
| `DATABASE_URL`, `DATABASE_URL_DIRECT` | unset | pooled URL for the app, direct URL for migrations |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | off | one trace per agent run; tracing never breaks a run. `LANGSMITH_API_KEY` also enables `agent eval run --langsmith` |
| `MODELS_CONFIG`, `SOPS_DIR`, `EVALS_DIR`, `CASSETTES_DIR`, `WEB_DIST` | repo paths | where config, SOPs, evals, cassettes and the built UI live |

Secrets are read as secrets: they are never printed, logged or returned by the API
(`/api/health` shows them only as set or unset).

## Repository layout

```
src/freight_agent/
  domain/        entities and rules: ledger, timers, ping filter, SOP merge, validator rules (pure)
  ports/         interfaces: repositories, gateways, language model, clock, SOP source, tracer
  application/   run context, prefilter, gate, decide, executor, the LangGraph run graph, runtime
  adapters/      memory and Postgres stores, OpenRouter / cassette / scripted models, SOP files, LangSmith
  evalkit/       scenario spec, runner, graders, suites and reports
  api/           FastAPI platform (simulation, ingress, Hero console, inspector, evals, SSE)
  bootstrap.py   the composition root: the only place that wires adapters to ports
  cli.py         the `agent` command
sops/            standard, client and client + load-type SOP sections (Markdown)
evals/           scenarios, fixtures, cassettes, results (gitignored)
config/          models per role, prices, bake-off candidates
migrations/      numbered SQL
web/             React + Vite platform UI
tests/           unit, contract, property, integration, evalkit
```

The layer rules (domain imports nothing outside itself; application never imports
adapters; adapters never import application) are enforced by import-linter in CI and by a
unit test. pyright runs in strict mode on `domain`, `ports` and `application`.

## What is mocked or not built

The integration layer (SMS, email, Slack, TMS, the internal system) is replaced by
recording gateways: actions are delivered to a log, not sent. Document reading is a stub
that creates a Hero task. There is no production worker process or deployment. The design
of each missing part is in [`docs/answers.md`](docs/answers.md).
