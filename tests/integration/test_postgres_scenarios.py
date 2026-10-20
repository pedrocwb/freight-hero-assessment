"""Every eval scenario, scripted, through the product runtime on Postgres and on the
memory store: each step must leave the same state and produce the same actions.

The contract suites check each repository on its own. This checks them as the runtime
uses them: its write order under deferred foreign keys, its claim, think and commit
units of work, timers that fire and go stale, documents read before the queue runs.

Two comparisons, each over every suite in `evals/scenarios/` (core, agent, docs):

- **Storage.** The runner's own runtime (blobs, document pipeline, config) with only the
  store replaced by Postgres; both runs read the `sops/` files.
- **Storage and knowledge, as production runs.** `agent sop import sops/` (card W3-KNW)
  seeds a database of its own with the brokers and the published sections. Each scenario
  runs on a schema of that database holding a copy of those brokers, and reads the
  sections through `adapters.knowledge.postgres.knowledge_index`. Skipped, with the
  reason, until that card lands.
"""

import asyncio
import inspect
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from typer.testing import CliRunner

from freight_agent import cli
from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.adapters.persistence.postgres.migrate import apply_migrations, scratch_schema
from freight_agent.adapters.persistence.postgres.store import PostgresStore, open_store
from freight_agent.bootstrap import LazyAdapter
from freight_agent.cli_sop import sop_app
from freight_agent.domain.runs import RunRecord
from freight_agent.evalkit import runner as eval_runner
from freight_agent.evalkit.results import StepObservation
from freight_agent.evalkit.runner import RunOutcome, ScenarioRunner
from freight_agent.evalkit.spec import load_scenario
from freight_agent.ports.knowledge import SopIndex
from freight_agent.settings import Settings

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="set DATABASE_URL to run the Postgres tests"),
]

ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "evals"
SOPS = ROOT / "sops"
SCENARIOS = sorted((EVALS / "scenarios").rglob("*.yaml"))
IDS = [f"{p.parent.name}/{p.stem}" for p in SCENARIOS]

# `knowledge_index(settings) -> SopIndex` (card W3-KNW), as the composition root loads it.
KNOWLEDGE_INDEX = LazyAdapter("freight_agent.adapters.knowledge.postgres", "knowledge_index")
# What may differ when the runs read imported brokers and published sections instead of the
# files: which catalog and which content version they recorded, not what they did.
PROVENANCE = {"sop_version", "broker_id", "broker_config_version"}


# --- the runner on Postgres ---------------------------------------------------------------


def _store_in(monkeypatch: pytest.MonkeyPatch, store: PostgresStore) -> None:
    """From now on the runner builds each trial's runtime on `store`: its own runtime (blob
    store, document pipeline, config, seeds) with only the storage replaced."""
    monkeypatch.setattr(eval_runner, "MemoryStore", lambda *args, **kwargs: store)


async def _ran_on(store: PostgresStore, outcome: RunOutcome) -> None:
    """The comparison means something only if the trial really wrote to Postgres."""
    if not outcome.observations:
        return
    load_id = outcome.observations[-1].snapshot.load.load_id
    async with store.unit_of_work() as uow:
        stored = await uow.loads.get(load_id)
    assert stored is not None, (
        "the trial did not run on the Postgres store: does evalkit.runner.build_runtime "
        "still build its store with MemoryStore()?"
    )


def _comparable(observation: StepObservation, *, drop: frozenset[str] = frozenset()) -> Any:
    """A timer event's `data.due_at` is an ISO string made from the timer as stored: the
    memory store keeps the zone it was written in, Postgres returns UTC. Same instant.
    `drop` names run fields left out of the comparison."""
    snapshot = observation.snapshot
    events = tuple(
        event.model_copy(
            update={"data": {**event.data, "due_at": datetime.fromisoformat(event.data["due_at"])}}
        )
        if isinstance(event.data.get("due_at"), str)
        else event
        for event in snapshot.events
    )

    def runs(records: tuple[RunRecord, ...]) -> list[dict[str, Any]]:
        return [record.model_dump(exclude=set(drop)) for record in records]

    return {
        **observation.model_dump(exclude={"snapshot", "runs"}),
        "runs": runs(observation.runs),
        "snapshot": {
            **snapshot.model_copy(update={"events": events}).model_dump(exclude={"runs"}),
            "runs": runs(snapshot.runs),
        },
    }


def _assert_same(
    postgres: RunOutcome, memory: RunOutcome, *, drop: frozenset[str] = frozenset()
) -> None:
    assert postgres.error == memory.error
    assert postgres.failures == memory.failures
    assert [_comparable(o, drop=drop) for o in postgres.observations] == [
        _comparable(o, drop=drop) for o in memory.observations
    ]


async def _on_memory(scenario_path: Path) -> RunOutcome:
    return await ScenarioRunner(EVALS, MemorySopIndex.from_files(SOPS), "scripted").run(
        load_scenario(scenario_path)
    )


# --- storage --------------------------------------------------------------------------------


@pytest.fixture
async def store() -> AsyncIterator[PostgresStore]:
    async with (
        scratch_schema(DATABASE_URL, prefix="scenario") as schema,
        open_store(DATABASE_URL, schema=schema) as opened,
    ):
        yield opened


@pytest.mark.parametrize("path", SCENARIOS, ids=IDS)
async def test_a_scenario_runs_on_postgres_as_on_the_memory_store(
    path: Path, store: PostgresStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = await _on_memory(path)
    _store_in(monkeypatch, store)
    knowledge = MemorySopIndex.from_files(SOPS)

    postgres = await ScenarioRunner(EVALS, knowledge, "scripted").run(load_scenario(path))

    await _ran_on(store, postgres)
    _assert_same(postgres, memory)


# --- storage and knowledge ------------------------------------------------------------------


def _knowledge_missing() -> str | None:
    """Why the imported-knowledge comparison cannot run yet, or None."""
    if KNOWLEDGE_INDEX.load() is None:
        return f"card W3-KNW: {KNOWLEDGE_INDEX.module}.{KNOWLEDGE_INDEX.attribute} is not there yet"
    commands = {c.name or getattr(c.callback, "__name__", "") for c in sop_app.registered_commands}
    if "import" not in commands:
        return "card W3-KNW: `agent sop import` is not there yet"
    if not any(path.name.startswith("0004_") for path in (ROOT / "migrations").glob("*.sql")):
        return "card W3-KNW: migration 0004 (the knowledge tables) is not there yet"
    return None


def _admin(statement: sql.Composed) -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(statement)


def _import_sops(database_url: str, workdir: Path) -> None:
    """`agent sop import sops/`, as an operator runs it against a fresh database: settings
    from the environment only (no `.env`), Postgres, offline (the fake embedder)."""
    with pytest.MonkeyPatch.context() as env:
        for name in Settings.model_fields:
            env.delenv(name.upper(), raising=False)
        env.chdir(workdir)
        for name, value in {
            "PERSISTENCE": "postgres",
            "LLM_MODE": "scripted",
            "DATABASE_URL": database_url,
            "DATABASE_URL_DIRECT": database_url,
            "MODELS_CONFIG": str(ROOT / "config" / "models.yaml"),
            "SOPS_DIR": str(SOPS),
            "EVALS_DIR": str(EVALS),
            "BLOBS_DIR": str(workdir / "blobs"),
        }.items():
            env.setenv(name, value)
        result = CliRunner().invoke(cli.app, ["sop", "import", str(SOPS)])
    assert result.exit_code == 0, result.output


@pytest.fixture(scope="module")
def imported_database(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A database of its own, migrated, into which `agent sop import` loaded `sops/`: the
    brokers and their published sections, in its default schema. Dropped at the end."""
    if (reason := _knowledge_missing()) is not None:
        pytest.skip(reason)
    name = f"scenario_kb_{uuid.uuid4().hex[:10]}"
    try:
        _admin(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    except psycopg.errors.InsufficientPrivilege:
        pytest.skip("the DATABASE_URL user cannot create a database")
    url = make_conninfo(DATABASE_URL, dbname=name)
    try:
        asyncio.run(apply_migrations(url))
        _import_sops(url, tmp_path_factory.mktemp("sop-import"))
        yield url
    finally:
        _admin(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


async def _copy_brokers(source: PostgresStore, target: PostgresStore) -> None:
    """Every saved version of every broker, oldest first, then each current one again."""
    async with source.unit_of_work() as uow:
        current = await uow.brokers.list()
        history = [v for b in current for v in await uow.brokers.versions(b.broker_id)]
    async with target.unit_of_work() as uow:
        for broker in (*history, *current):
            await uow.brokers.save(broker)
        await uow.commit()


@pytest.fixture
async def seeded_store(imported_database: str) -> AsyncIterator[PostgresStore]:
    """A fresh schema of the imported database, holding a copy of its brokers."""
    async with (
        scratch_schema(imported_database, prefix="scenario") as schema,
        open_store(imported_database, schema=schema) as opened,
        open_store(imported_database) as imported,
    ):
        await _copy_brokers(imported, opened)
        yield opened


@pytest.fixture
async def imported_index(imported_database: str) -> AsyncIterator[SopIndex]:
    """The production SOP index over the imported database, opened in this test's loop."""
    options: dict[str, Any] = {  # `_env_file` is an init option type checkers cannot see
        "_env_file": None,
        "persistence": "postgres",
        "llm_mode": "scripted",
        "database_url": imported_database,
        "models_config": ROOT / "config" / "models.yaml",
        "sops_dir": SOPS,
    }
    knowledge_index = KNOWLEDGE_INDEX.load()
    assert knowledge_index is not None  # `imported_database` skipped otherwise
    index: SopIndex = knowledge_index(Settings(**options))
    yield index
    for closer in ("aclose", "close"):
        if callable(close := getattr(index, closer, None)):
            if inspect.isawaitable(result := close()):
                await result
            break


@pytest.mark.parametrize("path", SCENARIOS, ids=IDS)
async def test_a_scenario_runs_on_the_imported_brokers_and_sections_as_on_the_sop_files(
    path: Path,
    seeded_store: PostgresStore,
    imported_index: SopIndex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = load_scenario(path)
    if scenario.broker_file is not None:
        pytest.skip(
            "a broker export brings its own SOP sections instead of the imported ones; "
            "the store-only variant runs this scenario on Postgres"
        )
    memory = await _on_memory(path)
    _store_in(monkeypatch, seeded_store)

    postgres = await ScenarioRunner(EVALS, imported_index, "scripted").run(scenario)

    await _ran_on(seeded_store, postgres)
    _assert_same(postgres, memory, drop=frozenset(PROVENANCE))
