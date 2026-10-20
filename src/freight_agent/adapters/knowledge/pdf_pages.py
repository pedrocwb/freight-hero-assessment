"""A small PDF page reader for SOP ingestion, on `pypdfium2`.

Owner: card W3-KNW. The document pipeline has its own reader
(`adapters/documents/pdfium.py`, card W3-DOC); this one keeps the two cards' files apart.

Implements `ports.documents.PageReader`: each page's text layer, and, for a page whose
text is too thin to trust (a scan, a photo), the page rendered as a PNG, so ingestion
can transcribe it on the vision path (role `extract`, `LlmRequest.images`). Plain text
and Markdown are one page of text. The PNG is encoded here with `zlib` (no imaging
library): 8-bit RGB, one filter byte per row.
"""

import struct
import zlib
from typing import Final

import pypdfium2 as pdfium

from freight_agent.domain.documents import Page

PDF: Final = "application/pdf"
_TEXT_TYPES: Final = frozenset({"text/plain", "text/markdown"})
_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"


class UnreadableDocument(ValueError):
    """The bytes are not a document this reader can open."""


class PdfPageReader:
    """`min_chars`: a page with fewer non-blank characters of text also carries its
    image. `scale`: the render scale (1 is 72 dpi; 2 reads well for a vision model)."""

    def __init__(self, *, min_chars: int = 40, scale: float = 2.0) -> None:
        self._min_chars = min_chars
        self._scale = scale

    def pages(self, data: bytes, content_type: str) -> list[Page]:
        kind = content_type.split(";", 1)[0].strip().lower()
        if kind in _TEXT_TYPES:
            return [Page(number=1, text=data.decode("utf-8", errors="replace"))]
        if kind != PDF:
            raise UnreadableDocument(f"cannot read {kind or 'an unnamed type'}: send a PDF")
        try:
            document = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as exc:
            raise UnreadableDocument(f"the PDF cannot be opened: {exc}") from None
        try:
            return [self._page(document, index) for index in range(len(document))]
        finally:
            document.close()

    def _page(self, document: pdfium.PdfDocument, index: int) -> Page:
        page = document[index]
        try:
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_bounded().replace("\r\n", "\n")
            finally:
                textpage.close()
            image = None
            if len("".join(text.split())) < self._min_chars:
                image = _render(page, self._scale)
        finally:
            page.close()
        return Page(number=index + 1, text=text.strip(), image_png=image)


def _render(page: pdfium.PdfPage, scale: float) -> bytes:
    # RGB(A) byte order. pypdfium2 takes any number as the scale; its hint says int.
    bitmap = page.render(scale=scale, rev_byteorder=True)  # pyright: ignore[reportArgumentType]
    try:
        width, height, stride = bitmap.width, bitmap.height, bitmap.stride
        channels = bitmap.n_channels
        buffer = bytes(bitmap.buffer)
    finally:
        bitmap.close()
    rows = bytearray()
    for y in range(height):
        rows += b"\x00" + rgb(buffer[y * stride : y * stride + width * channels], channels)
    return png(width, height, bytes(rows))


def rgb(row: bytes, channels: int) -> bytes:
    """One row of pixels as RGB: alpha dropped (a rendered page is opaque), gray spread."""
    if channels == 3:
        return row
    width = len(row) // channels
    out = bytearray(width * 3)
    for color in range(3):
        out[color::3] = row[(color if channels == 4 else 0) :: channels]
    return bytes(out)


def png(width: int, height: int, filtered_rows: bytes) -> bytes:
    """An 8-bit RGB PNG from rows that already start with their filter byte."""
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        _PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(filtered_rows, 6))
        + _chunk(b"IEND", b"")
    )


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)
