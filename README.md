<!-- generated-by: gsd-doc-writer -->
# Freight load agent

An agent that follows a truckload's events (driver SMS, email, location pings with a delay
risk, its own follow-up timers, documents) and acts as each client's SOP says. This page is
how to run the agent and the evals on your computer. The design and the answers to the
assignment's questions are in [docs/answers.md](docs/answers.md).

The UI groups live operations by broker: Northline and Keystone from the assignment,
Aurora's extreme follow-up case, and Acme's document cases. Each load runs on Temporal
with a saved SOP version. Models run live; channel gateways record test deliveries.

## 1. Requirements

- **Docker** with Compose 2.24 or later, for the quickest start.
- For host development: **Python 3.13** with [uv](https://docs.astral.sh/uv/) 0.8+, and
  **Node 22** with pnpm 10 for the UI. Postgres and Temporal are still required for
  live operations.
- An **OpenRouter key** (required: the agent calls its models through OpenRouter).
- A **LangSmith key** (optional: experiments, traces and the Prompt Hub).

## 2. Set up

Clone the repository and configure the local environment:

```bash
git clone https://github.com/pedrocwb/freight-hero-assessment.git
cd freight-hero-assessment
cp .env.example .env        # set OPENROUTER_API_KEY; LANGSMITH_API_KEY is optional
```

Without `LANGSMITH_API_KEY` the agent reads its prompts from `prompts/` and sends no traces.
With your own LangSmith key, run `uv run agent prompts push --tag prod` once, or set
`PROMPTS_SOURCE=local` ([why](docs/development.md#prompts-with-your-own-langsmith-workspace)).

## 3. Run the agent

```bash
docker compose -f compose.temporal.yml up -d --build  # durable API, workers and timers
docker compose up -d --build app                     # API bridge and UI on port 8000
```

Open <http://localhost:8000/#/scenarios?broker=northline>:

1. Select a broker and a scenario, then **Iniciar ao vivo**. A fresh load is created
   with the broker's saved SOP and an initial location ping.
2. Use **Enviar esta entrada** for sample messages/documents or the contact/channel
   composer for your own replies. Follow the conversation, decisions, obligations and
   deadlines in **Operações**. Wait steps use real Temporal timers.
3. In **SOPs e configurações**, edit or extend procedures and select **Salvar para
   novas execuções**. Start another scenario to use that revision; existing loads keep
   their snapshot. Use **Parar execução** to stop a load while preserving its history.

For example, Northline's `a_client_a_high_risk` asks for an ETA, answers a PO question
without resetting the wait, then retries and escalates according to its saved catalog.
Keystone uses a broker Slack alert; Aurora demonstrates ten requests with one-minute
waits. Inputs are manual and the UI does not grade the run. See
[Live broker workspaces](docs/live-workspace.md) for the presentation flow.

Stop both stacks with `docker compose down` and
`docker compose -f compose.temporal.yml down`; named volumes preserve their data.
For host processes, follow [Temporal setup](docs/temporal.md#run-locally) and
[development setup](docs/development.md). Starting `agent serve` alone does not start
the durable worker or ingress.

## 4. Run the evals

The evals are YAML scenarios in `evals/scenarios/` (`core`, `agent`, `docs`, `memory`, `grounding`). Each
one plays a load step by step on the real models and checks what the agent did, including
the steps where the right action is to do nothing.

| Command | What it does | Needs | Model spending |
|---|---|---|---|
| `uv run agent play a_client_a_high_risk b_ack_with_nothing_open --no-upload` | Plays scenarios and prints inputs, actions, rationale and checks | OpenRouter | Live calls; varies by run |
| `uv run agent eval run --suite core --k 3 --cost-cap-usd 0.15` | A suite as a LangSmith experiment, three times each | OpenRouter, LangSmith | Budget guard at $0.15 |
| `make eval` | All five suites once, including grounding | OpenRouter, LangSmith | Budget guard at $0.10 per suite by default |
| `make test` | Automated tests with scripted model responses; database/Temporal suites skip unless enabled | uv dependencies | No paid model calls |

Automated scenarios advance a virtual clock. The live UI uses real time; neither the
browser countdown nor a scenario's original timestamps drive Temporal execution.
Cost guards check accumulated usage between plays, so one play can cross the limit.
The CLI reads SOPs from files; it does not automatically evaluate a saved or draft UI
profile. See [SOP authoring](sops/README.md) for the two configuration paths.

In Docker, prefix the commands with `docker compose exec app` instead of `uv run`, for
example `docker compose exec app agent play a_client_a_high_risk --no-upload`.

Results we measured are in [evals/RESULTS.md](evals/RESULTS.md).

The [stop-grounding eval](docs/stop-grounding.md) checks the facility used at each
load stage, including SMS requests, Slack escalations and pickup departure.

## Read more

- [docs/answers.md](docs/answers.md): the answers to the assignment's questions, and what is
  built or only designed.
- [docs/architecture.md](docs/architecture.md): the design in diagrams.
- [docs/development.md](docs/development.md): every command, Postgres, prompts, keys, LangGraph
  Studio and the repository layout.

## Durable scheduling with Temporal

The real-clock integration runs one active Workflow per load, the existing LangGraph
in an Activity, and broker-configured SOP waits measured from confirmed outbound
requests (30 minutes in the seeded Northline and Keystone catalogs).
Run `docker compose -f compose.temporal.yml up --build` for the isolated local stack
(API on 8001, Temporal UI on 8233). The platform UI connects to this stack through
`/api/durable`; the eval CLI and Studio use virtual time.

See [the Temporal guide](docs/temporal.md) for ingress examples, restart/retry behavior,
provider idempotency requirements and production limits. `make temporal-test` validates
the real Workflow and LangGraph with time skipping and a scripted model.

Temporal persists deadlines across worker restarts. Due work still depends on worker,
model and provider availability; a durable timer is not a promise of delivery at the
exact wall-clock second.
