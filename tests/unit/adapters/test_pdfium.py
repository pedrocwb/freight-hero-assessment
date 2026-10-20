"""The pypdfium2 page reader, on the document fixtures of load 481207.

Owner: card W3-DOC. The text PDFs give their text and no image; the scan gives no text
and a PNG of the page, for the vision path.
"""

import base64
import struct
import zlib
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from freight_agent.adapters.documents.pdfium import (
    PdfiumPageReader,
    UnsupportedDocument,
    encode_png,
)

pytestmark = pytest.mark.unit

DOCS = Path(__file__).resolve().parents[3] / "evals" / "fixtures" / "docs"
PDF = "application/pdf"

# A 12x16 photo of the scanned POD (made with macOS `sips`, metadata segments removed).
PHOTO_JPEG = base64.b64decode(
    "/9j/wAALCAAQAAwBAREA/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA"
    "AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdI"
    "SUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
    "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9sAQwAWFhYWFhYmFhYmNiYmJjZJNjY2NklcSUlJSUlc"
    "b1xcXFxcXG9vb29vb29vhoaGhoaGnJycnJyvr6+vr6+vr6+v/90ABAAC/9oACAEBAAA/AOiQnYOe1Sr0r//Q6RAd"
    "o+gqSv/Z"
)


def fixture(name: str) -> bytes:
    return (DOCS / name).read_bytes()


def png_image(data: bytes) -> tuple[int, int, int, bytes]:
    """(width, height, color type, the unfiltered rows) of a PNG, every CRC checked."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    chunks: dict[bytes, bytes] = {}
    at = 8
    while at < len(data):
        (length,) = struct.unpack(">I", data[at : at + 4])
        tag, body = data[at + 4 : at + 8], data[at + 8 : at + 8 + length]
        (crc,) = struct.unpack(">I", data[at + 8 + length : at + 12 + length])
        assert crc == zlib.crc32(tag + body) & 0xFFFFFFFF
        chunks[tag] = chunks.get(tag, b"") + body
        at += 12 + length
    assert list(chunks)[-1] == b"IEND"
    width, height, depth, color, *_ = struct.unpack(">IIBBBBB", chunks[b"IHDR"])
    assert depth == 8
    return width, height, color, zlib.decompress(chunks[b"IDAT"])


@pytest.mark.parametrize(
    ("name", "printed"),
    [
        ("pod_clean.pdf", ("PROOF OF DELIVERY", "PO NO.\n55821", "Valley DC", "NONE")),
        ("pod_exceptions.pdf", ("PO NO.\n55812", "2 cases damaged, 1 short")),
        ("bol.pdf", ("STRAIGHT BILL OF LADING", "RBF-30417", "SL-44120")),
    ],
)
def test_a_text_pdf_gives_its_text_and_no_image(name: str, printed: tuple[str, ...]) -> None:
    (page,) = PdfiumPageReader().pages(fixture(name), PDF)

    assert page.number == 1
    assert page.image_png is None
    assert len(page.text) > 1000
    assert "\r" not in page.text
    for text in printed:
        assert text in page.text


def test_the_scanned_pod_gives_almost_no_text_and_a_png_of_the_page() -> None:
    (page,) = PdfiumPageReader().pages(fixture("pod_scanned.pdf"), PDF)

    assert sum(c.isalnum() for c in page.text) < 40
    assert page.image_png is not None
    width, height, color, rows = png_image(page.image_png)
    assert (width, height, color) == (1275, 1651, 0)  # 150 dpi, grayscale
    assert len(rows) == height * (width + 1)


def test_the_render_keeps_the_longer_side_under_the_limit() -> None:
    (page,) = PdfiumPageReader(max_side_px=500).pages(fixture("pod_scanned.pdf"), PDF)

    assert page.image_png is not None
    width, height, _, _ = png_image(page.image_png)
    assert max(width, height) <= 500


def test_the_thin_text_threshold_and_the_render_cap_are_settings() -> None:
    (strict,) = PdfiumPageReader(min_text_chars=100_000).pages(fixture("pod_clean.pdf"), PDF)
    (capped,) = PdfiumPageReader(max_rendered=0).pages(fixture("pod_scanned.pdf"), PDF)

    assert strict.text and strict.image_png is not None
    assert capped.image_png is None


def test_a_pdf_is_known_by_its_bytes_whatever_its_content_type() -> None:
    (page,) = PdfiumPageReader().pages(fixture("bol.pdf"), "application/octet-stream")

    assert "RBF-30417" in page.text


def test_a_png_is_one_page_with_the_image_as_it_is() -> None:
    png = encode_png(bytes(range(12)), 2, 2, 6, 3)

    assert PdfiumPageReader().pages(png, "image/png")[0].image_png == png


def test_a_jpeg_photo_is_one_page_rendered_as_a_png() -> None:
    (page,) = PdfiumPageReader().pages(PHOTO_JPEG, "image/jpeg")

    assert page.text == ""
    assert page.image_png is not None
    assert png_image(page.image_png)[:3] == (12, 16, 0)


def test_a_text_file_is_one_page_of_text() -> None:
    (page,) = PdfiumPageReader().pages(
        b"POD signed\r\nPO 55821  \x00\r\n", "text/plain; charset=utf-8"
    )

    assert (page.text, page.image_png) == ("POD signed\nPO 55821", None)


def test_a_file_of_another_kind_is_refused() -> None:
    with pytest.raises(UnsupportedDocument, match="application/zip"):
        PdfiumPageReader().pages(b"PK\x03\x04", "application/zip")
    with pytest.raises(UnsupportedDocument, match="typeless"):
        PdfiumPageReader().pages(b"???", "")


def test_a_broken_pdf_raises_the_pdfium_error() -> None:
    with pytest.raises(pdfium.PdfiumError):
        PdfiumPageReader().pages(b"%PDF-1.7\nnot really a pdf", PDF)


@pytest.mark.parametrize(("channels", "color"), [(1, 0), (3, 2), (4, 6)])
def test_the_png_encoder_writes_each_row_without_the_stride_padding(
    channels: int, color: int
) -> None:
    width, height, stride = 3, 2, 3 * channels + 2  # two bytes of padding per row
    row = bytes(range(width * channels))
    pixels = (row + b"\xee\xee") * height

    decoded = png_image(encode_png(pixels, width, height, stride, channels))

    assert decoded == (width, height, color, (b"\x00" + row) * height)
