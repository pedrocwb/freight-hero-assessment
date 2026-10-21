"""Which database the Postgres tests run on.

`TEST_DATABASE_URL`, else `DATABASE_URL` (as `make integration` sets it). Without either
every Postgres test skips, so `make test` and `make ci` need no database. Each test works
in a schema of its own (`scratch_schema`), dropped afterwards, so the URL may
point at a database that holds data: nothing outside that schema is touched.
"""

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from psycopg import AsyncConnection, sql

from freight_agent.adapters.persistence.postgres.connection import (
    CONNECTION_KWARGS,
    check_schema,
)
from freight_agent.adapters.persistence.postgres.migrate import apply_migrations

DATABASE_ENV = ("TEST_DATABASE_URL", "DATABASE_URL")


def database_url_for_tests() -> str | None:
    """The database the Postgres tests run on, or None to skip them."""
    return next((value for name in DATABASE_ENV if (value := os.environ.get(name))), None)


SKIP_REASON = f"set {' or '.join(DATABASE_ENV)} to run the Postgres tests"
DATABASE_URL = database_url_for_tests() or ""

# The marks of a module whose every test needs the database.
POSTGRES = [pytest.mark.integration, pytest.mark.skipif(not DATABASE_URL, reason=SKIP_REASON)]


async def drop_schema(conninfo: str, schema: str) -> None:
    async with await AsyncConnection.connect(conninfo, **CONNECTION_KWARGS) as conn:
        await conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(check_schema(schema)))
        )


@asynccontextmanager
async def scratch_schema(conninfo: str, *, prefix: str = "scratch") -> AsyncIterator[str]:
    """A new schema with every migration applied, dropped on exit.

    It gives a test an empty store of its own on a shared database, without touching
    anything outside it (pass the name to `open_store`).
    """
    schema = check_schema(f"{prefix}_{uuid.uuid4().hex[:12]}")
    try:
        await apply_migrations(conninfo, schema=schema)
        yield schema
    finally:
        await drop_schema(conninfo, schema)
