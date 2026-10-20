"""`BlobStore` on the filesystem (`BLOBS_DIR`, default `blobs/`): one file per blob,
named by the sha256 of its bytes. S3 later, behind the same port.

Owner: card W3-DOC (C0 wrote a working version). The folder is created on the first
`put`, so building the store writes nothing.
"""

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

PREFIX = "sha256:"
_HEX = frozenset("0123456789abcdef")


class FsBlobStore:
    """Implements `ports.documents.BlobStore`. Writes are atomic (a temporary file, then
    a rename), so a crash never leaves half a blob under its final name."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def put(self, data: bytes, content_type: str, *, name: str = "") -> str:
        digest = hashlib.sha256(data).hexdigest()
        await asyncio.to_thread(self._write, self._path(digest), data)
        return PREFIX + digest

    async def get(self, ref: str) -> bytes:
        digest = ref.removeprefix(PREFIX)
        if not ref.startswith(PREFIX) or len(digest) != 64 or not set(digest) <= _HEX:
            raise KeyError(f"no blob {ref!r}")
        try:
            return await asyncio.to_thread(self._path(digest).read_bytes)
        except FileNotFoundError:
            raise KeyError(f"no blob {ref!r}") from None

    def _path(self, digest: str) -> Path:
        return self._root / digest[:2] / digest

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        if path.exists():
            return  # the same bytes: the name is their hash
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".blob-")
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(data)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
