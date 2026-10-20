"""Document ports: where attachment bytes live, how pages are read, the document rows,
and the pipeline the runtime ticks.

Owner: card W3-DOC. C0 wrote the contracts; the memory implementations are
`adapters/documents/memory_blobs.py`, `adapters/documents/fake_pages.py` and
`adapters/persistence/memory/documents.py`.
"""

from typing import Protocol

from freight_agent.domain.documents import Document, Page
from freight_agent.domain.events import Event


class BlobStore(Protocol):
    """Attachment and SOP file bytes. The filesystem now, an object store later."""

    async def put(self, data: bytes, content_type: str, *, name: str = "") -> str:
        """Store `data` and return its reference. The same bytes give the same reference."""
        ...

    async def get(self, ref: str) -> bytes:
        """The bytes of `ref`. Raises `KeyError` for a reference it does not hold."""
        ...


class PageReader(Protocol):
    def pages(self, data: bytes, content_type: str) -> list[Page]:
        """The file's pages in order. A page with too little text also carries its image.

        Synchronous and possibly slow (the pipeline calls it in a worker thread). Raises
        for a file it cannot read; the pipeline then marks the document `failed`.
        """
        ...


class DocumentRepository(Protocol):
    """Document rows, on the unit of work."""

    async def get(self, document_id: str) -> Document | None: ...

    async def for_attachment(self, load_id: str, attachment_id: str) -> Document | None:
        """The document of one attachment of the load."""
        ...

    async def list_for_load(self, load_id: str) -> list[Document]:
        """Oldest `received_at` first, the id breaking ties."""
        ...

    async def list_pending(self, limit: int = 10) -> list[Document]:
        """Pending documents of every load, oldest first."""
        ...

    async def save(self, document: Document) -> None:
        """Insert or replace by `document_id`."""
        ...


class DocumentProcessor(Protocol):
    """Reads the pending documents. It opens its own units of work (the model runs
    outside any transaction) and logs and enqueues one `document_processed` event per
    document it finishes, which it returns."""

    async def process_pending(self, limit: int = 10) -> list[Event]: ...
