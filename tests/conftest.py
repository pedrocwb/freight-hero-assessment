"""Shared fixtures. Fronts add their own fixtures in their subdirectory's conftest.py."""

import json
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import langsmith
import pytest

from freight_agent.adapters.channels.fakes.recording import RecordingGateways
from freight_agent.adapters.clock import SequentialIdGenerator, VirtualClock
from freight_agent.adapters.persistence.memory.store import MemoryStore
from freight_agent.domain.load import Load

ROOT = Path(__file__).resolve().parents[1]
LA = ZoneInfo("America/Los_Angeles")
_TRACING_PREFIXES = ("LANGSMITH_", "LANGCHAIN_")


@pytest.fixture(autouse=True)
def _no_tracing_to_langsmith(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test traces to LangSmith: LangGraph and LangChain read their switch from the
    process environment, so every LangSmith variable is cleared for the test, and one the
    code under test sets (`bootstrap.enable_native_tracing`) is gone after it."""
    for name in [n for n in os.environ if n.startswith(_TRACING_PREFIXES)]:
        monkeypatch.delenv(name)
    yield
    for name in [n for n in os.environ if n.startswith(_TRACING_PREFIXES)]:
        del os.environ[name]


@pytest.fixture(autouse=True)
def _no_prompt_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reads or writes the LangSmith Prompt Hub: a test of the Hub adapter or of
    `agent prompts` gives it a fake client, and anything else that reaches the real one
    fails here instead of calling the network."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a test called the LangSmith Prompt Hub")

    for name in ("pull_prompt", "pull_prompt_commit", "push_prompt", "get_prompt"):
        monkeypatch.setattr(langsmith.Client, name, refuse)


@pytest.fixture
def load_metadata() -> dict[str, Any]:
    with (ROOT / "evals" / "fixtures" / "load_481207.json").open() as handle:
        return json.load(handle)


@pytest.fixture
def load(load_metadata: dict[str, Any]) -> Load:
    return Load.from_metadata(load_metadata)


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock(datetime(2026, 10, 13, 6, 55, tzinfo=LA))


@pytest.fixture
def ids() -> SequentialIdGenerator:
    return SequentialIdGenerator()


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def gateways() -> RecordingGateways:
    return RecordingGateways()
