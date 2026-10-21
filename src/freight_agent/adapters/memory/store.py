"""The facility memory and its extraction queue on any LangGraph `BaseStore`.

Namespaces:

- `("facility_memory", <client>, <facility key>)`: one item per fact, keyed by its id,
  whose value is the fact as JSON. The store's index embeds the `text` field, so
  `search` is semantic search over a client's facts (`asearch` on the prefix
  `("facility_memory", <client>)`), and `recall` lists one facility's.
- `("facility_memory_jobs", <client>)`: one item per load to extract, keyed by load id,
  not indexed.

The client label is the first level under each root, so every read and write is scoped to
one client by its namespace, not by a filter a caller could forget. `InMemoryStore` (with
an embeddings index) is the store today; a `PostgresStore` with pgvector takes the same
namespaces.
"""

from collections.abc import Sequence
from typing import Any

from langgraph.store.base import BaseStore

from freight_agent.domain.memory import (
    MAX_FACTS_PER_FACILITY,
    ExtractionJob,
    ExtractionStatus,
    FacilityFact,
    FactDelete,
    namespace_label,
)

FACTS_ROOT = "facility_memory"
JOBS_ROOT = "facility_memory_jobs"
# Items one listing reads at most: a client's facilities times their cap is far below it.
_LISTING_LIMIT = 10_000


def facts_namespace(client_id: str, facility_key: str | None = None) -> tuple[str, ...]:
    prefix = (FACTS_ROOT, namespace_label(client_id))
    return prefix if facility_key is None else (*prefix, facility_key)


def jobs_namespace(client_id: str | None = None) -> tuple[str, ...]:
    return (JOBS_ROOT,) if client_id is None else (JOBS_ROOT, namespace_label(client_id))


class StoreFacilityMemory:
    """Implements `interfaces.memory.FacilityMemory` and `ExtractionQueue` on `store`."""

    def __init__(self, store: BaseStore) -> None:
        self.store = store

    # --- facts --------------------------------------------------------------------------

    async def recall(
        self, client_id: str, facility_key: str, *, limit: int = 5
    ) -> list[FacilityFact]:
        items = await self.store.asearch(
            facts_namespace(client_id, facility_key), limit=MAX_FACTS_PER_FACILITY * 4
        )
        facts = _facts(client_id, (i.value for i in items))
        return _recent_first(facts)[:limit]

    async def search(
        self, client_id: str, query: str, *, facility_key: str | None = None, limit: int = 5
    ) -> list[FacilityFact]:
        items = await self.store.asearch(
            facts_namespace(client_id, facility_key), query=query.strip() or None, limit=limit
        )
        return _facts(client_id, (i.value for i in items))

    async def facts(self, client_id: str) -> list[FacilityFact]:
        items = await self.store.asearch(facts_namespace(client_id), limit=_LISTING_LIMIT)
        facts = _recent_first(_facts(client_id, (i.value for i in items)))
        return sorted(facts, key=lambda f: f.facility_key)  # stable: recent first per facility

    async def apply(
        self,
        client_id: str,
        *,
        upserts: Sequence[FacilityFact] = (),
        deletes: Sequence[FactDelete] = (),
    ) -> None:
        others = {f.client_id for f in upserts} | {d.client_id for d in deletes}
        if others - {client_id}:
            raise ValueError(
                f"a write for client {client_id!r} names another client: "
                + ", ".join(sorted(others - {client_id}))
            )
        for fact in upserts:
            await self.store.aput(
                facts_namespace(client_id, fact.facility_key),
                fact.fact_id,
                fact.model_dump(mode="json"),
            )
        for delete in deletes:
            await self.store.adelete(
                facts_namespace(client_id, delete.facility_key), delete.fact_id
            )

    # --- the extraction queue -----------------------------------------------------------

    async def enqueue(self, job: ExtractionJob) -> bool:
        if await self.get(job.client_id, job.load_id) is not None:
            return False
        await self.save(job)
        return True

    async def get(self, client_id: str, load_id: str) -> ExtractionJob | None:
        item = await self.store.aget(jobs_namespace(client_id), load_id)
        return None if item is None else ExtractionJob.model_validate(item.value)

    async def pending(self, limit: int = 5) -> list[ExtractionJob]:
        items = await self.store.asearch(
            jobs_namespace(),
            filter={"status": ExtractionStatus.PENDING.value},
            limit=_LISTING_LIMIT,
        )
        jobs = [ExtractionJob.model_validate(i.value) for i in items]
        return sorted(jobs, key=lambda j: (j.queued_at, j.client_id, j.load_id))[:limit]

    async def save(self, job: ExtractionJob) -> None:
        await self.store.aput(
            jobs_namespace(job.client_id), job.load_id, job.model_dump(mode="json"), index=False
        )


def _facts(client_id: str, values: Any) -> list[FacilityFact]:
    """The stored facts, checked again for the client: a value of another client is never
    returned, whatever namespace it was found in."""
    facts = [FacilityFact.model_validate(v) for v in values]
    return [f for f in facts if f.client_id == client_id]


def _recent_first(facts: list[FacilityFact]) -> list[FacilityFact]:
    return sorted(facts, key=lambda f: (f.updated_at, f.fact_id), reverse=True)
