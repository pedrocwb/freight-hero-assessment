# Freight load agent

An agent that monitors the events of a freight load (messages, location pings, documents,
its own follow-up timers) and follows each broker's SOP: it asks the driver for an ETA,
answers questions from the load data, reads a POD, follows up, escalates to the
dispatcher, the broker or our Hero team, and does nothing when nothing should be done. It
works in a bounded tool loop: every action it proposes is checked by code at the call and
only happens after the run commits.

Brokers are onboarded without code: a broker is a catalog of intents, obligations and
documents, plus its own SOP uploaded as a PDF, compiled into a draft, reviewed, published
as a version and rolled out to a share of its loads.

| Read | For |
|---|---|
| [`docs/architecture.html`](docs/architecture.html) | The design, in 10 diagrams drawn from the code (open it in a browser) |
| [`docs/answers.md`](docs/answers.md) | Answers to the assignment's questions, with pointers to the code and the evals |
| [`docs/decisions.md`](docs/decisions.md) | The contracts every part follows: SOP format, intents, brokers, the run pipeline, executor semantics, documents |
| [`sops/README.md`](sops/README.md) | How a non-technical editor writes and changes an SOP |
| [`docs/api.md`](docs/api.md) | The platform API (simulation, ingress, brokers, SOPs, documents, Hero console, inspector, evals) |
| [`evals/RESULTS.md`](evals/RESULTS.md) | Live model rounds: models tried, pass^k, cost; the wave 3 bake-off |

## Quick start (no keys, no network)

Requirements: Python 3.13 and [uv](https://docs.astral.sh/uv/). Node 22 and pnpm only for
the web UI. Docker is optional.

```bash
make setup         # uv sync (on macOS with Homebrew Python; elsewhere: uv sync --python 3.13)
make eval          # every eval suite (core, agent, docs), in scripted mode
make eval-replay   # the same suites on the real models' recorded answers, offline
make test          # unit, contract and property tests
make ci            # everything CI runs: lint, types, layer rules, coverage gate, SOP lint, evals
```

`make eval` runs every scenario through the real agent runtime with a virtual clock and
recording gateways, and grades each step. Each suite ends with a line like:

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
| `scripted` | the scenario file's `script` (an agent entry may carry its tool calls) | nothing | the default; tests the agent's code paths (triage rules, SOP selection, the transcript fold, the rules, ledger, timers, outbox) deterministically, on every PR |
| `replay` | recorded answers in `evals/cassettes/` | nothing | offline, deterministic runs of the real model's answers, tool loop included; a missing recording fails loudly |
| `live` | OpenRouter, the models in `config/models.yaml` | `OPENROUTER_API_KEY` | measures the real model; spends money |
| `record` | live, and saves the answers for replay | `OPENROUTER_API_KEY` | refresh the cassettes after a prompt or model change |

| Suite | What it checks |
|---|---|
| `core` (22 scenarios) | The assignment's sequence and the standard procedures, for Client A, Client B and a reefer load |
| `agent` | The loop's guardrails: an outside email refused at the call, the refusal budget, a gap searched before it goes to a Hero |
| `docs` | The document pipeline with real PDFs: a clean POD, a POD with exceptions, an overdue POD |
| `<broker>` | A broker's own scenarios, run with its export (`--broker-file evals/brokers/<broker>.json`) |

Graders check facts (actions, recipients, ledger, timers, statuses), the trajectory
(`tools_used`, `sections_read`, `max_rejections`) and, with a rubric, the wording (an LLM
judge on the `judge` role, which never counts as an agent call).

```bash
uv run agent eval run --suite agent --mode replay
uv run agent eval run --mode live --k 3 --cost-cap-usd 1   # pass^3: a scenario passes only if all 3 trials pass
uv run agent eval list                                    # scenarios and tags
uv run agent eval report evals/results/<run>/results.json --compare evals/results/<baseline>/results.json
uv run agent eval run --broker-file evals/brokers/<broker>.json   # a suite on an exported broker
uv run agent eval draft --broker-file evals/brokers/<broker>.json # draft scenarios from its sections
uv run agent eval lint --broker-file evals/brokers/<broker>.json  # an intent with no scenario is an error
uv run agent sop lint                                     # SOP files + "is every section covered by a scenario?"
```

Exit codes: 0 pass, 1 a regression scenario failed, 2 the harness could not run (bad
scenario, missing cassette or key), 3 the cost cap stopped the run. Scenarios tagged
`capability` may fail without failing the run; `regression` ones may not. `make eval`
runs every suite even after one fails, and exits with the worst code.

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
  their prompts, tool calls, answers and tokens.
- The graders' verdicts are feedback on each row: `passed` (the failed checks are its
  comment), `checks_failed`, `cost_usd`, `model_calls`, `pass_hat_k`, and for the loop
  `judge_score`, `rejections` and `tools_used`. The experiment itself gets pass^k and
  pass@k, overall and per group, plus cost and exit code.

The graders and the exit code stay the harness's own (`results.json` and `report.md` are
written as always); LangSmith is where runs are compared side by side. Scripted and replay
runs are refused: they measure the code, not the model. The flag needs
`LANGSMITH_API_KEY`, and the dataset and experiment are created before the first trial, so
a run never spends model money for an experiment LangSmith cannot store. After that,
LangSmith errors are logged and never change the verdict.

**Models.** One model per role (`config/models.yaml`): the triage gate on
`openai/gpt-4.1-nano`; the agent loop on `openai/gpt-oss-120b`, the wave 2 bake-off's
choice on the single-plan design (22/22 at pass^3, about $0.0002 per run); the judge,
`extract` (documents, page images included), `compile` (SOP drafts) and the embedding
model for SOP search. The agent role was measured again on the loop by the wave 3
bake-off, which also gives the measured cost per load. Its thresholds are fixed in
`evals/RESULTS.md` ("Wave 3: the agent loop") and its results are filled there. `make eval-replay` replays the chosen models' recorded answers offline;
after a prompt, SOP, scenario or model change, re-record each suite with
`uv run agent eval run --suite S --mode record --cost-cap-usd 0.20`.

## The simulation platform

A web page to drive a load through time and play every role: the driver's phone (with
attachments), the main email thread (dispatcher, broker), the broker's Slack, the Hero
console. The agent inspector shows, for each run, the triage decision, the agent's intent,
the broker config version, the SOP sections it read and searched, its tool calls (accepted
or refused, with their cost), what the validator dropped, the ledger and the cost. The
Brokers and SOP Studio screens onboard a broker.

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

## Onboarding a broker without code

The same steps from the UI (Brokers, then SOP Studio) or the API (`docs/api.md`). The
example is Acme Freight, whose procedure is `evals/fixtures/sops/acme_sop.pdf`: a 3-page
PDF in Acme's own layout, with a detention procedure and a POD rule that the standard
catalog does not have. Compiling a new PDF needs a live model (`LLM_MODE=live uv run agent
serve`, about $0.05); every other step works offline.

```bash
API=http://localhost:8000/api

# 1. The broker: a copy of the standard catalog (12 intents, eta_request, 4 document types).
curl -s -X POST $API/brokers -H 'content-type: application/json' \
  -d '{"broker_id": "acme", "name": "Acme Freight"}'

# 2. Its SOP, as it is: stored, read page by page, compiled into a draft.
curl -s -X POST $API/brokers/acme/sops -F file=@evals/fixtures/sops/acme_sop.pdf

# 3. Review: sections with Acme's own words, the proposed intent detention_request, the
#    proposed requirement "pod at at_delivery within 120 minutes", and what a publish
#    would refuse now. Accept or edit, then save the whole draft back.
curl -s $API/brokers/acme/sops/draft > draft.json
curl -s -X PUT $API/brokers/acme/sops/draft -H 'content-type: application/json' -d @draft.json

# 4. Publish: lint, embeddings, an immutable version, config_version 2, rollout 100%
#    with previous_version 1 (a percent below 100 starts a canary).
curl -s -X POST $API/brokers/acme/sops/publish -H 'content-type: application/json' -d '{"percent": 100}'
curl -s $API/brokers/acme/versions

# 5. The suite on Acme's new version, before more loads get it.
curl -s -X POST $API/brokers/acme/evals/run -H 'content-type: application/json' \
  -d '{"suite": "core", "mode": "replay"}'

# 6. Roll back if needed: every Acme load goes back to config version 1.
curl -s -X POST $API/brokers/acme/rollout -H 'content-type: application/json' -d '{"percent": 0}'
```

Then a load with `broker.id = acme` runs on that catalog and SOP: a driver "at the
receiver" moves the stage to `at_delivery`, which opens the `pod` obligation with its
120-minute timer; a POD uploaded to `POST /api/loads/{id}/attachments` is read on the next
tick, checked against the load, and its `document_processed` run moves the load to
`delivered`. The inspector shows the intent, config version 2 and the trajectory.

For the repository's own brokers, `sops/` is the seed: `uv run agent sop import sops/`
publishes the standard, `northline` (Client A) and `keystone` (Client B) versions into
the database, idempotently.

### Docker

```bash
docker compose up --build                # UI + API on http://localhost:8000
LLM_MODE=live docker compose up          # the live model (reads OPENROUTER_API_KEY from .env)
docker compose run --rm app eval run --suite core --mode replay
docker compose down                      # add -v to drop the blobs volume
```

Ports are published on 127.0.0.1 only. Uploaded attachments and SOP files are kept in the
`blobs` volume (`BLOBS_DIR=/app/blobs`); the image writes nowhere else but eval results.

## Configuration

Copy `.env.example` to `.env`. Every value is optional for the quick start.

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | unset | live and record modes: model calls and SOP embeddings |
| `LLM_MODE` | `replay` | `scripted`, `replay`, `live` or `record`; offline modes embed with a deterministic fake |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | off | one trace per agent run; tracing never breaks a run. `LANGSMITH_API_KEY` also enables `agent eval run --langsmith` |
| `MODELS_CONFIG`, `SOPS_DIR`, `EVALS_DIR`, `CASSETTES_DIR`, `WEB_DIST` | repo paths | where config, SOPs, evals, cassettes and the built UI live |
| `BLOBS_DIR` | `blobs` | bytes of uploaded attachments and SOP files, created on the first upload |

Secrets are read as secrets: they are never printed, logged or returned by the API.

## Repository layout

```
src/freight_agent/
  domain/        entities and rules (pure): brokers and their catalog, ledger, timers, ping
                 filter, SOP merge, documents, actions, validator rules, runs
  ports/         interfaces: repositories (with brokers, SOP store, documents), SOP index,
                 embedder, blobs, page reader, document processor, gateways, language
                 model, clock, tracer
  application/   run context, prefilter, triage, the agent loop and its tools, executor,
                 the LangGraph run graph, runtime, document pipeline, SOP ingestion
  adapters/      the memory store, OpenRouter / cassette / scripted models, the SOP
                 index (memory, pgvector) and embedders, SOP files, lint and importer, blobs
                 and the PDF page reader, LangSmith
  evalkit/       scenario spec, runner, graders, judge, scenario drafts, suites and reports
  api/           FastAPI platform (simulation, ingress, brokers, SOPs, documents, Hero
                 console, inspector, evals, SSE)
  bootstrap.py   the composition root: the only place that wires adapters to ports
  cli.py         the `agent` command (`cli_sop.py`: `agent sop import|lint`)
sops/            the seed SOPs: standard, client and client + load-type sections (Markdown)
evals/           scenarios (core, agent, docs, per broker), broker exports, fixtures (the
                 load, the Acme SOP, POD and BOL PDFs), cassettes, results (gitignored)
config/          models per role, prices, the embedding model
web/             React + Vite platform UI
tests/           unit, contract, property, integration, evalkit
```

The layer rules (domain imports nothing outside itself; application never imports
adapters; adapters never import application) are enforced by import-linter in CI and by a
unit test. pyright runs in strict mode on `domain`, `ports` and `application`.

## What is mocked or not built

The integration layer (SMS, email, Slack, TMS, the internal system) is replaced by
recording gateways: actions are delivered to a log, not sent. Blobs are files under
`BLOBS_DIR`, not an object store. There is no production worker process or deployment,
and the canary's comparison with the previous version is a query, not a report yet. The
design of each missing part is in [`docs/answers.md`](docs/answers.md).
