"""The platform on `PERSISTENCE=postgres`: loads, runs, the ledger and the facility notes
survive a restart of the API, and a reset empties the database.

Each test gets a database of its own, migrated as `agent db migrate` does (the SQL files,
then the facility memory's LangGraph store) and dropped afterwards (the platform uses the
database's default schema, as in compose).
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from freight_agent.adapters.memory.postgres import setup_store
from freight_agent.adapters.memory.store import StoreFacilityMemory
from freight_agent.adapters.persistence.postgres.migrate import migrate
from freight_agent.domain.load import Stage
from freight_agent.domain.memory import ExtractionStatus, FacilityFact, FactKind
from tests.fakes import FakeModel
from tests.integration.test_api_harness import (
    ASK_ETA,
    DECIDE,
    FOLLOW_UP_IN_30,
    LOAD_ID,
    Api,
    local,
    open_api,
    plan,
)
from tests.postgres import DATABASE_URL, POSTGRES

pytestmark = POSTGRES


def _admin(statement: sql.Composed) -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(statement)


@pytest.fixture
def database() -> Iterator[str]:
    name = f"platform_{uuid.uuid4().hex[:10]}"
    try:
        _admin(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    except psycopg.errors.InsufficientPrivilege:
        pytest.skip("the test database user cannot create a database")
    url = make_conninfo(DATABASE_URL, dbname=name)
    try:
        assert migrate(url) == ["0001_load_memory.sql", "0002_vector_extension.sql"]
        asyncio.run(setup_store(url, 1536))
        yield url
    finally:
        _admin(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@asynccontextmanager
async def platform(url: str, blobs: Path, model: FakeModel | None = None) -> AsyncIterator[Api]:
    """One API process on the database, started and stopped as uvicorn does."""
    async with (
        open_api(
            model, persistence="postgres", database_url=SecretStr(url), blobs_dir=blobs
        ) as api,
        api.app.router.lifespan_context(api.app),
    ):
        yield api


async def _rows(url: str) -> dict[str, Any]:
    async with await psycopg.AsyncConnection.connect(url) as conn:
        counts: dict[str, Any] = {}
        for table in ("loads", "events", "obligations", "timers", "runs", "outbox"):
            cursor = await conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            )
            row = await cursor.fetchone()
            counts[table] = None if row is None else row[0]
        return counts


async def test_loads_runs_and_the_ledger_survive_a_restart(database: str, tmp_path: Path) -> None:
    async with platform(database, tmp_path) as api:
        await api.create_load()
        api.model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))
        await api.ping("high", local(7, 0))
        before = await api.get(f"/api/loads/{LOAD_ID}")
        runs = await api.get(f"/api/loads/{LOAD_ID}/runs")
    assert [r["status"] for r in runs] == ["acted"]
    assert [o["status"] for o in before["obligations"]] == ["open"]

    async with platform(database, tmp_path) as api:  # a new process on the same database
        after = await api.get(f"/api/loads/{LOAD_ID}")
        state = await api.get("/api/sim/state")
        assert await api.get(f"/api/loads/{LOAD_ID}/runs") == runs

    for part in ("load", "obligations", "timers", "outbox", "runs", "events"):
        assert after[part] == before[part], part
    # The clock goes on from the latest time the loads hold, not back to 06:55, and the
    # follow-up timer is still pending.
    assert state["now"] == local(7, 1)
    assert state["next_timer_due_at"] == local(7, 31)
    assert [s["load_id"] for s in state["loads"]] == [LOAD_ID]


async def test_a_reset_empties_the_database(database: str, tmp_path: Path) -> None:
    async with platform(database, tmp_path) as api:
        await api.create_load()
        api.model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))
        await api.ping("high", local(7, 0))
        assert (await _rows(database))["runs"] == 1

        response = await api.client.post("/api/sim/reset", json={})

        assert response.status_code == 200
        assert await _rows(database) == dict.fromkeys(
            ("loads", "events", "obligations", "timers", "runs", "outbox"), 0
        )
        assert await api.loads() == []


def _memory(api: Api) -> StoreFacilityMemory:
    """The platform's facility memory, its extraction queue included."""
    return cast(
        StoreFacilityMemory, api.app.state.service.session.simulation.runtime.facility_memory
    )


def _note(text: str, at: str) -> FacilityFact:
    told = datetime.fromisoformat(at)
    return FacilityFact(
        fact_id="f1",
        client_id="northline",
        facility_key="riverbend-plant~stockton-ca",
        facility_name="Riverbend plant",
        text=text,
        kind=FactKind.CHECK_IN,
        source_load_id="470113",
        source_event_id="m1",
        source_at=told,
        created_at=told,
        updated_at=told,
    )


async def test_facility_notes_survive_a_restart_and_the_next_run_reads_them(
    database: str, tmp_path: Path
) -> None:
    async with platform(database, tmp_path) as api:
        await api.create_load()
        memory = _memory(api)
        await memory.apply("northline", upserts=[_note("Check in at gate 4", local(6, 50))])

    async with platform(database, tmp_path) as api:  # a new process on the same database
        stops = await api.get(f"/api/loads/{LOAD_ID}/facility-memory")
        api.model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))
        await api.ping("high", local(7, 0))
        runs = await api.get(f"/api/loads/{LOAD_ID}/runs")

    assert [f["text"] for f in stops[0]["facts"]] == ["Check in at gate 4"]
    assert [f["text"] for f in runs[0]["facility_facts"]] == ["Check in at gate 4"]
    async with await psycopg.AsyncConnection.connect(database) as conn:
        cursor = await conn.execute("SELECT count(*) FROM store_vectors")
        assert await cursor.fetchone() == (1,)  # the note's text, embedded


async def test_a_reset_forgets_the_facility_notes(database: str, tmp_path: Path) -> None:
    async with platform(database, tmp_path) as api:
        await api.create_load()
        memory = _memory(api)
        await memory.apply("northline", upserts=[_note("Check in at gate 4", local(6, 50))])

        assert (await api.client.post("/api/sim/reset", json={})).status_code == 200
        await api.create_load()
        stops = await api.get(f"/api/loads/{LOAD_ID}/facility-memory")

    assert [s["facts"] for s in stops] == [[], []]


async def test_a_delivered_load_whose_extraction_job_was_lost_is_queued_at_the_next_start(
    database: str, tmp_path: Path
) -> None:
    """The run that delivers a load commits, then queues its extraction: a crash in
    between loses the job. The next start queues it again, and the next tick runs it."""
    async with platform(database, tmp_path) as api:
        await api.create_load()
        session = api.app.state.service.session
        async with session.uow() as uow:  # delivered, as if the process died before the queue
            load = await uow.loads.get(LOAD_ID)
            assert load is not None
            await uow.loads.save(load.model_copy(update={"stage": Stage.DELIVERED}))
            await uow.commit()
        assert await _memory(api).get("northline", LOAD_ID) is None

    async with platform(database, tmp_path) as api:
        memory = _memory(api)
        queued = await memory.get("northline", LOAD_ID)
        await api.client.post("/api/sim/tick")
        done = await memory.get("northline", LOAD_ID)

    assert queued is not None and queued.status is ExtractionStatus.PENDING
    # No inbound message to read: the job is done without a model call.
    assert done is not None and done.status is ExtractionStatus.DONE
    assert api.model.calls == []
