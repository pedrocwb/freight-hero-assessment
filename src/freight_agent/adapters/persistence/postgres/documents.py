"""Postgres `DocumentRepository` (table `documents`, migration 0005).

Owner: card W3-DOC. A row is the `Document` as it serializes (`rows.columns`): one
column per field, `extracted` and `checks` as JSONB. Lists sort by `received_at`, then
by natural id (`id_order`, migration 0001), exactly as the memory store sorts.

No read takes a lock. The pipeline needs none: it reads a document again before it
writes it, and the `document_processed` event id (`doc:{document_id}`, unique in the
event log) decides which worker announces it.
"""

from typing import Any, Final

from psycopg.rows import DictRow

from freight_agent.adapters.persistence.postgres import rows
from freight_agent.adapters.persistence.postgres.connection import Session
from freight_agent.domain.documents import Document, DocumentStatus

_COLUMNS: Final = (
    "document_id, load_id, attachment_id, broker_id, blob_ref, filename, content_type,"
    " status, type_id, confidence, extracted, checks, text, pages, received_at, processed_at"
)
_VALUES: Final = ", ".join(f"%({name.strip()})s" for name in _COLUMNS.split(","))
_UPDATES: Final = ", ".join(
    f"{name} = EXCLUDED.{name}"
    for name in (n.strip() for n in _COLUMNS.split(","))
    if name != "document_id"
)
_ORDER: Final = "ORDER BY received_at, id_order(document_id)"


def _params(document: Document) -> dict[str, Any]:
    return rows.columns(document, jsonb=("extracted", "checks"))


def _document(row: DictRow) -> Document:
    return Document.model_validate(row)


class PostgresDocumentRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    async def get(self, document_id: str) -> Document | None:
        row = await self._s.one(
            f"SELECT {_COLUMNS} FROM documents WHERE document_id = %s", (document_id,)
        )
        return None if row is None else _document(row)

    async def for_attachment(self, load_id: str, attachment_id: str) -> Document | None:
        row = await self._s.one(
            f"SELECT {_COLUMNS} FROM documents WHERE load_id = %s AND attachment_id = %s"
            f" {_ORDER} LIMIT 1",
            (load_id, attachment_id),
        )
        return None if row is None else _document(row)

    async def list_for_load(self, load_id: str) -> list[Document]:
        found = await self._s.all(
            f"SELECT {_COLUMNS} FROM documents WHERE load_id = %s {_ORDER}", (load_id,)
        )
        return [_document(row) for row in found]

    async def list_pending(self, limit: int = 10) -> list[Document]:
        if limit <= 0:
            return []
        found = await self._s.all(
            f"SELECT {_COLUMNS} FROM documents WHERE status = %s {_ORDER} LIMIT %s",
            (DocumentStatus.PENDING.value, limit),
        )
        return [_document(row) for row in found]

    async def save(self, document: Document) -> None:
        await self._s.run(
            f"INSERT INTO documents ({_COLUMNS}) VALUES ({_VALUES})"
            f" ON CONFLICT (document_id) DO UPDATE SET {_UPDATES}",
            _params(document),
        )
