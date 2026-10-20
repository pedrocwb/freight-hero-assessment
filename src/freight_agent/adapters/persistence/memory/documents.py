"""In-memory `DocumentRepository` over the memory store's state.

Owner: card W3-DOC (C0 wrote a working version). Rows live in `_State.documents`.
"""

from typing import TYPE_CHECKING

from freight_agent.domain.documents import Document, DocumentStatus

if TYPE_CHECKING:
    from freight_agent.adapters.persistence.memory.store import MemoryStore


def _order(document: Document) -> tuple[object, ...]:
    return (document.received_at, document.document_id)


class MemoryDocumentRepository:
    def __init__(self, store: "MemoryStore") -> None:
        self._s = store

    async def get(self, document_id: str) -> Document | None:
        return self._s.state.documents.get(document_id)

    async def for_attachment(self, load_id: str, attachment_id: str) -> Document | None:
        return next(
            (
                d
                for d in sorted(self._s.state.documents.values(), key=_order)
                if d.load_id == load_id and d.attachment_id == attachment_id
            ),
            None,
        )

    async def list_for_load(self, load_id: str) -> list[Document]:
        found = [d for d in self._s.state.documents.values() if d.load_id == load_id]
        return sorted(found, key=_order)

    async def list_pending(self, limit: int = 10) -> list[Document]:
        pending = [
            d for d in self._s.state.documents.values() if d.status is DocumentStatus.PENDING
        ]
        return sorted(pending, key=_order)[:limit]

    async def save(self, document: Document) -> None:
        self._s.state.documents[document.document_id] = document
