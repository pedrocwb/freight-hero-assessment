"""Every eval scenario, scripted, through the product runtime on Postgres and on the
memory store: each step must leave the same state and produce the same actions.

The contract suite checks each repository on its own. This checks them as the runtime
uses them: its write order under deferred foreign keys, its claim, think and commit
units of work, timers that fire and go stale.
"""

import os
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import pytest

from freight_agent.adapters.clock import SequentialIdGenerator
from freight_agent.adapters.observability.null import NullTracer
from freight_agent.adapters.persistence.postgres.migrate import scratch_schema
from freight_agent.adapters.persistence.postgres.store import PostgresStore, open_store
from freight_agent.adapters.sop.files import FileSopRepository
from freight_agent.application.runtime import AgentRuntime, RuntimeDeps
from freight_agent.evalkit.results import StepObservation
from freight_agent.evalkit.runner import RuntimeFactory, ScenarioRunner
from freight_agent.evalkit.spec import load_scenario
from freight_agent.ports.clock import Clock
from freight_agent.ports.gateways import Gateways
from freight_agent.ports.llm import LanguageModel
from freight_agent.ports.sop import SopRepository

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="set DATABASE_URL to run the Postgres tests"),
]

ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "evals"
SCENARIOS = sorted((EVALS / "scenarios").rglob("*.yaml"))


@pytest.fixture
async def store() -> AsyncIterator[PostgresStore]:
    async with (
        scratch_schema(DATABASE_URL, prefix="scenario") as schema,
        open_store(DATABASE_URL, schema=schema) as opened,
    ):
        yield opened


def _on(store: PostgresStore) -> RuntimeFactory:
    def build(clock: Clock, model: LanguageModel, gateways: Gateways, sop: SopRepository):
        deps = RuntimeDeps(
            uow_factory=store.unit_of_work,
            clock=clock,
            ids=SequentialIdGenerator(),
            llm=model,
            sop=sop,
            gateways=gateways,
            tracer=NullTracer(),
        )
        return AgentRuntime(deps)

    return build


def _as_instants(observation: StepObservation) -> StepObservation:
    """A timer event's `data.due_at` is an ISO string made from the timer as stored: the
    memory store keeps the zone it was written in, Postgres returns UTC. Same instant."""
    snapshot = observation.snapshot
    events = tuple(
        event.model_copy(
            update={"data": {**event.data, "due_at": datetime.fromisoformat(event.data["due_at"])}}
        )
        if isinstance(event.data.get("due_at"), str)
        else event
        for event in snapshot.events
    )
    return observation.model_copy(
        update={"snapshot": snapshot.model_copy(update={"events": events})}
    )


@pytest.mark.parametrize("path", SCENARIOS, ids=[p.stem for p in SCENARIOS])
async def test_a_scenario_runs_on_postgres_as_on_the_memory_store(
    path: Path, store: PostgresStore
) -> None:
    scenario = load_scenario(path)
    sop = FileSopRepository(ROOT / "sops")

    memory = await ScenarioRunner(EVALS, sop, "scripted").run(scenario)
    postgres = await ScenarioRunner(EVALS, sop, "scripted", runtime_factory=_on(store)).run(
        scenario
    )

    assert postgres.error == memory.error
    assert postgres.failures == memory.failures
    assert [_as_instants(o) for o in postgres.observations] == [
        _as_instants(o) for o in memory.observations
    ]
