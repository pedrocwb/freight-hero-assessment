"""Contract suite for the facility memory (`interfaces/memory.py`: `FacilityMemory` and
`ExtractionQueue`) on `StoreFacilityMemory` over each LangGraph store it runs on: the
`InMemoryStore` with an embeddings index (the tests, the evals, Studio, `PERSISTENCE=memory`)
always, and `AsyncPostgresStore` with a pgvector index (`PERSISTENCE=postgres`), marked
`integration`, when a test database is set (`tests/postgres.py`).

Recall, semantic search, writes and deletes, one namespace per client and facility, the
client guardrails as the interface shows them, the extraction queue, and the reset.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime, timedelta

import pytest

from freight_agent.adapters.memory.embeddings import HashEmbeddings, memory_store
from freight_agent.adapters.memory.postgres import LazyPostgresStore, postgres_index
from freight_agent.adapters.memory.store import (
    StoreFacilityMemory,
    facts_namespace,
    jobs_namespace,
)
from freight_agent.domain.memory import (
    ExtractionJob,
    ExtractionStatus,
    FacilityFact,
    FactDelete,
    FactKind,
)
from tests.conftest import LA
from tests.postgres import DATABASE_URL, SKIP_REASON, scratch_schema

pytestmark = pytest.mark.contract

T0 = datetime(2026, 10, 2, 9, 0, tzinfo=LA)
PICKUP = "riverbend-plant~stockton-ca"
DELIVERY = "valley-dc~reno-nv"
DIMS = 256


@asynccontextmanager
async def in_memory() -> AsyncIterator[StoreFacilityMemory]:
    yield StoreFacilityMemory(memory_store(HashEmbeddings(DIMS), DIMS))


@asynccontextmanager
async def on_postgres() -> AsyncIterator[StoreFacilityMemory]:
    """LangGraph's Postgres store, set up in a scratch schema of its own (dropped after)."""
    async with scratch_schema(DATABASE_URL, prefix="memory") as schema:
        store = LazyPostgresStore(
            DATABASE_URL, index=postgres_index(HashEmbeddings(DIMS), DIMS), schema=schema
        )
        try:
            await store.setup()
            yield StoreFacilityMemory(store)
        finally:
            await store.close()


STORES: dict[str, Callable[[], AbstractAsyncContextManager[StoreFacilityMemory]]] = {
    "memory": in_memory,
    "postgres": on_postgres,
}


@pytest.fixture(
    params=[
        pytest.param("memory", id="memory"),
        pytest.param(
            "postgres",
            id="postgres",
            marks=[
                pytest.mark.integration,
                pytest.mark.skipif(not DATABASE_URL, reason=SKIP_REASON),
            ],
        ),
    ]
)
async def memory(request: pytest.FixtureRequest) -> AsyncIterator[StoreFacilityMemory]:
    async with STORES[request.param]() as opened:
        yield opened


def fact(
    fact_id: str,
    text: str,
    *,
    client: str = "northline",
    key: str = PICKUP,
    minutes: int = 0,
    kind: FactKind = FactKind.OTHER,
) -> FacilityFact:
    at = T0 + timedelta(minutes=minutes)
    return FacilityFact(
        fact_id=fact_id,
        client_id=client,
        facility_key=key,
        facility_name="Riverbend plant",
        text=text,
        kind=kind,
        source_load_id="470001",
        source_event_id="m1",
        source_at=at,
        created_at=at,
        updated_at=at,
    )


async def test_a_client_never_reads_another_clients_facts(memory: StoreFacilityMemory) -> None:
    await memory.apply("northline", upserts=[fact("f1", "Check in at gate 4")])
    await memory.apply("acme", upserts=[fact("f2", "Check in at gate 9", client="acme")])

    assert [f.fact_id for f in await memory.recall("northline", PICKUP)] == ["f1"]
    assert [f.fact_id for f in await memory.recall("acme", PICKUP)] == ["f2"]
    assert [f.fact_id for f in await memory.search("acme", "check in gate")] == ["f2"]
    assert [f.fact_id for f in await memory.facts("northline")] == ["f1"]
    assert await memory.recall("keystone", PICKUP) == []


async def test_a_write_naming_another_client_writes_nothing(memory: StoreFacilityMemory) -> None:
    with pytest.raises(ValueError, match="names another client"):
        await memory.apply(
            "northline",
            upserts=[fact("f1", "gate 4"), fact("f2", "gate 9", client="acme")],
        )
    with pytest.raises(ValueError, match="names another client"):
        await memory.apply(
            "northline", deletes=[FactDelete(client_id="acme", facility_key=PICKUP, fact_id="x")]
        )

    assert await memory.facts("northline") == []


async def test_a_value_of_another_client_is_dropped_on_read(memory: StoreFacilityMemory) -> None:
    """Two client ids with the same namespace label still never see each other."""
    await memory.apply("acme.freight", upserts=[fact("f1", "gate 4", client="acme.freight")])

    assert await memory.recall("acme-freight", PICKUP) == []
    assert facts_namespace("acme.freight", PICKUP) == ("facility_memory", "acme-freight", PICKUP)


async def test_recall_is_the_most_recent_first_and_delete_removes(
    memory: StoreFacilityMemory,
) -> None:
    await memory.apply(
        "northline",
        upserts=[
            fact("f1", "Check in at gate 4"),
            fact("f2", "Dock closes at 15:00", minutes=10),
            fact("f3", "PO required at the gate", minutes=5),
            fact("f4", "Unload at door 12", key=DELIVERY),
        ],
    )

    assert [f.fact_id for f in await memory.recall("northline", PICKUP)] == ["f2", "f3", "f1"]
    assert [f.fact_id for f in await memory.recall("northline", PICKUP, limit=1)] == ["f2"]
    assert [f.fact_id for f in await memory.facts("northline")] == ["f2", "f3", "f1", "f4"]

    await memory.apply(
        "northline", deletes=[FactDelete(client_id="northline", facility_key=PICKUP, fact_id="f2")]
    )

    assert [f.fact_id for f in await memory.recall("northline", PICKUP)] == ["f3", "f1"]


async def test_search_ranks_by_meaning_within_the_client_and_the_facility(
    memory: StoreFacilityMemory,
) -> None:
    await memory.apply(
        "northline",
        upserts=[
            fact("f1", "Dock closes at 15:00"),
            fact("f2", "Check in at gate 4"),
            fact("f3", "Check in at the guard shack", key=DELIVERY),
        ],
    )

    best = await memory.search("northline", "where do I check in", limit=3)
    assert best[0].fact_id in ("f2", "f3")
    only_pickup = await memory.search("northline", "check in", facility_key=PICKUP, limit=3)
    assert {f.fact_id for f in only_pickup} == {"f1", "f2"}
    assert only_pickup[0].fact_id == "f2"


async def test_the_queue_takes_a_load_once_and_lists_the_pending_oldest_first(
    memory: StoreFacilityMemory,
) -> None:
    late = ExtractionJob(load_id="2", client_id="acme", queued_at=T0 + timedelta(hours=1))
    early = ExtractionJob(load_id="1", client_id="northline", queued_at=T0)

    assert await memory.enqueue(late)
    assert await memory.enqueue(early)
    assert not await memory.enqueue(early)
    assert [j.load_id for j in await memory.pending()] == ["1", "2"]

    await memory.save(early.model_copy(update={"status": ExtractionStatus.DONE}))

    assert [j.load_id for j in await memory.pending()] == ["2"]
    found = await memory.get("northline", "1")
    assert found is not None and found.status is ExtractionStatus.DONE
    assert await memory.get("acme", "1") is None
    assert jobs_namespace("acme") == ("facility_memory_jobs", "acme")


async def test_a_fact_survives_as_written(memory: StoreFacilityMemory) -> None:
    written = fact("f1", "Check in at gate 4", kind=FactKind.CHECK_IN)
    await memory.apply("northline", upserts=[written])

    assert await memory.recall("northline", PICKUP) == [written]


async def test_a_write_replaces_the_fact_of_the_same_id(memory: StoreFacilityMemory) -> None:
    await memory.apply("northline", upserts=[fact("f1", "Check in at gate 4")])
    await memory.apply("northline", upserts=[fact("f1", "Check in at gate 6", minutes=5)])

    assert [f.text for f in await memory.recall("northline", PICKUP)] == ["Check in at gate 6"]
    assert [f.text for f in await memory.search("northline", "gate")] == ["Check in at gate 6"]


async def test_a_client_prefix_never_reaches_a_longer_client_id(
    memory: StoreFacilityMemory,
) -> None:
    """A namespace prefix matches whole labels: `acme` is not a prefix of `acme2`."""
    await memory.apply("acme2", upserts=[fact("f1", "Check in at gate 9", client="acme2")])

    assert await memory.facts("acme") == []
    assert await memory.search("acme", "check in gate") == []


async def test_clear_forgets_every_fact_and_job(memory: StoreFacilityMemory) -> None:
    await memory.apply("northline", upserts=[fact("f1", "Check in at gate 4")])
    await memory.apply("acme", upserts=[fact("f2", "Gate 9", client="acme", key=DELIVERY)])
    await memory.enqueue(ExtractionJob(load_id="1", client_id="acme", queued_at=T0))

    await memory.clear()

    assert await memory.facts("northline") == await memory.facts("acme") == []
    assert await memory.pending() == []
    assert await memory.get("acme", "1") is None
    assert await memory.enqueue(ExtractionJob(load_id="1", client_id="acme", queued_at=T0))
