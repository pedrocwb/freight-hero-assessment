"""Documents: attachments the pipeline reads, classifies and checks against the load.

Owner: card W3-DOC (the pipeline fills these rows); C0 wrote the shape.

A message's attachment is saved as a `pending` document in the unit of work that logs
the message (`AgentRuntime.ingest_message`). The document pipeline
(`application.documents`, `ports.documents.DocumentProcessor`) reads its pages, asks the
`extract` model for the type and the values of the broker's `DocumentTypeDef`, checks the
values in code (`FieldDef.check`) and saves the document as `processed` (every check ok),
`needs_review` (a check failed or the type is uncertain) or `failed` (unreadable). It then
logs one `document_processed` event (`doc:{document_id}`), and that event's run acts on
the document; the message's own run never does.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from freight_agent.domain.base import DomainModel


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class Page(DomainModel):
    """One page as the page reader returns it: its text, and an image when the text is
    too thin to trust (a scan or a photo), for the vision path."""

    number: int  # 1-based
    text: str = ""
    image_png: bytes | None = None


class FieldCheck(DomainModel):
    """One extracted value checked against the load (a PO number, the MC number)."""

    field: str
    expected: str | None
    found: str | None
    ok: bool


class Document(DomainModel):
    document_id: str
    load_id: str
    attachment_id: str
    broker_id: str
    blob_ref: str
    filename: str
    content_type: str
    status: DocumentStatus = DocumentStatus.PENDING
    type_id: str | None = None  # a `DocumentTypeDef.type_id` of the broker, once classified
    confidence: float | None = None
    extracted: dict[str, Any] = Field(default_factory=dict)
    checks: tuple[FieldCheck, ...] = ()
    text: str = ""  # every page's text, for read_document
    pages: int = 0
    received_at: datetime
    processed_at: datetime | None = None

    @classmethod
    def pending(
        cls,
        *,
        document_id: str,
        load_id: str,
        attachment_id: str,
        broker_id: str,
        blob_ref: str,
        filename: str,
        content_type: str,
        received_at: datetime,
    ) -> "Document":
        """An attachment just received, before the pipeline reads it."""
        return cls(
            document_id=document_id,
            load_id=load_id,
            attachment_id=attachment_id,
            broker_id=broker_id,
            blob_ref=blob_ref,
            filename=filename,
            content_type=content_type,
            received_at=received_at,
        )
