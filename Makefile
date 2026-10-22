.PHONY: setup hooks fmt test lint typecheck arch cov eval sop-lint integration studio ci all

# Local interpreter for `make setup`. CI gets Python 3.13 from setup-uv instead.
PY ?= /opt/homebrew/bin/python3.13

# The scenario suites `make eval` runs, in this order: core (the assignment and the SOP
# procedures), agent (the tool loop's outcomes), docs (the document pipeline) and memory
# (the facility memory), grounding (the operational stop at each load stage).
# A suite runs once its folder exists under evals/scenarios/.
SUITES ?= core agent docs memory grounding
EVAL_SUITES := $(notdir $(wildcard $(addprefix evals/scenarios/,$(SUITES))))
# Repetitions per scenario, and the most one suite's experiment may spend (USD).
EVAL_K ?= 1
EVAL_COST_CAP_USD ?= 0.10

# Runs `agent eval run --suite S <args>` for every suite of EVAL_SUITES, even after a
# failure, and exits with the highest exit code (1 regression, 2 harness, 3 cost cap).
define each_suite
	@status=0; for suite in $(EVAL_SUITES); do \
		uv run agent eval run --suite $$suite $(1) || { code=$$?; [ $$code -gt $$status ] && status=$$code; }; \
	done; exit $$status
endef

setup:
	uv sync --python $(PY)

# Installs the git hooks in .pre-commit-config.yaml (run once per clone).
hooks:
	uv run pre-commit install

# Fixes first, then formats: a lint fix can leave code that needs formatting.
fmt:
	uv run ruff check --fix src tests
	uv run ruff format src tests

test:
	uv run pytest

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

typecheck:
	uv run pyright

arch:
	uv run lint-imports

# The coverage threshold lives in pyproject.toml ([tool.coverage.report] fail_under).
cov:
	uv run pytest --cov --cov-report=term-missing

# Every suite live on OpenRouter, each as a LangSmith experiment. It spends money from
# OPENROUTER_API_KEY (about $0.05 for the three suites at k=1) and needs LANGSMITH_API_KEY.
# Not part of `make ci`.
eval:
	@echo "make eval: the $(EVAL_SUITES) suites, live on OpenRouter, k=$(EVAL_K). This spends money: at most \$$$(EVAL_COST_CAP_USD) per suite."
	$(call each_suite,--k $(EVAL_K) --cost-cap-usd $(EVAL_COST_CAP_USD))

sop-lint:
	uv run agent sop lint

# The Postgres tests: every contract suite on Postgres, the migrations, the platform's
# broker profiles and the assignment's scenario on both stores, as CI's integration job runs them.
# Needs a database: `docker compose up -d postgres`. Each test works in a schema (or a
# database) of its own and drops it, so this database may hold data. Without a database
# these tests skip, which keeps `make test` and `make ci` offline.
TEST_DATABASE_URL ?= postgresql://agent:agent@localhost:5432/agent
integration:
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) uv run pytest -m integration -rs

# LangGraph Studio: the local Agent Server (`langgraph dev`, in memory) serves the scenario
# graph of src/freight_agent/studio.py (langgraph.json), and Studio, in the LangSmith UI,
# connects to it. Each scenario played calls the live model on OPENROUTER_API_KEY (about
# $$0.003 for scenario A). Safari blocks the page from reaching localhost: there, run
# `make studio STUDIO_ARGS=--tunnel` and open the URL the server prints. The SOP index
# reads its files on every run, synchronously, which the server's blocking-call check
# would refuse: hence --allow-blocking.
STUDIO_PORT ?= 2024
STUDIO_ARGS ?=
studio:
	@echo "Agent Server: http://127.0.0.1:$(STUDIO_PORT) (API docs at /docs). Open LangGraph Studio:"
	@echo "  https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:$(STUDIO_PORT)"
	@echo "Safari blocks localhost there: run 'make studio STUDIO_ARGS=--tunnel' and open the URL it prints."
	@echo "Input: {\"scenario_id\": \"a_client_a_high_risk\"}. Each play spends money on OPENROUTER_API_KEY."
	uv run langgraph dev --no-browser --port $(STUDIO_PORT) --allow-blocking $(STUDIO_ARGS)

# The gates of .github/workflows/ci.yml, in the same order. No evals: they are live.
ci: lint typecheck arch cov sop-lint

all: lint typecheck arch cov

# Downloads the Temporal time-skipping test server on first run; no LLM calls.
.PHONY: temporal-test temporal-up
temporal-test:
	RUN_TEMPORAL_TESTS=1 uv run pytest tests/temporal -v

temporal-up:
	docker compose -f compose.temporal.yml up --build
