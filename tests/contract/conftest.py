"""Fixtures shared by the contract suites of this folder.

`uow_factory` gives each test a fresh, empty store, once for each implementation in
`uow_factories.UOW_FACTORIES` (memory always; Postgres, marked `integration`, when a test
database is set), so a suite written against the interfaces runs on every implementation
and pins down what the interface docstrings leave implicit.
"""

from collections.abc import AsyncIterator

import pytest

from freight_agent.interfaces.repositories import UnitOfWorkFactory
from tests.contract.uow_factories import MAX_QUEUE_ATTEMPTS, UOW_FACTORIES, UowFactory


@pytest.fixture(
    params=[
        pytest.param(f, id=name, marks=[pytest.mark.integration] if f.needs_database else [])
        for name, f in UOW_FACTORIES.items()
    ]
)
async def uow_factory(request: pytest.FixtureRequest) -> AsyncIterator[UnitOfWorkFactory]:
    """A fresh, empty store per test, once for each implementation in the registry."""
    factory: UowFactory = request.param
    if (reason := factory.skip_reason()) is not None:
        pytest.skip(reason)
    async with factory.open_store(max_queue_attempts=MAX_QUEUE_ATTEMPTS) as new_uow:
        yield new_uow
