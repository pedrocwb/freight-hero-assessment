"""Rebuild `acme_sop.pdf` from `acme_sop.md` (the broker SOP fixture for onboarding tests).

    uv run python evals/fixtures/sops/make_fixtures.py

No dependency: the PDF is written by `../docs/pdfmini.py` (standard Helvetica fonts, plain
content streams). The Markdown file is the source of truth; this script lays it out like a
broker's printed procedure (letterhead, running header, page footer) and understands only
the subset the file uses: `#` and `##` headings, paragraphs, numbered, lettered and dashed
list items (three spaces of indent per level) and pipe tables.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "acme_sop.md"
TARGET = HERE / "acme_sop.pdf"


def _load_pdfmini() -> ModuleType:
    path = HERE.parent / "docs" / "pdfmini.py"
    spec = importlib.util.spec_from_file_location("pdfmini", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pdfmini"] = module
    spec.loader.exec_module(module)
    return module


pdf = _load_pdfmini()

LEFT = 72.0
RIGHT = 540.0
TOP = 78.0  # first baseline below the running header
BOTTOM = 736.0  # no baseline below this line
BODY_FONT = "F1"
BODY_SIZE = 10.0
LEADING = 13.0
TABLE_SIZE = 8.8
TABLE_LEADING = 11.4


@dataclass
class Heading:
    level: int
    text: str


@dataclass
class Paragraph:
    text: str


@dataclass
class Item:
    marker: str
    level: int
    text: str


@dataclass
class Table:
    rows: list[list[str]] = field(default_factory=list)


Block = Heading | Paragraph | Item | Table

_ITEM = re.compile(r"^( *)(\d+\.|[a-z]\.|-) +(.*)$")


def parse(markdown: str) -> list[Block]:
    blocks: list[Block] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append(Paragraph(" ".join(paragraph)))
            paragraph.clear()

    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        item = _ITEM.match(line)
        if not line.strip():
            flush()
        elif line.startswith("#"):
            flush()
            marks, _, text = line.partition(" ")
            blocks.append(Heading(len(marks), text.strip()))
        elif line.startswith("|"):
            flush()
            table = Table()
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(set(c) <= set("-:") for c in cells):
                    table.rows.append(cells)
                i += 1
            blocks.append(table)
            continue
        elif item:
            flush()
            blocks.append(Item(item.group(2), len(item.group(1)) // 3, item.group(3)))
        elif line.startswith(" ") and blocks and isinstance(blocks[-1], Item) and not paragraph:
            blocks[-1].text += " " + line.strip()
        else:
            paragraph.append(line.strip())
        i += 1
    flush()
    return blocks


class Layout:
    def __init__(self) -> None:
        self.pages: list[pdf.Canvas] = []
        self.top = 0.0
        self.new_page()

    @property
    def page(self) -> pdf.Canvas:
        return self.pages[-1]

    def new_page(self) -> None:
        self.pages.append(pdf.Canvas())
        self.top = TOP

    def need(self, height: float) -> None:
        """Start a new page unless `height` more points fit above the bottom margin."""
        if self.top + height > BOTTOM and self.top > TOP:
            self.new_page()

    def title(self, text: str) -> None:
        self.page.text(LEFT, self.top + 4, "ACME FREIGHT, INC.", "F2", 11.0)
        self.page.text(LEFT, self.top + 16, "Carrier Operations", "F1", 8.5, gray=0.35)
        self.top += 44
        for line in pdf.wrap(text, RIGHT - LEFT, "F2", 15.0):
            self.page.text(LEFT, self.top, line, "F2", 15.0)
            self.top += 19
        self.top += 2

    def heading(self, text: str) -> None:
        self.need(16 + 12 + 3 * LEADING)  # keep a heading with its first lines
        self.top += 16
        self.page.text(LEFT, self.top, text, "F2", 12.0)
        self.page.line(LEFT, self.top + 4, RIGHT, self.top + 4, 0.5, gray=0.55)
        self.top += 6 + LEADING

    def lines(self, x: float, text: str, font: str = BODY_FONT) -> None:
        for line in pdf.wrap(text, RIGHT - x, font, BODY_SIZE):
            self.need(LEADING)
            self.page.text(x, self.top, line, font, BODY_SIZE)
            self.top += LEADING

    def paragraph(self, text: str) -> None:
        self.lines(LEFT, text)
        self.top += 5

    def item(self, block: Item) -> None:
        marker_x = LEFT + 6 + block.level * 20
        text_x = marker_x + 16
        marker = "•" if block.marker == "-" else block.marker
        self.need(LEADING)
        self.page.text(marker_x, self.top, marker, BODY_FONT, BODY_SIZE)
        self.lines(text_x, block.text)
        self.top += 2.5

    def table(self, table: Table) -> None:
        width = RIGHT - LEFT
        columns = len(table.rows[0])
        natural = [
            min(max(pdf.text_width(row[c], "F2", TABLE_SIZE) for row in table.rows), 230.0) + 12
            for c in range(columns)
        ]
        scale = width / sum(natural)
        widths = [w * scale for w in natural]
        self.top -= 6
        for r, row in enumerate(table.rows):
            font = "F2" if r == 0 else "F1"
            cells = [pdf.wrap(text, widths[c] - 10, font, TABLE_SIZE) for c, text in enumerate(row)]
            height = max(len(c) for c in cells) * TABLE_LEADING + 7
            self.need(height)
            x = LEFT
            for c, cell in enumerate(cells):
                self.page.rect(x, self.top, widths[c], height, 0.5, 0.9 if r == 0 else None)
                for n, line in enumerate(cell):
                    self.page.text(x + 5, self.top + 11 + n * TABLE_LEADING, line, font, TABLE_SIZE)
                x += widths[c]
            self.top += height
        self.top += 6 + LEADING


def render(blocks: list[Block]) -> list[pdf.Canvas]:
    layout = Layout()
    for i, block in enumerate(blocks):
        if isinstance(block, Heading) and block.level == 1:
            layout.title(block.text)
        elif isinstance(block, Heading):
            layout.heading(block.text)
        elif isinstance(block, Paragraph):
            layout.paragraph(block.text)
        elif isinstance(block, Item):
            layout.item(block)
            following = blocks[i + 1] if i + 1 < len(blocks) else None
            if not isinstance(following, Item):
                layout.top += 4
        else:
            layout.table(block)
    total = len(layout.pages)
    for number, page in enumerate(layout.pages, start=1):
        page.text(LEFT, 44, "Acme Freight, Inc.", "F2", 8.0)
        page.text_right(RIGHT, 44, "CO-07  In-Transit Communication  |  Revision 3", "F1", 8.0)
        page.line(LEFT, 49, RIGHT, 49, 0.5, gray=0.4)
        page.line(LEFT, 748, RIGHT, 748, 0.5, gray=0.4)
        page.text(
            LEFT, 760, "Controlled document. A printed copy is for reference only.", "F1", 7.5
        )
        page.text_right(RIGHT, 760, f"Page {number} of {total}", "F1", 7.5)
    return layout.pages


def main() -> None:
    pages = render(parse(SOURCE.read_text(encoding="utf-8")))
    data = pdf.write_pdf(
        pages,
        title="CO-07 In-Transit Communication",
        author="Acme Freight, Inc. Carrier Operations",
        created="D:20260901080000-07'00'",
    )
    TARGET.write_bytes(data)
    print(f"wrote {TARGET.relative_to(HERE.parents[2])}: {len(pages)} pages, {len(data)} bytes")


if __name__ == "__main__":
    main()
