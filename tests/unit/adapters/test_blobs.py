"""Blob stores and the fake page reader.

Owner: card W3-DOC (C0 wrote the tests of the versions it shipped).
"""

from pathlib import Path

import pytest

from freight_agent.adapters.documents.fs_blobs import FsBlobStore
from freight_agent.adapters.documents.memory_blobs import MemoryBlobStore
from freight_agent.domain.documents import Page
from freight_agent.interfaces.documents import BlobStore
from tests.fakes import FakePageReader


def _stores(tmp_path: Path) -> list[BlobStore]:
    return [MemoryBlobStore(), FsBlobStore(tmp_path / "blobs")]


@pytest.mark.unit
async def test_a_blob_comes_back_by_its_reference(tmp_path: Path) -> None:
    for store in _stores(tmp_path):
        ref = await store.put(b"%PDF-1.7 pod", "application/pdf", name="pod.pdf")

        assert ref.startswith("sha256:")
        assert await store.get(ref) == b"%PDF-1.7 pod"
        assert await store.put(b"%PDF-1.7 pod", "application/pdf") == ref


@pytest.mark.unit
async def test_an_unknown_reference_is_a_key_error(tmp_path: Path) -> None:
    for store in _stores(tmp_path):
        with pytest.raises(KeyError):
            await store.get("sha256:" + "0" * 64)
        with pytest.raises(KeyError):
            await store.get("../../etc/passwd")


@pytest.mark.unit
async def test_the_filesystem_store_writes_nothing_until_the_first_blob(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    store = FsBlobStore(root)
    assert not root.exists()

    ref = await store.put(b"bytes", "application/octet-stream")

    digest = ref.removeprefix("sha256:")
    assert (root / digest[:2] / digest).read_bytes() == b"bytes"
    assert await FsBlobStore(root).get(ref) == b"bytes"  # another process reads it back


@pytest.mark.unit
def test_the_fake_reader_reads_text_and_sends_anything_else_to_vision() -> None:
    reader = FakePageReader()

    assert reader.pages(b"POD signed", "text/plain; charset=utf-8") == [
        Page(number=1, text="POD signed")
    ]
    assert reader.pages(b"# SOP", "text/markdown") == [Page(number=1, text="# SOP")]
    assert reader.pages(b"%PDF", "application/pdf") == [Page(number=1, image_png=b"")]
