"""The platform on `PERSISTENCE=postgres`: loads, runs and the ledger survive a restart of
the API, and a reset empties the database.

Each test gets a database of its own, migrated by `agent db migrate`'s runner and
dropped afterwards (the platform uses the database's default schema, as in compose).
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from freight_agent.adapters.persistence.postgres.migrate import migrate
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
