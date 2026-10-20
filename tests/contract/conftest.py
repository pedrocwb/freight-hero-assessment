"""Fixtures shared by the contract suites of this folder.

`uow_factory` gives each test a fresh, empty store, once for each implementation in
`uow_factories.UOW_FACTORIES` (memory always; Postgres when `DATABASE_URL` is set), so a
suite written against the ports runs on every implementation. The wave 3 suites (brokers,
SOP store, documents) use it as `test_unit_of_work.py` does.
"""

from collections.abc import AsyncIterator

import pytest

from freight_agent.ports.repositories import UnitOfWorkFactory
from tests.contract.uow_factories import MAX_QUEUE_ATTEMPTS, UOW_FACTORIES, UowFactory


@pytest.fixture(params=list(UOW_FACTORIES.values()), ids=list(UOW_FACTORIES))
async def uow_factory(request: pytest.FixtureRequest) -> AsyncIterator[UnitOfWorkFactory]:
    """A fresh, empty store per test, once for each implementation in the registry."""
    factory: UowFactory = request.param
    if (reason := factory.skip_reason()) is not None:
        pytest.skip(reason)
    async with factory.open_store(max_queue_attempts=MAX_QUEUE_ATTEMPTS) as new_uow:
        yield new_uow
