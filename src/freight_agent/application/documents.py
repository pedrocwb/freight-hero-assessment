"""The document pipeline the runtime ticks before it processes the queue.

Owner: card W3-DOC. C0 ships only `NullDocumentProcessor`, which leaves every pending
document pending, so the runtime behaves as before wave 3.

Contract for DOC (the composition root builds it by these names, see
`bootstrap.build_documents`):

    DocumentPipeline(
        uow_factory=..., clock=..., ids=..., llm=..., blobs=..., pages=..., timeout_s=...
    )

implements `ports.documents.DocumentProcessor`: classify and extract in one `extract`
call with the broker's `DocumentTypeDef` schema, check the values in code
(`FieldDef.check`), save the document, close the requirement obligation on a clean
document, and append and enqueue one `document_processed` event (`doc:{document_id}`).
"""

from freight_agent.domain.events import Event


class NullDocumentProcessor:
    """Implements `ports.documents.DocumentProcessor` by reading nothing."""

    async def process_pending(self, limit: int = 10) -> list[Event]:
        return []
