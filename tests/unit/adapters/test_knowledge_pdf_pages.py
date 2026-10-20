"""The SOP ingestion's page reader, on the fixture PDFs (pypdfium2, no network)."""

import struct
import zlib
from pathlib import Path

import pytest

from freight_agent.adapters.knowledge.pdf_pages import (
    PdfPageReader,
    UnreadableDocument,
    png,
    rgb,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[3] / "evals" / "fixtures"
ACME = (FIXTURES / "sops" / "acme_sop.pdf").read_bytes()
SCANNED = (FIXTURES / "docs" / "pod_scanned.pdf").read_bytes()


def decode(image: bytes) -> tuple[int, int, bytes]:
    """Width, height and the filtered rows of an RGB PNG written by `png`."""
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    chunks: dict[bytes, bytes] = {}
    at = 8
    while at < len(image):
        (length,) = struct.unpack(">I", image[at : at + 4])
        kind, data = image[at + 4 : at + 8], image[at + 8 : at + 8 + length]
        (crc,) = struct.unpack(">I", image[at + 8 + length : at + 12 + length])
        assert crc == zlib.crc32(kind + data) & 0xFFFFFFFF
        chunks[kind] = data
        at += 12 + length
    width, height, depth, color, *_ = struct.unpack(">IIBBBBB", chunks[b"IHDR"])
    assert (depth, color) == (8, 2)  # 8-bit RGB
    assert b"IEND" in chunks
    return width, height, zlib.decompress(chunks[b"IDAT"])


def test_a_text_pdf_gives_each_page_s_text_and_no_image() -> None:
    pages = PdfPageReader().pages(ACME, "application/pdf")

    assert [p.number for p in pages] == [1, 2, 3]
    assert "Late truck: asking the driver for an ETA" in pages[0].text
    assert "Proof of delivery (POD)" in "\n".join(p.text for p in pages)
    assert all(p.image_png is None for p in pages)
    assert "\r" not in pages[0].text


def test_a_scanned_page_also_carries_its_image() -> None:
    (page,) = PdfPageReader(scale=0.5).pages(SCANNED, "application/pdf; name=pod.pdf")

    assert page.text == ""
    assert page.image_png is not None
    width, height, rows = decode(page.image_png)
    assert width > 0 and height > 0
    assert len(rows) == height * (1 + width * 3)
    assert rows[0] == 0  # each row starts with filter 0


def test_a_page_below_the_threshold_is_rendered_too() -> None:
    pages = PdfPageReader(min_chars=100_000, scale=0.2).pages(ACME, "application/pdf")

    assert all(p.image_png is not None and p.text for p in pages)


def test_text_and_markdown_are_one_page_of_text() -> None:
    (page,) = PdfPageReader().pages(b"# Acme\n1. Ask.", "text/markdown; charset=utf-8")

    assert (page.number, page.text, page.image_png) == (1, "# Acme\n1. Ask.", None)


@pytest.mark.parametrize(
    ("data", "content_type", "problem"),
    [
        (b"PK\x03\x04", "application/vnd.openxmlformats", "send a PDF"),
        (b"", "", "an unnamed type"),
        (b"not a pdf", "application/pdf", "cannot be opened"),
    ],
)
def test_what_cannot_be_read_says_so(data: bytes, content_type: str, problem: str) -> None:
    with pytest.raises(UnreadableDocument, match=problem):
        PdfPageReader().pages(data, content_type)


def test_rows_become_rgb_whatever_the_bitmap_holds() -> None:
    assert rgb(b"\x01\x02\x03", 3) == b"\x01\x02\x03"
    assert rgb(b"\x01\x02\x03\xff\x04\x05\x06\xff", 4) == b"\x01\x02\x03\x04\x05\x06"
    assert rgb(b"\x07\x08", 1) == b"\x07\x07\x07\x08\x08\x08"


def test_a_png_holds_its_size_and_rows() -> None:
    rows = b"\x00\x01\x02\x03\x00\x04\x05\x06"

    assert decode(png(1, 2, rows)) == (1, 2, rows)
