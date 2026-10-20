"""Postgres `SopStore` (tables `sop_documents`, `sop_drafts`, `sop_versions`, migration 0004).

Owner: card W3-KNW. C0 stub: every method raises until the card lands.
"""

from freight_agent.adapters.persistence.postgres.connection import Session
from freight_agent.domain.sop import SopDocument, SopDraft, SopVersion

_CARD = "card KNW"


class PostgresSopStore:
    def __init__(self, session: Session) -> None:
        self._s = session

    async def save_document(self, document: SopDocument) -> None:
        raise NotImplementedError(_CARD)

    async def get_document(self, document_id: str) -> SopDocument | None:
        raise NotImplementedError(_CARD)

    async def list_documents(self, broker_id: str | None) -> list[SopDocument]:
        raise NotImplementedError(_CARD)

    async def save_draft(self, draft: SopDraft) -> None:
        raise NotImplementedError(_CARD)

    async def get_draft(self, broker_id: str | None) -> SopDraft | None:
        raise NotImplementedError(_CARD)

    async def save_version(self, version: SopVersion) -> None:
        raise NotImplementedError(_CARD)

    async def get_version(self, broker_id: str | None, version: str) -> SopVersion | None:
        raise NotImplementedError(_CARD)

    async def published(self, broker_id: str | None) -> SopVersion | None:
        raise NotImplementedError(_CARD)

    async def versions(self, broker_id: str | None) -> list[SopVersion]:
        raise NotImplementedError(_CARD)
