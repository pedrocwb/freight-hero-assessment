"""The facility memory interfaces: facts about pickup and delivery places, kept across loads.

`FacilityMemory` is the long-term store of `domain.memory.FacilityFact`s, one namespace
per client and facility (`("facility_memory", <client>, <facility key>)` in the LangGraph
store adapter, `adapters/memory/store.py`). Every read and write names the client, and
an implementation never returns or changes another client's facts. A run reads it
outside any unit of work, as it reads the SOP index; only the extraction writes it.

`ExtractionQueue` holds the loads whose facts are still to be read (`ExtractionJob`),
queued when a load is delivered; `MemoryProcessor` is what the runtime's tick calls to
work through it (`application/memory.py`), like the document pipeline.
"""

from collections.abc import Sequence
from typing import Protocol

from freight_agent.domain.memory import ExtractionJob, FacilityFact, FactDelete


class FacilityMemory(Protocol):
    async def recall(
        self, client_id: str, facility_key: str, *, limit: int = 5
    ) -> list[FacilityFact]:
        """The facility's facts for this client, the most recently updated first."""
        ...

    async def search(
        self, client_id: str, query: str, *, facility_key: str | None = None, limit: int = 5
    ) -> list[FacilityFact]:
        """The client's facts that best match `query` (semantic search), best first; with
        `facility_key`, only that facility's."""
        ...

    async def facts(self, client_id: str) -> list[FacilityFact]:
        """Every fact of the client, by facility key, then the most recent first."""
        ...

    async def apply(
        self,
        client_id: str,
        *,
        upserts: Sequence[FacilityFact] = (),
        deletes: Sequence[FactDelete] = (),
    ) -> None:
        """Write facts (insert or replace by fact id) and delete facts, all of `client_id`.
        `ValueError`, and nothing written, when one names another client."""
        ...


class ExtractionQueue(Protocol):
    async def enqueue(self, job: ExtractionJob) -> bool:
        """Queue the load. False, and nothing changed, when it is already queued or done."""
        ...

    async def get(self, client_id: str, load_id: str) -> ExtractionJob | None: ...

    async def pending(self, limit: int = 5) -> list[ExtractionJob]:
        """Pending jobs of every client, the oldest first."""
        ...

    async def save(self, job: ExtractionJob) -> None:
        """Insert or replace the job of its load."""
        ...


class MemoryProcessor(Protocol):
    """Reads the queued loads' facility facts. It opens its own units of work (the model
    runs outside any transaction) and returns the jobs it finished."""

    async def process_pending(self, limit: int = 5) -> list[ExtractionJob]: ...
