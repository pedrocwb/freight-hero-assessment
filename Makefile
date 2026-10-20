.PHONY: setup hooks fmt test lint typecheck arch cov eval eval-replay eval-langsmith bakeoff sop-lint integration diagrams ci all

# Local interpreter for `make setup`. CI gets Python 3.13 from setup-uv instead.
PY ?= /opt/homebrew/bin/python3.13

# The scenario suites `make eval` and `make eval-replay` run, in this order: core (the
# assignment and the SOP procedures), agent (the tool loop's guardrails) and docs (the
# document pipeline). A suite runs once its folder exists under evals/scenarios/.
SUITES ?= core agent docs
EVAL_SUITES := $(notdir $(wildcard $(addprefix evals/scenarios/,$(SUITES))))

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
	uv run pytest -m "not integration and not eval_live"

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

typecheck:
	uv run pyright

arch:
	uv run lint-imports

# The coverage threshold lives in pyproject.toml ([tool.coverage.report] fail_under).
cov:
	uv run pytest -m "not integration and not eval_live" --cov --cov-report=term-missing

# Scripted mode: no network and no model.
eval:
	$(call each_suite,--mode scripted)

# The real model's recorded answers (evals/cassettes): offline, no key.
eval-replay:
	$(call each_suite,--mode replay)

# The real models, pass^3, recorded as a LangSmith experiment. Spends about $0.03 from
# OPENROUTER_API_KEY; needs LANGSMITH_API_KEY. Not part of `make ci`.
eval-langsmith:
	uv run agent eval run --suite core --mode live --k 3 --cost-cap-usd 0.10 --langsmith

# The wave 3 bake-off of the agent role (evals/RESULTS.md, "Wave 3"): k=1 screening of each
# candidate set (config/models.NAME.yaml) on every suite, live, as LangSmith experiments.
# Spends from OPENROUTER_API_KEY and needs LANGSMITH_API_KEY. The caps per suite add up to
# $0.40 per model set ($1.20 for the three); a suite that passes its cap stops with exit 3
# and the next one starts. Then `--k 3` on the best two, by hand. Not part of `make ci`.
BAKEOFF_SETS ?= gptoss haiku gemini
BAKEOFF_K ?= 1
BAKEOFF_CAP_core ?= 0.30
BAKEOFF_CAP_agent ?= 0.05
BAKEOFF_CAP_docs ?= 0.05
bakeoff:
	@status=0; for set in $(BAKEOFF_SETS); do for suite in $(EVAL_SUITES); do \
		case $$suite in core) cap=$(BAKEOFF_CAP_core);; agent) cap=$(BAKEOFF_CAP_agent);; \
			docs) cap=$(BAKEOFF_CAP_docs);; *) cap=0.05;; esac; \
		echo "== bake-off: --model-set $$set, suite $$suite, cap \$$$$cap"; \
		uv run agent eval run --suite $$suite --mode live --k $(BAKEOFF_K) --model-set $$set \
			--cost-cap-usd $$cap --langsmith || { code=$$?; [ $$code -gt $$status ] && status=$$code; }; \
	done; done; exit $$status

sop-lint:
	uv run agent sop lint

# The Postgres tests and every contract suite on Postgres, as CI's integration job runs
# them. Needs a database: `docker compose up -d postgres`.
DATABASE_URL ?= postgresql://agent:agent@localhost:5432/agent
integration:
	DATABASE_URL=$(DATABASE_URL) uv run pytest -m "integration or contract" -rs

# Rebuilds docs/architecture.html from docs/build_diagrams.js (needs Node).
diagrams:
	node docs/build_diagrams.js $(or $(TMPDIR:%/=%),/tmp)/architecture-page.html docs/architecture.html

# The gates of .github/workflows/ci.yml, in the same order.
ci: lint typecheck arch cov sop-lint eval eval-replay

all: lint typecheck arch cov
