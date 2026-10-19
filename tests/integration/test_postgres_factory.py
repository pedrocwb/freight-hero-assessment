"""The lazily opened factory the composition root uses for PERSISTENCE=postgres."""

import json
import os
from pathlib import Path

import pytest

from freight_agent.adapters.persistence.postgres import unit_of_work_factory
from freight_agent.adapters.persistence.postgres.migrate import scratch_schema
from freight_agent.domain.load import Load

DATABASE_URL = os.environ.get("DATABASE_URL", "")
FIXTURE = Path(__file__).resolve().parents[2] / "evals" / "fixtures" / "load_481207.json"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="set DATABASE_URL to run the Postgres tests"),
]


async def test_the_pool_opens_on_the_first_unit_of_work_and_closes_on_demand() -> None:
    load = Load.from_metadata(json.loads(FIXTURE.read_text()))
    async with scratch_schema(DATABASE_URL, prefix="factory") as schema:
        factory = unit_of_work_factory(DATABASE_URL, schema=schema)
        assert factory.pool.closed

        async with factory() as uow:
            await uow.loads.save(load)
            await uow.commit()
        async with factory() as uow:
            assert await uow.loads.get(load.load_id) == load

        await factory.close()
        assert factory.pool.closed
