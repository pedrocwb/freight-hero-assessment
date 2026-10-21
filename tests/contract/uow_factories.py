"""The `UnitOfWork` implementations the contract suites run on.

Every contract suite of this folder runs each test once per entry of `UOW_FACTORIES`,
through the `uow_factory` fixture of `conftest.py`: the memory store always, and Postgres
when `TEST_DATABASE_URL` (or `DATABASE_URL`) is set, marked `integration`. So the two
stores pass the same suites.
"""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from freight_agent.adapters.persistence.memory.store import MemoryStore
from freight_agent.adapters.persistence.postgres.migrate import scratch_schema
from freight_agent.adapters.persistence.postgres.store import open_store
from freight_agent.interfaces.repositories import UnitOfWorkFactory
from tests.postgres import DATABASE_ENV, database_url_for_tests

# How many failed attempts make a queued event a dead letter, in every contract store:
# the queue tests count them.
MAX_QUEUE_ATTEMPTS = 3


class OpenStore(Protocol):
    """Opens a fresh, empty store for one test and closes it when the test ends.

    The suite sets how many failed attempts make a queued event a dead letter,
    because its queue tests count them.
    """

    def __call__(
        self, *, max_queue_attempts: int
    ) -> AbstractAsyncContextManager[UnitOfWorkFactory]: ...


@dataclass(frozen=True)
class UowFactory:
    open_store: OpenStore
    needs_database: bool = False  # skipped while no test database is set

    def skip_reason(self) -> str | None:
        if self.needs_database and database_url_for_tests() is None:
            return f"set {' or '.join(DATABASE_ENV)} to run this implementation"
        return None


@asynccontextmanager
async def open_memory(*, max_queue_attempts: int) -> AsyncIterator[UnitOfWorkFactory]:
    yield MemoryStore(max_queue_attempts=max_queue_attempts).unit_of_work


@asynccontextmanager
async def open_postgres(*, max_queue_attempts: int) -> AsyncIterator[UnitOfWorkFactory]:
    """A schema of its own on the test database, migrated from scratch and dropped
    afterwards. Nothing outside that schema is read or written, so the URL may point at
    a database that holds data."""
    conninfo = database_url_for_tests()
    assert conninfo is not None, "skipped without a test database"
    async with (
        scratch_schema(conninfo, prefix="contract") as schema,
        open_store(conninfo, schema=schema, max_queue_attempts=max_queue_attempts) as store,
    ):
        yield store.unit_of_work


UOW_FACTORIES: dict[str, UowFactory] = {
    "memory": UowFactory(open_memory),
    "postgres": UowFactory(open_postgres, needs_database=True),
}
