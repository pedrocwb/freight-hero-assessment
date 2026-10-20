"""The `UnitOfWork` implementation the contract suites run on: the memory store.

Every contract suite of this folder (`test_unit_of_work.py`, and the wave 3 suites of the
broker, SOP store and document repositories) opens a fresh store per test through the
`uow_factory` fixture of `conftest.py`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from freight_agent.adapters.persistence.memory.store import MemoryStore
from freight_agent.ports.repositories import UnitOfWorkFactory

# How many failed attempts make a queued event a dead letter, in every contract store:
# the queue tests count them.
MAX_QUEUE_ATTEMPTS = 3


@asynccontextmanager
async def open_memory(*, max_queue_attempts: int) -> AsyncIterator[UnitOfWorkFactory]:
    yield MemoryStore(max_queue_attempts=max_queue_attempts).unit_of_work
