"""`BlobStore` in memory: tests, evals (fixture bytes go into the trial's store) and the
platform's sessions, which start over on every reset.

Owner: card W3-DOC (C0 wrote a working version).
"""

import hashlib

PREFIX = "sha256:"


def blob_ref(data: bytes) -> str:
    """The reference of some bytes: their sha256, so the same bytes are stored once."""
    return PREFIX + hashlib.sha256(data).hexdigest()


class MemoryBlobStore:
    """Implements `ports.documents.BlobStore`."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    async def put(self, data: bytes, content_type: str, *, name: str = "") -> str:
        ref = blob_ref(data)
        self._blobs[ref] = data
        return ref

    async def get(self, ref: str) -> bytes:
        try:
            return self._blobs[ref]
        except KeyError:
            raise KeyError(f"no blob {ref!r}") from None
