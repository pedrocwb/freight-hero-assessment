"""The Postgres store: a connection pool and the unit of work factory over it.

    async with open_store(os.environ["DATABASE_URL"]) as store:
        deps = RuntimeDeps(uow_factory=store.unit_of_work, ...)

Which URL to pass is in the package docstring (`postgres/__init__.py`).
"""

import contextlib
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any, Self

from psycopg import AsyncConnection
from psycopg import errors as pg_errors
from psycopg.abc import QueryNoTemplate
from psycopg.pq import TransactionStatus
from psycopg_pool import AsyncConnectionPool

from freight_agent.adapters.persistence.postgres.connection import (
    CONNECTION_KWARGS,
    Session,
    begin_command,
)
from freight_agent.adapters.persistence.postgres.repositories import (
    PostgresEventLog,
    PostgresEventQueue,
    PostgresLoadRepository,
    PostgresObligationRepository,
    PostgresOutboxRepository,
    PostgresRunRepository,
    PostgresTimerRepository,
)
from freight_agent.ports.repositories import (
    EventLog,
    EventQueue,
    LoadRepository,
    ObligationRepository,
    OutboxRepository,
    RunRepository,
    TimerRepository,
)


class PostgresStore:
    """`schema` selects a schema other than the connection's default (tests, eval
    trials); every transaction sets it with `SET LOCAL`, so it works through a pooler."""

    def __init__(
        self,
        pool: AsyncConnectionPool[Any],
        *,
        schema: str | None = None,
        max_queue_attempts: int = 3,
    ) -> None:
        self._pool = pool
        self._begin = begin_command(schema)
        self.max_queue_attempts = max_queue_attempts

    def unit_of_work(self) -> "PostgresUnitOfWork":
        return PostgresUnitOfWork(self._pool, self._begin, self.max_queue_attempts)


@contextlib.asynccontextmanager
async def open_store(
    conninfo: str,
    *,
    schema: str | None = None,
    max_queue_attempts: int = 3,
    min_size: int = 1,
    max_size: int = 10,
) -> AsyncIterator[PostgresStore]:
    """Open a pool on `conninfo` for the duration of the block.

    Each connection is checked as it leaves the pool: Neon closes idle connections when
    its compute scales to zero, and a dead one would fail the first statement of a unit
    of work.
    """
    begin_command(schema)  # an invalid schema name fails before any connection opens
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        conninfo,
        min_size=min_size,
        max_size=max_size,
        kwargs=CONNECTION_KWARGS,
        check=AsyncConnectionPool.check_connection,
        open=False,
    )
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
        self._session = Session()
        self._committed = False
        # Typed as the ports: the protocol's attributes are invariant, so a concrete
        # annotation would keep this unit of work from satisfying `UnitOfWork`.
        self.loads: LoadRepository = PostgresLoadRepository(self._session)
        self.events: EventLog = PostgresEventLog(self._session)
        self.obligations: ObligationRepository = PostgresObligationRepository(self._session)
        self.timers: TimerRepository = PostgresTimerRepository(self._session)
        self.outbox: OutboxRepository = PostgresOutboxRepository(self._session)
        self.runs: RunRepository = PostgresRunRepository(self._session)
        self.queue: EventQueue = PostgresEventQueue(self._session, max_queue_attempts)

    async def __aenter__(self) -> Self:
        if self._session.bound:
            raise RuntimeError("this unit of work is already open")
        conn = await self._pool.getconn()
        try:
            await conn.execute(self._begin)
        except BaseException:
            await self._pool.putconn(conn)
            raise
        self._session.bind(conn)
        self._committed = False
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        conn = self._session.unbind()
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


async def _commit(conn: AsyncConnection[Any]) -> None:
    # COMMIT on a transaction that already failed is answered with a silent ROLLBACK.
    # That happens when the block caught a database error and carried on.
    if conn.info.transaction_status is TransactionStatus.INERROR:
        await conn.rollback()
        raise pg_errors.InFailedSqlTransaction(
            "a statement of this unit of work failed earlier, so nothing was committed"
        )
    await conn.commit()
