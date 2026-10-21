"""Fixtures shared by the contract suites of this folder.

`uow_factory` gives each test a fresh, empty memory store (`uow_factories.open_memory`),
so a suite written against the ports pins down what the port docstrings leave implicit.
The wave 3 suites (brokers, documents) use it as `test_unit_of_work.py` does.
"""

from collections.abc import AsyncIterator

import pytest

from freight_agent.ports.repositories import UnitOfWorkFactory
from tests.contract.uow_factories import MAX_QUEUE_ATTEMPTS, open_memory


@pytest.fixture
async def uow_factory() -> AsyncIterator[UnitOfWorkFactory]:
    """A fresh, empty store per test."""
    async with open_memory(max_queue_attempts=MAX_QUEUE_ATTEMPTS) as new_uow:
        yield new_uow
