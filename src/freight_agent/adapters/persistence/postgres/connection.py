"""Connection settings and the transaction session shared by the repositories.

Everything here works through a transaction-mode pooler (Neon's PgBouncer): connections
carry no session state, and settings (the time zone, a schema other than the default)
are made with `SET LOCAL` inside each transaction, never per connection.
"""

import re
from typing import Any

from psycopg import AsyncConnection, sql
from psycopg.abc import Params, QueryNoTemplate
from psycopg.rows import DictRow, dict_row

# Autocommit, so that a unit of work opens its transaction with an explicit BEGIN and a
# nested `transaction()` is a savepoint; no server-side prepared statements, which a
# transaction-mode pooler cannot keep across transactions.
CONNECTION_KWARGS: dict[str, Any] = {"autocommit": True, "prepare_threshold": None}

_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def check_schema(schema: str) -> str:
    """A plain lowercase identifier, so it never needs quoting in a search_path."""
    if not _SCHEMA_NAME.fullmatch(schema):
        raise ValueError(f"schema {schema!r} must match {_SCHEMA_NAME.pattern}")
    return schema


def begin_command(schema: str | None) -> sql.SQL | sql.Composed:
    """BEGIN, in UTC, and in `schema` when one is given.

    Timestamps come back in the session's zone, which is the server's setting unless
    set: UTC keeps what the adapter returns the same on every server (the instants are
    equal either way; text derived from them, such as an ISO string, is not).
    """
    begin = sql.SQL("BEGIN; SET LOCAL TimeZone TO 'UTC'")
    if schema is None:
        return begin
    return sql.SQL("{}; SET LOCAL search_path TO {}").format(
        begin, sql.Identifier(check_schema(schema))
    )


class Session:
    """The connection of the open unit of work, shared by its repositories."""

    def __init__(self) -> None:
        self._conn: AsyncConnection[Any] | None = None

    @property
    def conn(self) -> AsyncConnection[Any]:
        if self._conn is None:
            raise RuntimeError("use the repositories inside `async with` on the unit of work")
        return self._conn

    @property
    def bound(self) -> bool:
        return self._conn is not None

    def bind(self, conn: AsyncConnection[Any]) -> None:
        self._conn = conn

    def unbind(self) -> AsyncConnection[Any]:
        conn, self._conn = self.conn, None
        return conn

    async def one(self, query: QueryNoTemplate, params: Params | None = None) -> DictRow | None:
        async with self.conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, params)
            return await cursor.fetchone()

    async def all(self, query: QueryNoTemplate, params: Params | None = None) -> list[DictRow]:
        async with self.conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, params)
            return await cursor.fetchall()

    async def run(self, query: QueryNoTemplate, params: Params | None = None) -> None:
        await self.conn.execute(query, params)
