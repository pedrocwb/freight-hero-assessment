"""Postgres `DocumentRepository` (table `documents`, migration 0005).

Owner: card W3-DOC. C0 stub: every method raises until the card lands, so on Postgres
a message with an attachment cannot be ingested before DOC (the memory store can).
"""

from freight_agent.adapters.persistence.postgres.connection import Session
from freight_agent.domain.documents import Document

_CARD = "card DOC"


class PostgresDocumentRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    async def get(self, document_id: str) -> Document | None:
        raise NotImplementedError(_CARD)

    async def for_attachment(self, load_id: str, attachment_id: str) -> Document | None:
        raise NotImplementedError(_CARD)

    async def list_for_load(self, load_id: str) -> list[Document]:
        raise NotImplementedError(_CARD)

    async def list_pending(self, limit: int = 10) -> list[Document]:
        raise NotImplementedError(_CARD)

    async def save(self, document: Document) -> None:
        raise NotImplementedError(_CARD)
