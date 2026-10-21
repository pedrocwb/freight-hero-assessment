"""Plain numbered SQL migrations: `migrations/NNNN_name.sql` at the repo root.

Each file runs in a transaction of its own, together with its row in `schema_migrations`,
so a failing file leaves no trace and stops the run. A rerun applies only the files not
recorded yet. A recorded file whose content changed is refused: the database no longer
matches it, and a new numbered file is the way to change a schema. Concurrent runs (two
containers starting) wait for each other on a transaction-scoped advisory lock. Since
the runner owns the transaction, a file holds no BEGIN or COMMIT, and nothing that
cannot run inside a transaction (such as `CREATE INDEX CONCURRENTLY`).

The entry points are `apply_migrations` (async) and `migrate` (sync, for the
`agent db migrate` command).
"""

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from psycopg import AsyncConnection, sql
from psycopg import errors as pg_errors
from psycopg.abc import QueryNoTemplate

from freight_agent.adapters.persistence.postgres.connection import (
    CONNECTION_KWARGS,
    begin_command,
)

# src/freight_agent/adapters/persistence/postgres/migrate.py -> the repo root.
MIGRATIONS_DIR: Final = Path(__file__).resolve().parents[5] / "migrations"

_FILENAME: Final = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_LOCK_CLASS: Final = 0x46524D47  # the first key of pg_advisory_xact_lock(int, int)

_SETUP: Final = sql.SQL(
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    " version integer PRIMARY KEY,"
    " filename text NOT NULL,"
    " checksum text NOT NULL,"
    " applied_at timestamptz NOT NULL DEFAULT now())"
)


class MigrationError(Exception):
    """The migrations cannot be applied as they are."""


@dataclass(frozen=True)
class Migration:
    version: int
    filename: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """The `.sql` files of `directory`, by number. Other files are ignored."""
    if not directory.is_dir():
        raise MigrationError(f"no migrations directory at {directory}")
    found: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"{path.name}: name a migration NNNN_lowercase_name.sql")
        version = int(match[1])
        if version in found:
            raise MigrationError(
                f"{path.name} and {found[version].filename} share the number {match[1]}"
            )
        found[version] = Migration(version, path.name, path.read_text())
    return [found[version] for version in sorted(found)]


async def apply_migrations(
    conninfo: str, *, schema: str | None = None, directory: Path = MIGRATIONS_DIR
) -> list[Migration]:
    """Apply the migrations not recorded yet, in order. Returns those this call applied.

    With `schema`, the schema is created if needed and everything goes into it.
    """
    begin = begin_command(schema)
    migrations = discover(directory)
    lock = sql.SQL("SELECT pg_advisory_xact_lock({}, hashtext({}))").format(
        _LOCK_CLASS, schema or ""
    )
    applied: list[Migration] = []
    async with await AsyncConnection.connect(conninfo, **CONNECTION_KWARGS) as conn:
        async with _locked_transaction(conn, begin, lock):
            if schema is not None:
                create = sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema))
                await conn.execute(create)
            await conn.execute(_SETUP)
        for migration in migrations:
            async with _locked_transaction(conn, begin, lock):
                if await _apply(conn, migration):
                    applied.append(migration)
    return applied


@asynccontextmanager
async def _locked_transaction(
    conn: AsyncConnection[Any], begin: QueryNoTemplate, lock: QueryNoTemplate
) -> AsyncIterator[None]:
    """A transaction that first waits for any other migration run on the same schema.

    The lock comes before anything else, `CREATE ... IF NOT EXISTS` included, which two
    concurrent runs could otherwise both attempt.
    """
    await conn.execute(begin)
    try:
        await conn.execute(lock)
        yield
    except BaseException:
        await conn.rollback()
        raise
    await conn.commit()


async def _apply(conn: AsyncConnection[Any], migration: Migration) -> bool:
    """Apply one migration unless it is recorded. Returns whether it was applied."""
    cursor = await conn.execute(
        "SELECT checksum FROM schema_migrations WHERE version = %s", (migration.version,)
    )
    recorded = await cursor.fetchone()
    if recorded is not None:
        if recorded[0] != migration.checksum:
            raise MigrationError(
                f"{migration.filename} changed after it was applied; add a new migration"
                " instead of editing an applied one"
            )
        return False
    try:
        await conn.execute(migration.sql.encode())  # bytes: a file, not a literal query
    except pg_errors.Error as exc:
        raise MigrationError(f"{migration.filename} failed: {exc}") from exc
    await conn.execute(
        "INSERT INTO schema_migrations (version, filename, checksum) VALUES (%s, %s, %s)",
        (migration.version, migration.filename, migration.checksum),
    )
    return True


def migrate(
    conninfo: str, *, schema: str | None = None, directory: Path = MIGRATIONS_DIR
) -> list[str]:
    """Apply pending migrations from synchronous code, such as a CLI command.

    Returns the file names applied; an empty list means the database was up to date.
    """
    applied = asyncio.run(apply_migrations(conninfo, schema=schema, directory=directory))
    return [migration.filename for migration in applied]
