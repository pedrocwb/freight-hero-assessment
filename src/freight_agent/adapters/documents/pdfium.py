"""The production `PageReader`, on `pypdfium2` (Apache/BSD; not PyMuPDF, which is AGPL):
each page's text, and the page rendered as a PNG when its text is too thin to trust.

Owner: card W3-DOC. The composition root finds `PdfiumPageReader()` here by name
(`bootstrap.build_documents`).

What it reads, by the file's first bytes (the sender's content type is a fallback):

- a PDF: every page's text layer; a page with fewer than `min_text_chars` letters and
  digits (a scan, a photo saved as PDF) is also rendered, in grayscale at `dpi` (its
  longer side at most `max_side_px`), for the vision path. At most `max_rendered` pages
  are rendered per file;
- a PNG: one page, the image as it is;
- a JPEG (a phone photo of a POD): one page, re-encoded as PNG through PDFium;
- plain text or Markdown: one page of text.

Anything else is refused with `UnsupportedDocument`, which the pipeline turns into a
`failed` document. PDFium is not thread-safe, and the pipeline calls the reader in a
worker thread, so every PDFium call of this module holds one lock.

There is no Pillow in the project: `encode_png` writes the PNG itself (zlib).
"""

import io
import struct
import threading
import zlib
from typing import Any

import pypdfium2 as pdfium

from freight_agent.domain.documents import Page

_PDF, _PNG, _JPEG = b"%PDF-", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"
_TEXT_TYPES = frozenset({"text/plain", "text/markdown", "text/csv"})
# PDF allows junk before the header; readers look for it in the first kilobyte.
_HEADER_WINDOW = 1024
_POINTS_PER_INCH = 72

_PDFIUM = threading.Lock()


class UnsupportedDocument(ValueError):
    """The file is of a kind the reader cannot read."""


class PdfiumPageReader:
    """Implements `ports.documents.PageReader`."""

    def __init__(
        self,
        *,
        min_text_chars: int = 40,
        dpi: int = 150,
        max_side_px: int = 2000,
        max_rendered: int = 8,
    ) -> None:
        self._min_text_chars = min_text_chars
        self._scale = dpi / _POINTS_PER_INCH
        self._max_side_px = max_side_px
        self._max_rendered = max_rendered

    def pages(self, data: bytes, content_type: str) -> list[Page]:
        match _kind(data, content_type):
            case "pdf":
                return self._pdf(data)
            case "png":
                return [Page(number=1, image_png=data)]
            case "jpeg":
                return [Page(number=1, image_png=self._jpeg_as_png(data))]
            case "text":
                return [Page(number=1, text=_clean(data.decode("utf-8", errors="replace")))]
            case _:
                raise UnsupportedDocument(f"cannot read a {content_type or 'typeless'} file")

    def _pdf(self, data: bytes) -> list[Page]:
        with _PDFIUM:
            document = pdfium.PdfDocument(data)
            try:
                pages: list[Page] = []
                rendered = 0
                for index in range(len(document)):
                    page = document[index]
                    try:
                        text = _page_text(page)
                        image = None
                        if self._thin(text) and rendered < self._max_rendered:
                            image = self._render(page)
                            rendered += 1
                        pages.append(Page(number=index + 1, text=text, image_png=image))
                    finally:
                        page.close()
                return pages
            finally:
                document.close()

    def _thin(self, text: str) -> bool:
        return sum(c.isalnum() for c in text) < self._min_text_chars

    def _render(self, page: Any, scale: float | None = None) -> bytes:
        """The page in grayscale at `scale` (default: `dpi`), its longer side capped."""
        width, height = page.get_size()
        cap = self._max_side_px / max(width, height, 1)
        bitmap = page.render(
            scale=min(self._scale if scale is None else scale, cap), grayscale=True
        )
        try:
            return encode_png(
                bytes(bitmap.buffer), bitmap.width, bitmap.height, bitmap.stride, bitmap.n_channels
            )
        finally:
            bitmap.close()

    def _jpeg_as_png(self, data: bytes) -> bytes:
        """The photo drawn on a page of its own size in pixels, rendered back 1:1."""
        with _PDFIUM:
            document = pdfium.PdfDocument.new()
            try:
                image = pdfium.PdfImage.new(document)
                image.load_jpeg(io.BytesIO(data), inline=True)
                width, height = image.get_px_size()
                page = document.new_page(width, height)
                try:
                    image.set_matrix(pdfium.PdfMatrix().scale(width, height))
                    page.insert_obj(image)
                    page.gen_content()
                    return self._render(page, scale=1.0)
                finally:
                    page.close()
            finally:
                document.close()


def _kind(data: bytes, content_type: str) -> str:
    """By the magic bytes, images first (theirs are at offset 0; a PDF's may come later)."""
    if data.startswith(_PNG):
        return "png"
    if data.startswith(_JPEG):
        return "jpeg"
    if _PDF in data[:_HEADER_WINDOW]:
        return "pdf"
    if content_type.split(";", 1)[0].strip().lower() in _TEXT_TYPES:
        return "text"
    return "unknown"


def _page_text(page: Any) -> str:
    textpage = page.get_textpage()
    try:
        return _clean(textpage.get_text_bounded())
    finally:
        textpage.close()


def _clean(text: str) -> str:
    """Unix newlines, no trailing spaces, no NUL (a text column refuses it)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def encode_png(pixels: bytes, width: int, height: int, stride: int, channels: int) -> bytes:
    """An 8-bit PNG of a raw bitmap: gray (1 channel), RGB (3) or RGBA (4), rows `stride`
    bytes apart. No filter, zlib level 6: a page scan compresses well enough."""
    color = {1: 0, 3: 2, 4: 6}[channels]
    row = width * channels
    raw = b"".join(b"\x00" + pixels[y * stride : y * stride + row] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0)
    return b"".join(
        (
            _PNG,
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(raw, 6)),
            _chunk(b"IEND", b""),
        )
    )


def _chunk(tag: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(tag + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)
