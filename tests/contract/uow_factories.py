"""The `UnitOfWork` implementations the contract suites run on.

Every contract suite of this folder (`test_unit_of_work.py`, and the wave 3 suites of the
broker, SOP store and document repositories) runs each test once per entry of
`UOW_FACTORIES`, through the `uow_factory` fixture of `conftest.py`, so putting an
implementation through every contract is one line in that registry.
"""

import os
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from freight_agent.adapters.persistence.memory.store import MemoryStore
from freight_agent.adapters.persistence.postgres.migrate import scratch_schema
from freight_agent.adapters.persistence.postgres.store import open_store
from freight_agent.ports.repositories import UnitOfWorkFactory

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
    requires_env: tuple[str, ...] = ()  # the suite skips this entry while one is unset or empty

    def skip_reason(self) -> str | None:
        missing = [name for name in self.requires_env if not os.environ.get(name)]
        return f"set {', '.join(missing)} to run this implementation" if missing else None


@asynccontextmanager
async def open_memory(*, max_queue_attempts: int) -> AsyncIterator[UnitOfWorkFactory]:
    yield MemoryStore(max_queue_attempts=max_queue_attempts).unit_of_work


@asynccontextmanager
async def open_postgres(*, max_queue_attempts: int) -> AsyncIterator[UnitOfWorkFactory]:
    """A schema of its own on DATABASE_URL, migrated from scratch and dropped afterwards.

    Nothing outside that schema is read or written, so DATABASE_URL may point at a
    database that holds data.
    """
    conninfo = os.environ["DATABASE_URL"]
    async with (
        scratch_schema(conninfo, prefix="contract") as schema,
        open_store(conninfo, schema=schema, max_queue_attempts=max_queue_attempts) as store,
    ):
        yield store.unit_of_work


UOW_FACTORIES: dict[str, UowFactory] = {
    "memory": UowFactory(open_memory),
    "postgres": UowFactory(open_postgres, requires_env=("DATABASE_URL",)),
}
