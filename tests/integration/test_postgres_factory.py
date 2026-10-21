"""The lazily opened factory the composition root uses for `PERSISTENCE=postgres`, and
`clear()`, which the platform's reset runs."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from freight_agent.adapters.persistence.postgres import PostgresUnitOfWorkFactory
from freight_agent.adapters.persistence.postgres.migrate import scratch_schema
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.load import Channel, Load
from tests.postgres import DATABASE_URL, POSTGRES

FIXTURE = Path(__file__).resolve().parents[2] / "evals" / "fixtures" / "load_481207.json"

pytestmark = POSTGRES


async def test_the_pool_opens_on_the_first_unit_of_work_and_closes_on_demand() -> None:
    load = Load.from_metadata(json.loads(FIXTURE.read_text()))
    async with scratch_schema(DATABASE_URL, prefix="factory") as schema:
        factory = PostgresUnitOfWorkFactory(DATABASE_URL, schema=schema)
        assert factory.pool.closed

        async with factory() as uow:
            await uow.loads.save(load)
            await uow.commit()
        async with factory() as uow:
            assert await uow.loads.get(load.load_id) == load

        await factory.close()
        assert factory.pool.closed


async def test_clear_empties_every_table_and_the_store_takes_new_rows() -> None:
    load = Load.from_metadata(json.loads(FIXTURE.read_text()))
    event = Event(
        event_id="m1",
        load_id=load.load_id,
        type=EventType.INBOUND_MESSAGE,
        occurred_at=datetime(2026, 10, 13, 14, 0, tzinfo=UTC),
        channel=Channel.SMS,
        body="ok",
    )
    async with scratch_schema(DATABASE_URL, prefix="factory") as schema:
        factory = PostgresUnitOfWorkFactory(DATABASE_URL, schema=schema)
        try:
            for _ in range(2):  # the same rows go in again after a clear
                async with factory() as uow:
                    await uow.loads.save(load)
                    await uow.queue.enqueue(event)
                    await uow.commit()
                async with factory() as uow:
                    assert await uow.loads.list_ids() == [load.load_id]
                    assert await uow.queue.size() == 1

                await factory.clear()

                async with factory() as uow:
                    assert await uow.loads.list_ids() == []
                    assert await uow.events.get("m1") is None
                    assert await uow.queue.size() == 0
        finally:
            await factory.close()


@pytest.mark.parametrize("schema", ["Upper", "x; DROP SCHEMA public"])
def test_an_invalid_schema_name_fails_before_any_connection(schema: str) -> None:
    with pytest.raises(ValueError, match="schema"):
        PostgresUnitOfWorkFactory("postgresql://never.connects.invalid/db", schema=schema)
