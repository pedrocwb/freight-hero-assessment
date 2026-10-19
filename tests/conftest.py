"""Shared fixtures. Fronts add their own fixtures in their subdirectory's conftest.py."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from freight_agent.adapters.channels.fakes.recording import RecordingGateways
from freight_agent.adapters.clock import SequentialIdGenerator, VirtualClock
from freight_agent.adapters.persistence.memory.store import MemoryStore
from freight_agent.domain.load import Load

ROOT = Path(__file__).resolve().parents[1]
LA = ZoneInfo("America/Los_Angeles")


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
