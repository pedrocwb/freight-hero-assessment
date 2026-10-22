# Freight load agent

> **Live UI:** broker workspaces, editable SOPs and all scenarios on Temporal. See [Live broker workspaces](docs/live-workspace.md) for setup and behavior.


An agent that follows a truckload's events (driver SMS, email, location pings with a delay
risk, its own follow-up timers, documents) and acts as each client's SOP says. This page is
how to run the agent and the evals on your computer. The design and the answers to the
assignment's questions are in [docs/answers.md](docs/answers.md).

## 1. Requirements

- **Docker** with Compose 2.24 or later, for the quickest start.
- Or, without Docker: **Python 3.13** with [uv](https://docs.astral.sh/uv/) 0.8+, and
  **Node 22** with pnpm 10 for the UI.
- An **OpenRouter key** (required: the agent calls its models through OpenRouter).
- A **LangSmith key** (optional: experiments, traces and the Prompt Hub).

## 2. Set up

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
docker compose up --build   # Postgres, the API and the UI on http://localhost:8000
```

Open http://localhost:8000 and play the assignment's situation (load 481207):

1. **Scenarios** → `a_client_a_high_risk` → **Run**. Four steps play live: an SMS asking the
   driver for the ETA (07:01), the answer to "What's the PO number?" (07:13), a second SMS
   (07:31), then an email to the dispatcher and a Hero task (08:01). All four pass.
2. Or by hand on the **Conversation** screen: **New load** `load_481207`, Location ping
   **High**, write as the driver "What's the PO number?" and press **Send and wait**, then
   **Skip to next timer** twice. Click any reply of the agent to see why it wrote it.

Each agent reply costs about $0.001. More cases to try are in
[docs/development.md](docs/development.md#more-test-cases). `docker compose down` stops
everything.

Without Docker:

```bash
uv sync --python 3.13
uv run agent serve                    # the API on http://127.0.0.1:8000
cd web && pnpm install && pnpm dev    # the UI on http://localhost:5173
```

## 4. Run the evals

The evals are YAML scenarios in `evals/scenarios/` (`core`, `agent`, `docs`, `memory`, `grounding`). Each
one plays a load step by step on the real models and checks what the agent did, including
the steps where the right action is to do nothing.

| Command | What it does | Needs | Cost |
|---|---|---|---|
| `uv run agent play a_client_a_high_risk b_ack_with_nothing_open --no-upload` | Plays scenarios and prints, step by step, what came in, what the agent did and why, and which checks passed | OpenRouter | ~$0.003 per scenario |
| `uv run agent eval run --suite core --k 3 --cost-cap-usd 0.15` | A whole suite as a LangSmith experiment, 3 times each; prints the pass rate and a link | OpenRouter, LangSmith | ~$0.10 |
| `make eval` | Every suite once | OpenRouter, LangSmith | ~$0.04 |
| `make test` | The unit and integration tests, with a fake model | nothing | free, ~20 s |

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

The real-clock integration now runs one active Workflow per load, the existing LangGraph
in an Activity, and durable SOP waits (ETA follow-up: 30 minutes from confirmed delivery).
Run `docker compose -f compose.temporal.yml up --build` for the isolated local stack
(API on 8001, Temporal UI on 8233). The original simulation UI keeps its virtual clock.

See [the Temporal guide](docs/temporal.md) for ingress examples, restart/retry behavior,
provider idempotency requirements and production limits. `make temporal-test` validates
the real Workflow and LangGraph with time skipping and a scripted model.

The platform's **Operação ao vivo** tab (`http://localhost:8000/#/durable`) starts
Aurora Transportes' extreme scenario: ten driver SMS with one-minute durable waits,
then an urgent Hero task after the final wait. It shows confirmed contacts, deadlines
and agent decisions as they happen. See the guide above to update both local stacks.
The model is live; channel gateways record test deliveries.
