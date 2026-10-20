"""A small PDF writer for the eval fixtures, with no dependency.

It writes PDF 1.4 files with the standard Type 1 fonts (Helvetica, Helvetica-Bold,
Helvetica-Oblique, WinAnsi encoding), plain uncompressed content streams, optional RGB
images (FlateDecode) and a correct cross-reference table. The output is deterministic:
the same input gives the same bytes.

Used by `make_fixtures.py` in this folder (the document fixtures) and by
`../sops/make_fixtures.py` (the broker SOP). Coordinates given to `Canvas` are measured
from the top-left corner of a US Letter page, in points; the writer flips them to PDF
coordinates.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field

PAGE_W = 612.0
PAGE_H = 792.0

FONTS = {"F1": "Helvetica", "F2": "Helvetica-Bold", "F3": "Helvetica-Oblique"}

# Advance widths (1/1000 em) of characters 32..126, from the Adobe Core 14 AFM files.
_HELVETICA = (
    "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 278 278 584 584 584 556 "
    "1015 667 667 722 722 667 611 778 722 278 500 667 556 833 722 778 "
    "667 778 722 667 611 722 667 944 667 667 611 278 278 278 469 556 "
    "333 556 556 500 556 556 278 556 556 222 222 500 222 833 556 556 "
    "556 556 333 500 278 556 500 722 500 500 500 334 260 334 584"
)
_HELVETICA_BOLD = (
    "278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 333 333 584 584 584 611 "
    "975 722 722 722 722 667 611 778 722 278 556 722 611 833 722 778 "
    "667 778 722 667 611 722 667 944 667 667 611 333 278 333 584 556 "
    "333 556 611 556 611 556 333 611 611 278 278 556 278 889 611 611 "
    "611 611 389 556 333 611 556 778 556 556 500 389 280 389 584"
)
_WIDTHS = {
    "F1": [int(w) for w in _HELVETICA.split()],
    "F2": [int(w) for w in _HELVETICA_BOLD.split()],
}
_WIDTHS["F3"] = _WIDTHS["F1"]  # the oblique face has the regular widths
_EXTRA_WIDTHS = {"•": 350, "–": 556, "—": 1000}


def text_width(text: str, font: str = "F1", size: float = 10.0) -> float:
    """The width of `text` in points, set in `font` at `size`."""
    widths = _WIDTHS[font]
    total = 0
    for ch in text:
        code = ord(ch)
        if 32 <= code <= 126:
            total += widths[code - 32]
        else:
            total += _EXTRA_WIDTHS.get(ch, 556)
    return total * size / 1000.0


def wrap(text: str, width: float, font: str = "F1", size: float = 10.0) -> list[str]:
    """Break `text` into lines no wider than `width` points, at spaces."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if current and text_width(candidate, font, size) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _num(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class Image:
    """An 8-bit RGB raster, rows top to bottom, 3 bytes per pixel, no padding."""

    width: int
    height: int
    rgb: bytes


@dataclass
class Canvas:
    """The content of one page."""

    ops: list[str] = field(default_factory=list)
    images: dict[str, Image] = field(default_factory=dict)

    def text(
        self,
        x: float,
        top: float,
        text: str,
        font: str = "F1",
        size: float = 10.0,
        gray: float = 0.0,
    ) -> None:
        """Write `text` with its baseline `top` points below the top edge."""
        self.ops.append(
            f"BT {_num(gray)} g /{font} {_num(size)} Tf 1 0 0 1 {_num(x)} {_num(PAGE_H - top)} Tm "
            f"({_escape(text)}) Tj ET"
        )

    def text_right(
        self, right: float, top: float, text: str, font: str = "F1", size: float = 10.0
    ) -> None:
        self.text(right - text_width(text, font, size), top, text, font, size)

    def text_center(
        self, center: float, top: float, text: str, font: str = "F1", size: float = 10.0
    ) -> None:
        self.text(center - text_width(text, font, size) / 2, top, text, font, size)

    def line(
        self, x1: float, top1: float, x2: float, top2: float, width: float = 0.6, gray: float = 0.0
    ) -> None:
        self.ops.append(
            f"{_num(gray)} G {_num(width)} w {_num(x1)} {_num(PAGE_H - top1)} m "
            f"{_num(x2)} {_num(PAGE_H - top2)} l S"
        )

    def rect(
        self,
        x: float,
        top: float,
        w: float,
        h: float,
        width: float = 0.6,
        fill: float | None = None,
        stroke: bool = True,
    ) -> None:
        """A rectangle whose top-left corner is (`x`, `top`); `fill` is a gray level."""
        box = f"{_num(x)} {_num(PAGE_H - top - h)} {_num(w)} {_num(h)} re"
        if fill is not None:
            self.ops.append(f"{_num(fill)} g {box} f 0 g")
        if stroke:
            self.ops.append(f"0 G {_num(width)} w {box} S")

    def curve(self, points: list[tuple[float, float]], width: float = 1.1) -> None:
        """A smooth pen stroke through `points` (Catmull-Rom turned into Bezier curves)."""
        if len(points) < 2:
            return
        pts = [points[0], *points, points[-1]]
        x0, t0 = points[0]
        parts = [f"0 G {_num(width)} w 1 J 1 j {_num(x0)} {_num(PAGE_H - t0)} m"]
        for i in range(1, len(pts) - 2):
            (xa, ta), (xb, tb), (xc, tc), (xd, td) = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
            c1 = (xb + (xc - xa) / 6, tb + (tc - ta) / 6)
            c2 = (xc - (xd - xb) / 6, tc - (td - tb) / 6)
            parts.append(
                f"{_num(c1[0])} {_num(PAGE_H - c1[1])} {_num(c2[0])} {_num(PAGE_H - c2[1])} "
                f"{_num(xc)} {_num(PAGE_H - tc)} c"
            )
        parts.append("S 0 J 0 j")
        self.ops.append(" ".join(parts))

    def image(self, name: str, img: Image, x: float, top: float, w: float, h: float) -> None:
        self.images[name] = img
        self.ops.append(
            f"q {_num(w)} 0 0 {_num(h)} {_num(x)} {_num(PAGE_H - top - h)} cm /{name} Do Q"
        )

    def content(self) -> bytes:
        return ("\n".join(self.ops) + "\n").encode("cp1252")


def write_pdf(
    pages: list[Canvas],
    *,
    title: str,
    author: str,
    created: str,
    fonts: bool = True,
) -> bytes:
    """Serialize `pages` into a PDF file. `created` is a PDF date, like
    "D:20261014094500-07'00'", fixed so that a rebuild gives the same bytes.
    With `fonts=False` the pages carry no font resource (an image-only scan)."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    def reserve() -> int:
        return add(b"")

    catalog = reserve()
    pages_tree = reserve()
    font_refs: dict[str, int] = {}
    if fonts:
        for key, base in FONTS.items():
            font_refs[key] = add(
                f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} "
                f"/Encoding /WinAnsiEncoding >>".encode()
            )
    kids: list[int] = []
    for page in pages:
        content = page.content()
        content_ref = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream")
        xobjects: list[str] = []
        for name, img in page.images.items():
            data = zlib.compress(img.rgb, 9)
            img_ref = add(
                (
                    f"<< /Type /XObject /Subtype /Image /Width {img.width} /Height {img.height} "
                    f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
                    f"/Length {len(data)} >>\nstream\n"
                ).encode()
                + data
                + b"\nendstream"
            )
            xobjects.append(f"/{name} {img_ref} 0 R")
        resources = ""
        if font_refs:
            resources += (
                "/Font << " + " ".join(f"/{k} {v} 0 R" for k, v in font_refs.items()) + " >> "
            )
        if xobjects:
            resources += "/XObject << " + " ".join(xobjects) + " >> "
        page_dict = (
            f"<< /Type /Page /Parent {pages_tree} 0 R /MediaBox [0 0 {_num(PAGE_W)} "
            f"{_num(PAGE_H)}] /Resources << {resources}>> /Contents {content_ref} 0 R >>"
        )
        kids.append(add(page_dict.encode()))
    info = add(
        (
            f"<< /Title ({_escape(title)}) /Author ({_escape(author)}) "
            f"/Producer (freight-hero eval fixtures, pdfmini.py) "
            f"/CreationDate ({created}) /ModDate ({created}) >>"
        ).encode("cp1252")
    )
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_tree} 0 R >>".encode()
    objects[pages_tree - 1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
    ).encode()

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)
