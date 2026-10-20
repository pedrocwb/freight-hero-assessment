"""In-memory `SopStore` over the memory store's state.

Owner: card W3-KNW. Rows live in `_State.sop_documents`, `_State.sop_drafts` (one per
broker; None is the standard layer) and `_State.sop_versions` (per broker, in the order
they were last published, so the last one is the published one). The memory index
searches by words, so embeddings are not kept.
"""

from typing import TYPE_CHECKING

from freight_agent.domain.sop import SopDocument, SopDraft, SopVersion, layer_owner
from freight_agent.ports.knowledge import SectionEmbeddings

if TYPE_CHECKING:
    from freight_agent.adapters.persistence.memory.store import MemoryStore


class MemorySopStore:
    def __init__(self, store: "MemoryStore") -> None:
        self._s = store

    async def save_document(self, document: SopDocument) -> None:
        self._s.state.sop_documents[document.document_id] = document

    async def get_document(self, document_id: str) -> SopDocument | None:
        return self._s.state.sop_documents.get(document_id)

    async def list_documents(self, broker_id: str | None) -> list[SopDocument]:
        owner = layer_owner(broker_id)
        found = [d for d in self._s.state.sop_documents.values() if d.broker_id == owner]
        return sorted(found, key=lambda d: (d.uploaded_at, d.document_id))

    async def save_draft(self, draft: SopDraft) -> None:
        self._s.state.sop_drafts[draft.broker_id] = draft

    async def get_draft(self, broker_id: str | None) -> SopDraft | None:
        return self._s.state.sop_drafts.get(layer_owner(broker_id))

    async def save_version(
        self, version: SopVersion, embeddings: SectionEmbeddings | None = None
    ) -> None:
        published = self._s.state.sop_versions.setdefault(version.broker_id, [])
        known = next((v for v in published if v.version == version.version), None)
        if known is not None:
            published.remove(known)
            version = version.model_copy(update={"sections": known.sections})
        published.append(version)

    async def get_version(self, broker_id: str | None, version: str) -> SopVersion | None:
        published = self._s.state.sop_versions.get(layer_owner(broker_id), [])
        return next((v for v in published if v.version == version), None)

    async def published(self, broker_id: str | None) -> SopVersion | None:
        published = self._s.state.sop_versions.get(layer_owner(broker_id), [])
        return published[-1] if published else None

    async def versions(self, broker_id: str | None) -> list[SopVersion]:
        return list(self._s.state.sop_versions.get(layer_owner(broker_id), []))
