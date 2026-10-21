"""A `PageReader` for tests, with no PDF library.

Owner: card W3-DOC (C0 wrote a working version). Plain text and Markdown are one page
of text; anything else is one page with an empty image and no text, which sends the
pipeline down its vision path.
"""

from freight_agent.domain.documents import Page

_TEXT_TYPES = frozenset({"text/plain", "text/markdown"})


class FakePageReader:
    """Implements `ports.documents.PageReader`."""

    def pages(self, data: bytes, content_type: str) -> list[Page]:
        kind = content_type.split(";", 1)[0].strip().lower()
        if kind in _TEXT_TYPES:
            return [Page(number=1, text=data.decode("utf-8", errors="replace"))]
        return [Page(number=1, image_png=b"")]
