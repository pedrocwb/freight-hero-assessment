.PHONY: setup test lint typecheck arch cov eval all

PY ?= /opt/homebrew/bin/python3.13

setup:
	uv sync --python $(PY)

test:
	uv run pytest -m "not integration and not eval_live"

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

typecheck:
	uv run pyright

arch:
	uv run lint-imports

cov:
	uv run pytest -m "not integration and not eval_live" --cov --cov-report=term-missing

eval:
	uv run agent eval run --suite core --mode scripted

all: lint typecheck arch cov
