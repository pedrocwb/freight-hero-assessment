"""The knowledge base: the index the agent reads the SOP through.

Owner: card W3-KNW. `SopIndex` is the read side the agent uses while it thinks, outside
any transaction (`RuntimeDeps.knowledge`). `by_applies` keeps the deterministic
selection (`domain.sop.resolve`); `search` is retrieval (word overlap, in memory). The
sections come from the `sops/` tree (`adapters/knowledge/memory.py`).
"""

from collections.abc import Sequence
from typing import Protocol

from freight_agent.domain.load import Stage
from freight_agent.domain.sop import ResolvedSection, ResolvedSop, SearchHit


class SopIndex(Protocol):
    """The sections a broker's loads can see: the standard layer, the broker's own layer
    and its load-type layers, merged (`domain.sop.effective_sections`). Another broker's
    sections are never returned."""

    async def by_applies(
        self, broker_id: str, load_types: Sequence[str], intent: str, stage: Stage
    ) -> ResolvedSop:
        """The sections whose intents and stages apply, plus the catalog of every visible
        section (`domain.sop.resolve`)."""
        ...

    async def visible(
        self, broker_id: str, load_types: Sequence[str]
    ) -> tuple[ResolvedSection, ...]:
        """Every visible section, merged, ordered by section id."""
        ...

    async def search(
        self, broker_id: str, load_types: Sequence[str], query: str, k: int = 5
    ) -> tuple[SearchHit, ...]:
        """The `k` visible sections that best match `query`, best first."""
        ...

    async def read(
        self, broker_id: str, load_types: Sequence[str], section_id: str
    ) -> ResolvedSection | None:
        """One visible section, merged; None when the broker cannot see it."""
        ...

    async def version(self, broker_id: str) -> str | None:
        """The content version of what the broker's runs read, recorded with every run."""
        ...
