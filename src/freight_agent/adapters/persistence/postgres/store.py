"""The Postgres store: a connection pool and the unit of work factory over it.

    async with open_store(database_url) as store:
        deps = RuntimeDeps(uow_factory=store.unit_of_work, ...)

A synchronous composition root (`bootstrap.build_uow_factory`) uses
`PostgresUnitOfWorkFactory` instead: its pool opens on the first unit of work, inside the
caller's event loop, and `close()` belongs in the caller's shutdown.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any, Final, Self

from psycopg import AsyncConnection, sql
from psycopg import errors as pg_errors
from psycopg.abc import QueryNoTemplate
from psycopg.pq import TransactionStatus
from psycopg_pool import AsyncConnectionPool

from freight_agent.adapters.persistence.postgres.connection import (
    CONNECTION_KWARGS,
    Session,
    begin_command,
)
from freight_agent.adapters.persistence.postgres.documents import PostgresDocumentRepository
from freight_agent.adapters.persistence.postgres.repositories import (
    PostgresEventLog,
    PostgresEventQueue,
    PostgresLoadRepository,
    PostgresObligationRepository,
    PostgresOutboxRepository,
    PostgresRunRepository,
    PostgresTimerRepository,
)
from freight_agent.interfaces.documents import DocumentRepository
from freight_agent.interfaces.repositories import (
    EventLog,
    EventQueue,
    LoadRepository,
    ObligationRepository,
    OutboxRepository,
    RunRepository,
    TimerRepository,
)

# Every table of a load's memory, emptied by `clear()`. `schema_migrations` is not one.
DOMAIN_TABLES: Final = (
    "loads",
    "risk_trackers",
    "events",
    "obligations",
    "timers",
    "runs",
    "outbox",
    "event_queue",
    "documents",
)


def _pool(conninfo: str, *, min_size: int, max_size: int) -> AsyncConnectionPool[Any]:
    """A closed pool. Each connection is checked as it leaves the pool, so a connection
    the server dropped while idle never fails the first statement of a unit of work."""
    return AsyncConnectionPool(
        conninfo,
        min_size=min_size,
        max_size=max_size,
        kwargs=CONNECTION_KWARGS,
        check=AsyncConnectionPool.check_connection,
        open=False,
    )


class PostgresStore:
    """`schema` selects a schema other than the connection's default (the tests); every
    transaction sets it with `SET LOCAL`, so it works through a pooler too."""

    def __init__(
        self,
        pool: AsyncConnectionPool[Any],
        *,
        schema: str | None = None,
        max_queue_attempts: int = 3,
    ) -> None:
        self.pool = pool
        self.begin = begin_command(schema)
        self.max_queue_attempts = max_queue_attempts

    def unit_of_work(self) -> "PostgresUnitOfWork":
        return PostgresUnitOfWork(self.pool, self.begin, self.max_queue_attempts)

    async def clear(self) -> None:
        """Empty every domain table in one transaction: a fresh simulation."""
        tables = sql.SQL(", ").join(sql.Identifier(t) for t in DOMAIN_TABLES)
        async with self.unit_of_work() as uow:
            await uow.session.run(sql.SQL("TRUNCATE {} RESTART IDENTITY").format(tables))
            await uow.commit()


@contextlib.asynccontextmanager
async def open_store(
    conninfo: str,
    *,
    schema: str | None = None,
    max_queue_attempts: int = 3,
    min_size: int = 1,
    max_size: int = 10,
) -> AsyncIterator[PostgresStore]:
    """Open a pool on `conninfo` for the duration of the block."""
    begin_command(schema)  # an invalid schema name fails before any connection opens
    pool = _pool(conninfo, min_size=min_size, max_size=max_size)
    await pool.open(wait=True)
    try:
        yield PostgresStore(pool, schema=schema, max_queue_attempts=max_queue_attempts)
    finally:
        await pool.close()


class PostgresUnitOfWork:
    """One transaction on one pooled connection, from `async with` to its end.

    `commit()` only marks the unit of work: the COMMIT is sent when the block ends
    normally, so an exception raised after `commit()` still rolls everything back.
    """

    def __init__(
        self, pool: AsyncConnectionPool[Any], begin: QueryNoTemplate, max_queue_attempts: int
    ) -> None:
        self._pool = pool
        self._begin = begin
        self.session = Session()
        self._committed = False
        # Typed as the interfaces: the protocol's attributes are invariant, so a concrete
        # annotation would keep this unit of work from satisfying `UnitOfWork`.
        self.loads: LoadRepository = PostgresLoadRepository(self.session)
        self.events: EventLog = PostgresEventLog(self.session)
        self.obligations: ObligationRepository = PostgresObligationRepository(self.session)
        self.timers: TimerRepository = PostgresTimerRepository(self.session)
        self.outbox: OutboxRepository = PostgresOutboxRepository(self.session)
        self.runs: RunRepository = PostgresRunRepository(self.session)
        self.queue: EventQueue = PostgresEventQueue(self.session, max_queue_attempts)
        self.documents: DocumentRepository = PostgresDocumentRepository(self.session)

    async def __aenter__(self) -> Self:
        if self.session.bound:
            raise RuntimeError("this unit of work is already open")
        conn = await self._pool.getconn()
        try:
            await conn.execute(self._begin)
        except BaseException:
            await self._pool.putconn(conn)
            raise
        self.session.bind(conn)
        self._committed = False
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        conn = self.session.unbind()
        try:
            if exc_type is None and self._committed:
                await _commit(conn)
            elif exc_type is None:
                await conn.rollback()
            else:
                # The original error matters more than a rollback on a broken connection,
                # which the server undoes anyway and the pool then discards.
                with contextlib.suppress(pg_errors.Error):
                    await conn.rollback()
        finally:
            await self._pool.putconn(conn)

    async def commit(self) -> None:
        self._committed = True


class _LazyUnitOfWork(PostgresUnitOfWork):
    def __init__(self, factory: "PostgresUnitOfWorkFactory") -> None:
        super().__init__(factory.pool, factory.begin, factory.max_queue_attempts)
        self._factory = factory

    async def __aenter__(self) -> Self:
        await self._factory.ensure_open()
        return await super().__aenter__()


class PostgresUnitOfWorkFactory(PostgresStore):
    """A `UnitOfWorkFactory` whose pool opens on first use.

    The composition root is synchronous and cannot await `pool.open()`: the API builds
    its runtime first and serves afterwards, in its event loop, where the first unit of
    work opens the pool. `close()` belongs in the API's shutdown.
    """

    def __init__(
        self,
        conninfo: str,
        *,
        schema: str | None = None,
        max_queue_attempts: int = 3,
        min_size: int = 1,
        max_size: int = 10,
    ) -> None:
        super().__init__(
            _pool(conninfo, min_size=min_size, max_size=max_size),
            schema=schema,
            max_queue_attempts=max_queue_attempts,
        )
        self._opened = False
        self._lock = asyncio.Lock()

    def __call__(self) -> PostgresUnitOfWork:
        return _LazyUnitOfWork(self)

    def unit_of_work(self) -> PostgresUnitOfWork:
        return _LazyUnitOfWork(self)

    async def ensure_open(self) -> None:
        if self._opened:
            return
        async with self._lock:
            if not self._opened:
                await self.pool.open(wait=True)
                self._opened = True

    async def close(self) -> None:
        if self._opened:
            await self.pool.close()
            self._opened = False


async def _commit(conn: AsyncConnection[Any]) -> None:
    # COMMIT on a transaction that already failed is answered with a silent ROLLBACK.
    # That happens when the block caught a database error and carried on.
    if conn.info.transaction_status is TransactionStatus.INERROR:
        await conn.rollback()
        raise pg_errors.InFailedSqlTransaction(
            "a statement of this unit of work failed earlier, so nothing was committed"
        )
    await conn.commit()
