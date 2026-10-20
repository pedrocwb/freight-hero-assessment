"""The knowledge base: SOP documents, drafts and published versions, and the index the
agent reads them through.

Owner: card W3-KNW. Two ports, because they live in different phases of a run:

- `SopStore` is the write side and sits on the unit of work: an upload, a draft edit and
  a publish are transactions (`publish_draft` also bumps the broker, in the same one).
- `SopIndex` is the read side the agent uses while it thinks, outside any transaction
  (`RuntimeDeps.knowledge`). `by_applies` keeps today's deterministic selection
  (`domain.sop.resolve`); `search` is retrieval (pgvector cosine and full text, fused, in
  Postgres; token overlap in memory).

`broker_id` None is the standard layer, which every broker inherits.

Embeddings are the index's business: a publish embeds each section's `search_text` and
hands the vectors to `save_version`, in the order of the version's sections. A store that
does not search by vector (memory) ignores them; Postgres keeps them next to the section,
with the model that made them, and a search only compares vectors of its own model.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from freight_agent.domain.load import Stage
from freight_agent.domain.sop import (
    ResolvedSection,
    ResolvedSop,
    SearchHit,
    SopDocument,
    SopDraft,
    SopVersion,
)


class EmbeddingError(Exception):
    """The embedding model failed or answered something unusable. Names the model."""


@dataclass(frozen=True)
class SectionEmbeddings:
    """One vector per section of a version, in the order of `SopVersion.sections`."""

    model: str
    vectors: tuple[tuple[float, ...], ...]


class SopStore(Protocol):
    async def save_document(self, document: SopDocument) -> None: ...

    async def get_document(self, document_id: str) -> SopDocument | None: ...

    async def list_documents(self, broker_id: str | None) -> list[SopDocument]:
        """The broker's uploads, oldest first."""
        ...

    async def save_draft(self, draft: SopDraft) -> None:
        """Replace the broker's draft (a broker has at most one)."""
        ...

    async def get_draft(self, broker_id: str | None) -> SopDraft | None: ...

    async def save_version(
        self, version: SopVersion, embeddings: SectionEmbeddings | None = None
    ) -> None:
        """Publish a version: it becomes the layer's published one.

        A version's sections never change (its id is their hash). Saving a version that
        exists publishes it again: it becomes the published one, with the new
        `published_at`, `published_by` and `lint`, and new embeddings replace the old.
        """
        ...

    async def get_version(self, broker_id: str | None, version: str) -> SopVersion | None: ...

    async def published(self, broker_id: str | None) -> SopVersion | None:
        """The version published last, or None before the first publish."""
        ...

    async def versions(self, broker_id: str | None) -> list[SopVersion]:
        """Every published version, in the order they were last published: the last one
        is `published`."""
        ...


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


class Embedder(Protocol):
    """Text embeddings for the index. Live: OpenRouter; scripted, replay and CI: a
    deterministic fake (tools never run offline, so no real query is needed)."""

    @property
    def model_id(self) -> str: ...

    @property
    def dim(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector of `dim` floats per text, in order. Raises `EmbeddingError`."""
        ...
