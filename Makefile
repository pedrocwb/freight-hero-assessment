.PHONY: setup hooks fmt test lint typecheck arch cov eval eval-replay eval-langsmith sop-lint ci all

# Local interpreter for `make setup`. CI gets Python 3.13 from setup-uv instead.
PY ?= /opt/homebrew/bin/python3.13

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
	uv run agent eval run --suite core --mode scripted

# The real model's recorded answers (evals/cassettes): offline, no key.
eval-replay:
	uv run agent eval run --suite core --mode replay

# The real models, pass^3, recorded as a LangSmith experiment. Spends about $0.03 from
# OPENROUTER_API_KEY; needs LANGSMITH_API_KEY. Not part of `make ci`.
eval-langsmith:
	uv run agent eval run --suite core --mode live --k 3 --cost-cap-usd 0.10 --langsmith

sop-lint:
	uv run agent sop lint

# The gates of .github/workflows/ci.yml, in the same order.
ci: lint typecheck arch cov eval eval-replay sop-lint

all: lint typecheck arch cov
