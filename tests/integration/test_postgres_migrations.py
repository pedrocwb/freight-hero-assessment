"""The migration runner against a real database: reruns, races, failures and edits.

Each test migrates a schema of its own and drops it afterwards.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from freight_agent.adapters.persistence.postgres.migrate import (
    MIGRATIONS_DIR,
    MigrationError,
    apply_migrations,
    discover,
    drop_schema,
    migrate,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="set DATABASE_URL to run the Postgres tests"),
]

PORT_TABLES = {
    "loads",
    "risk_trackers",
    "events",
    "obligations",
    "timers",
    "outbox",
    "runs",
    "event_queue",
    "schema_migrations",
}
# The tables the wave 3 migrations must create, by the number each card reserved
# (`docs/decisions.md`, "Wave 3 file ownership"). Checked once the file is in the repo.
WAVE_3_TABLES = {
    3: {"brokers", "broker_versions"},  # card W3-BRK
    4: {"sop_documents", "sop_drafts", "sop_versions", "sop_sections"},  # card W3-KNW
}


def _new_schema() -> str:
    return f"mtest_{uuid.uuid4().hex[:12]}"


@pytest.fixture
async def schema() -> AsyncIterator[str]:
    name = _new_schema()
    yield name
    await drop_schema(DATABASE_URL, name)


@pytest.fixture
def scripts(tmp_path: Path) -> Path:
    """A migrations directory of the test's own, independent of the repo's files."""
    (tmp_path / "0001_first.sql").write_text("CREATE TABLE first (id integer PRIMARY KEY);\n")
    return tmp_path


async def _query(schema: str, query: str) -> list[tuple[object, ...]]:
    """`query` with the schema name as its one parameter."""
    return await _fetch(query, (schema,))


async def _fetch(query: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        cursor = await conn.execute(query.encode(), params)
        return await cursor.fetchall()


async def _tables(schema: str) -> set[str]:
    rows = await _query(
        schema, "SELECT table_name FROM information_schema.tables WHERE table_schema = %s"
    )
    return {str(row[0]) for row in rows}


async def _versions(schema: str) -> list[object]:
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        cursor = await conn.execute(b"SELECT version FROM schema_migrations ORDER BY version")
        return [row[0] for row in await cursor.fetchall()]


async def test_the_repo_migrations_create_every_port_table_and_a_rerun_changes_nothing(
    schema: str,
) -> None:
    first = await apply_migrations(DATABASE_URL, schema=schema)
    assert [m.version for m in first] == [m.version for m in discover(MIGRATIONS_DIR)]
    assert await _tables(schema) >= PORT_TABLES

    assert await apply_migrations(DATABASE_URL, schema=schema) == []
    assert await _versions(schema) == [m.version for m in first]


async def test_the_wave_3_migrations_create_the_tables_of_their_cards(schema: str) -> None:
    applied = {m.version for m in await apply_migrations(DATABASE_URL, schema=schema)}
    present = {version: tables for version, tables in WAVE_3_TABLES.items() if version in applied}
    if not present:
        pytest.skip("no wave 3 migration in migrations/ yet (cards W3-BRK and W3-KNW)")

    tables = await _tables(schema)

    for version, expected in present.items():
        assert expected <= tables, f"migration {version:04d} misses {sorted(expected - tables)}"


async def test_the_vector_extension_and_its_columns_live_in_public(schema: str) -> None:
    """A scratch schema sets `search_path` to itself only, so the knowledge migration
    puts `vector` in `public` and names its type there (F9): a migration that worked only
    on the default schema would break every contract and scenario test."""
    applied = {m.version for m in await apply_migrations(DATABASE_URL, schema=schema)}
    if 4 not in applied:
        pytest.skip("migration 0004 (card W3-KNW) is not in migrations/ yet")

    extension = await _fetch(
        "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
        "WHERE e.extname = 'vector'"
    )
    assert extension == [("public",)]
    columns = await _query(
        schema,
        "SELECT format_type(a.atttypid, a.atttypmod), tn.nspname FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace cn ON cn.oid = c.relnamespace "
        "JOIN pg_type t ON t.oid = a.atttypid JOIN pg_namespace tn ON tn.oid = t.typnamespace "
        "WHERE cn.nspname = %s AND c.relname = 'sop_sections' AND a.attname = 'embedding'",
    )
    assert columns == [("vector(1536)", "public")]


async def test_runs_that_race_apply_each_migration_once(schema: str, scripts: Path) -> None:
    (scripts / "0002_second.sql").write_text("CREATE TABLE second (id integer);\n")

    results = await asyncio.gather(
        *(apply_migrations(DATABASE_URL, schema=schema, directory=scripts) for _ in range(3))
    )

    assert sorted(m.version for applied in results for m in applied) == [1, 2]
    assert await _versions(schema) == [1, 2]


async def test_a_new_migration_is_the_only_one_applied_on_the_next_run(
    schema: str, scripts: Path
) -> None:
    await apply_migrations(DATABASE_URL, schema=schema, directory=scripts)
    (scripts / "0002_second.sql").write_text("CREATE TABLE second (id integer);\n")

    applied = await apply_migrations(DATABASE_URL, schema=schema, directory=scripts)

    assert [m.filename for m in applied] == ["0002_second.sql"]
    assert {"first", "second"} <= await _tables(schema)


async def test_a_failing_migration_leaves_no_trace_and_stops_the_run(
    schema: str, scripts: Path
) -> None:
    broken = scripts / "0002_broken.sql"
    broken.write_text("CREATE TABLE half (id integer);\nSELECT no_such_column FROM half;\n")
    (scripts / "0003_after.sql").write_text("CREATE TABLE after (id integer);\n")

    with pytest.raises(MigrationError, match=r"0002_broken\.sql"):
        await apply_migrations(DATABASE_URL, schema=schema, directory=scripts)

    assert await _versions(schema) == [1]
    tables = await _tables(schema)
    assert "half" not in tables and "after" not in tables

    broken.write_text("CREATE TABLE half (id integer);\n")
    applied = await apply_migrations(DATABASE_URL, schema=schema, directory=scripts)
    assert [m.version for m in applied] == [2, 3]


async def test_an_applied_migration_that_was_edited_is_refused(schema: str, scripts: Path) -> None:
    await apply_migrations(DATABASE_URL, schema=schema, directory=scripts)
    (scripts / "0001_first.sql").write_text("CREATE TABLE first (id bigint PRIMARY KEY);\n")

    with pytest.raises(MigrationError, match=r"0001_first\.sql changed"):
        await apply_migrations(DATABASE_URL, schema=schema, directory=scripts)


@pytest.fixture
def sync_schema() -> Iterator[str]:
    name = _new_schema()
    yield name
    asyncio.run(drop_schema(DATABASE_URL, name))


def test_migrate_takes_the_direct_url_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, sync_schema: str, scripts: Path
) -> None:
    """The CLI entry point: DDL goes to the direct endpoint, never through the pooler."""
    monkeypatch.setenv("DATABASE_URL_DIRECT", DATABASE_URL)
    monkeypatch.setenv("DATABASE_URL", "postgresql://pooler.invalid:1/none")

    assert migrate(schema=sync_schema, directory=scripts) == ["0001_first.sql"]
    assert migrate(schema=sync_schema, directory=scripts) == []
