"""The document pipeline.

Owner: card W3-DOC (C0 wrote the tests of the null processor and of the pending
document; DOC adds the pipeline's).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from freight_agent.application.documents import NullDocumentProcessor
from freight_agent.domain.documents import Document, DocumentStatus

AT = datetime(2026, 10, 14, 9, 40, tzinfo=ZoneInfo("America/Los_Angeles"))


@pytest.mark.unit
async def test_the_null_processor_reads_nothing() -> None:
    assert await NullDocumentProcessor().process_pending() == []


@pytest.mark.unit
def test_a_pending_document_has_nothing_extracted_yet() -> None:
    document = Document.pending(
        document_id="doc_1",
        load_id="481207",
        attachment_id="a1",
        broker_id="northline",
        blob_ref="sha256:abc",
        filename="pod.pdf",
        content_type="application/pdf",
        received_at=AT,
    )

    assert document.status is DocumentStatus.PENDING
    assert (document.type_id, document.confidence, document.extracted) == (None, None, {})
    assert (document.checks, document.text, document.pages, document.processed_at) == (
        (),
        "",
        0,
        None,
    )
