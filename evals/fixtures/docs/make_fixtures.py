"""Rebuild the document fixtures of load 481207 and their expected extractions.

    uv run --with pypdfium2 python evals/fixtures/docs/make_fixtures.py

Writes `pod_clean.pdf`, `pod_exceptions.pdf`, `bol.pdf` (text PDFs, drawn with
`pdfmini.py`: standard Helvetica fonts, plain content streams), `pod_scanned.pdf` (the clean
POD rendered to pixels by pypdfium2, slightly rotated and toned like a scan, embedded as
one RGB image with no text layer) and one `<name>.expected.json` per PDF.

pypdfium2 is only needed for the scan; it comes from `uv run --with pypdfium2` and is not a
project dependency. Without it the script still writes the three text PDFs and every JSON
file, and says that the scan was skipped. No other dependency (no Pillow).

The values drawn on the documents and the expected values come from the same constants
below, so a change to one is a change to both.
"""

from __future__ import annotations

import importlib.util
import json
import math
import random
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent


def _load_pdfmini() -> ModuleType:
    path = HERE / "pdfmini.py"
    spec = importlib.util.spec_from_file_location("pdfmini", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pdfmini"] = module
    spec.loader.exec_module(module)
    return module


pdf = _load_pdfmini()

# --- load 481207 (evals/fixtures/load_481207.json) ----------------------------------------
LOAD_ID = "481207"
PO = "55821"
PO_TYPO = "55812"
CARRIER = "Prairie Haul LLC"
MC = "778120"
SHIPPER = "Riverbend Foods"
PICKUP_STOP = "Riverbend plant"
PICKUP_CITY = "Stockton, CA"
PICKUP_WINDOW = ("2026-10-13T06:00", "2026-10-13T14:00")
DELIVERY_STOP = "Valley DC"
DELIVERY_CITY = "Reno, NV"
DELIVERY_APPOINTMENT = "2026-10-14T09:30"  # fixed appointment
COMMODITY = "Canned tomato sauce"
WEIGHT_LB = 38000

# --- values printed on the documents -------------------------------------------------------
BOL_NUMBER = "RBF-30417"
SEAL = "SL-44120"
TRAILER = "PH-5327"
PALLETS = 20
CASES = 1600
PICKUP_DATE = "2026-10-13"
DELIVERED_AT = "2026-10-14T09:42"
RECEIVED_BY = "Dana Whitfield"
DRIVER = "Marco Ruiz"
SHIPPER_SIGNER = "K. Patel"
EXCEPTIONS = "2 cases damaged, 1 short"

LEFT = 40.0
RIGHT = 572.0


def us_date(iso: str) -> str:
    """2026-10-14 or 2026-10-14T09:42 -> 10/14/2026, as US paperwork prints it."""
    year, month, day = iso[:10].split("-")
    return f"{month}/{day}/{year}"


def clock(iso: str) -> str:
    return iso[11:16]


# --- drawing helpers -----------------------------------------------------------------------


def label_cell(
    c: Any, x: float, top: float, w: float, h: float, label: str, value: str, size: float = 11.0
) -> None:
    c.rect(x, top, w, h)
    c.text(x + 5, top + 10, label, "F2", 6.8)
    c.text(x + 5, top + h - 9, value, "F2", size)


def signature(c: Any, x: float, top: float, w: float, seed: int) -> None:
    """A pen scribble that looks like a handwritten signature, sitting on the line `top`."""
    rng = random.Random(seed)
    base = top - 5
    pen = x + 10
    # A tall looped capital, then small cursive letters (each one loops back a little).
    points = [
        (pen - 2, base - 3),
        (pen + 3, base - 26),
        (pen + 8, base - 22),
        (pen + 4, base - 7),
        (pen + 13, base - 12),
    ]
    pen += 15
    for _ in range(rng.randint(7, 9)):
        tall = rng.random() < 0.25
        height = rng.uniform(13.0, 19.0) if tall else rng.uniform(5.0, 9.5)
        step = rng.uniform(7.0, 10.5)
        points += [
            (pen + step * 0.55, base - height),
            (pen + step * 0.2, base - height * 0.65),
            (pen + step, base - rng.uniform(0.0, 2.5)),
        ]
        pen += step
    c.curve(points, 1.1)
    # The flourish: back under the name.
    c.curve([(pen, base - 1), (pen + 9, base - 7), (pen - 20, base + 3), (x + 14, base + 1)], 0.8)


def check_mark(c: Any, x: float, top: float) -> None:
    c.line(x + 1.5, top + 4.5, x + 3.8, top + 7.5, 1.3)
    c.line(x + 3.8, top + 7.5, x + 8.5, top + 0.5, 1.3)


def footer(c: Any, text: str) -> None:
    c.line(LEFT, 752, RIGHT, 752, 0.4)
    c.text(LEFT, 763, text, "F1", 7.0)
    c.text_right(RIGHT, 763, "Page 1 of 1", "F1", 7.0)


def table(
    c: Any,
    top: float,
    widths: list[float],
    rows: list[list[str]],
    row_h: float = 19.0,
    bold_last: bool = False,
) -> float:
    """Rows of cells; the first row is the gray header. Returns the top below the table."""
    for r, row in enumerate(rows):
        x = LEFT
        header = r == 0
        bold = header or (bold_last and r == len(rows) - 1)
        for w, cell in zip(widths, row, strict=True):
            c.rect(x, top, w, row_h, 0.5, 0.88 if header else None)
            c.text(x + 5, top + row_h - 6.5, cell, "F2" if bold else "F1", 7.2 if header else 9.0)
            x += w
        top += row_h
    return top


# --- the POD -------------------------------------------------------------------------------


def draw_pod(*, po: str, exceptions: str) -> Any:
    c = pdf.Canvas()
    c.text(LEFT, 62, CARRIER.upper(), "F2", 17)
    c.text(LEFT, 76, f"Truckload carrier  |  MC {MC}  |  USDOT 3318842", "F1", 8.5)
    c.text(LEFT, 87, "4410 E Jensen Ave, Fresno, CA 93725", "F1", 8.5)
    c.text_right(RIGHT, 62, "PROOF OF DELIVERY", "F2", 18)
    c.text_right(RIGHT, 76, "Delivery receipt  |  Form DR-2", "F1", 8.5)
    c.text_right(RIGHT, 87, "Original", "F1", 8.5)
    c.line(LEFT, 98, RIGHT, 98, 1.6)

    refs = [
        ("LOAD NO.", LOAD_ID),
        ("BOL NO.", BOL_NUMBER),
        ("PO NO.", po),
        ("TRAILER NO.", TRAILER),
        ("SEAL NO.", SEAL),
    ]
    w = (RIGHT - LEFT) / len(refs)
    for i, (label, value) in enumerate(refs):
        label_cell(c, LEFT + i * w, 108, w, 36, label, value)

    half = (RIGHT - LEFT - 8) / 2
    for x, label, lines in (
        (
            LEFT,
            "SHIPPER",
            [SHIPPER, PICKUP_STOP, PICKUP_CITY, f"Picked up: {us_date(PICKUP_DATE)}"],
        ),
        (
            LEFT + half + 8,
            "RECEIVER (CONSIGNEE)",
            [
                DELIVERY_STOP,
                "Receiving dock",
                DELIVERY_CITY,
                f"Appointment: {us_date(DELIVERY_APPOINTMENT)} {clock(DELIVERY_APPOINTMENT)}",
            ],
        ),
    ):
        c.rect(x, 154, half, 84)
        c.text(x + 6, 166, label, "F2", 7.2)
        c.text(x + 6, 184, lines[0], "F2", 11)
        for n, line in enumerate(lines[1:]):
            c.text(x + 6, 198 + n * 12.5, line, "F1", 9.2)

    third = (RIGHT - LEFT) / 3
    label_cell(c, LEFT, 248, third, 36, "CARRIER", f"{CARRIER}, MC {MC}", 10)
    label_cell(c, LEFT + third, 248, third, 36, "DRIVER", DRIVER, 10)
    label_cell(
        c,
        LEFT + 2 * third,
        248,
        third,
        36,
        "DELIVERED (DATE AND TIME)",
        f"{us_date(DELIVERED_AT)}  {clock(DELIVERED_AT)} PT",
        11.5,
    )

    c.text(LEFT, 304, "FREIGHT", "F2", 8)
    top = table(
        c,
        310,
        [96, 96, 244, 96],
        [
            ["HANDLING UNITS", "PIECES", "DESCRIPTION", "WEIGHT (LB)"],
            [f"{PALLETS} pallets", f"{CASES:,} cases", f"{COMMODITY}, dry van", f"{WEIGHT_LB:,}"],
            ["", "", "", ""],
            ["", "", "", ""],
            ["TOTAL", f"{CASES:,} cases", "", f"{WEIGHT_LB:,}"],
        ],
        bold_last=True,
    )

    top += 12
    c.rect(LEFT, top, RIGHT - LEFT, 74)
    c.text(LEFT + 6, top + 12, "EXCEPTIONS", "F2", 8)
    c.text(LEFT + 62, top + 12, "(shortage, overage, damage, refused)", "F1", 7.5)
    marks = {"Shortage": "short" in exceptions, "Damage": "damage" in exceptions}
    for n, name in enumerate(("Shortage", "Overage", "Damage", "Refused")):
        bx = LEFT + 8 + n * 92
        c.rect(bx, top + 21, 9, 9, 0.6)
        if marks.get(name):
            check_mark(c, bx, top + 21)
        c.text(bx + 14, top + 29, name, "F1", 8.5)
    c.text(LEFT + 6, top + 56, "Notes:", "F2", 8.5)
    c.text(LEFT + 44, top + 57, exceptions or "NONE", "F3", 12)
    c.line(LEFT + 42, top + 60, RIGHT - 8, top + 60, 0.4)

    top += 92
    c.text(
        LEFT,
        top,
        "Received the freight described above in good order and condition, except as noted "
        "under Exceptions.",
        "F1",
        8.5,
    )

    top += 22
    col = (RIGHT - LEFT - 24) / 2
    for x, label, signer, line2, seed in (
        (
            LEFT,
            "RECEIVER SIGNATURE",
            RECEIVED_BY,
            f"Signed by {RECEIVED_BY}, {us_date(DELIVERED_AT)} {clock(DELIVERED_AT)}",
            7,
        ),
        (
            LEFT + col + 24,
            "DRIVER SIGNATURE",
            DRIVER,
            f"Signed by {DRIVER}, {us_date(DELIVERED_AT)}",
            3,
        ),
    ):
        c.text(x, top, label, "F2", 7.2)
        signature(c, x, top + 40, col, seed)
        c.line(x, top + 42, x + col, top + 42, 0.6)
        c.text(x, top + 54, f"Print name: {signer}", "F1", 8.5)
        c.text(x, top + 66, line2, "F1", 8.5)

    top += 92
    c.rect(LEFT, top, RIGHT - LEFT, 62)
    c.text(LEFT + 6, top + 12, "DELIVERY CONDITIONS", "F2", 8)
    for n, (label, value) in enumerate(
        (
            ("Seal intact on arrival:", f"Yes, {SEAL}"),
            ("Unloaded by:", "Receiver"),
            ("Trailer temperature:", "Not applicable (dry van)"),
        )
    ):
        c.text(LEFT + 6, top + 28 + n * 12, label, "F1", 8.5)
        c.text(LEFT + 120, top + 28 + n * 12, value, "F1", 8.5)

    footer(c, f"{CARRIER}  |  Delivery receipt DR-2  |  Carrier copy")
    return c


# --- the BOL -------------------------------------------------------------------------------


def draw_bol() -> Any:
    c = pdf.Canvas()
    c.text(LEFT, 62, "STRAIGHT BILL OF LADING", "F2", 17)
    c.text(LEFT, 76, "Short form  |  Original  |  Not negotiable", "F1", 8.5)
    c.text_right(RIGHT, 58, f"Date: {us_date(PICKUP_DATE)}", "F1", 9.5)
    c.text_right(RIGHT, 74, f"Bill of lading number: {BOL_NUMBER}", "F2", 11)
    c.line(LEFT, 86, RIGHT, 86, 1.6)

    half = (RIGHT - LEFT - 8) / 2
    right_x = LEFT + half + 8

    def box(
        x: float, top: float, h: float, title: str, lines: list[tuple[str, str, float]]
    ) -> None:
        c.rect(x, top, half, h)
        c.rect(x, top, half, 14, 0.5, 0.88)
        c.text(x + 6, top + 10, title, "F2", 7.4)
        for n, (text, font, size) in enumerate(lines):
            c.text(x + 6, top + 30 + n * 13, text, font, size)

    box(
        LEFT,
        96,
        90,
        "SHIP FROM (SHIPPER)",
        [
            (SHIPPER, "F2", 10.5),
            (PICKUP_STOP, "F1", 9.2),
            (PICKUP_CITY, "F1", 9.2),
            ("Shipping office, dock 4", "F1", 9.2),
        ],
    )
    box(
        right_x,
        96,
        90,
        "CARRIER",
        [
            (f"Carrier name: {CARRIER}", "F2", 9.5),
            (f"MC number: {MC}", "F1", 9.2),
            (f"Trailer number: {TRAILER}", "F1", 9.2),
            (f"Seal number: {SEAL}", "F2", 9.5),
            (f"Load number: {LOAD_ID}", "F1", 9.2),
        ],
    )
    box(
        LEFT,
        194,
        78,
        "SHIP TO (CONSIGNEE)",
        [
            (DELIVERY_STOP, "F2", 10.5),
            (DELIVERY_CITY, "F1", 9.2),
            (
                f"Appointment: {us_date(DELIVERY_APPOINTMENT)} {clock(DELIVERY_APPOINTMENT)}",
                "F1",
                9.2,
            ),
        ],
    )
    box(
        right_x,
        194,
        78,
        "FREIGHT CHARGE TERMS",
        [("Prepaid", "F1", 9.2), ("Collect", "F1", 9.2), ("Third party", "F1", 9.2)],
    )
    for n in range(3):
        c.rect(right_x + half - 24, 216 + n * 13, 9, 9, 0.6)
    check_mark(c, right_x + half - 24, 216)

    c.rect(LEFT, 280, RIGHT - LEFT, 38)
    c.text(LEFT + 6, 291, "SPECIAL INSTRUCTIONS", "F2", 7.4)
    c.text(
        LEFT + 6,
        306,
        f"Deliver by appointment. PO {PO} required at the gate. Keep dry, do not double stack.",
        "F1",
        9.2,
    )

    c.text(LEFT, 334, "CUSTOMER ORDER INFORMATION", "F2", 8)
    top = table(
        c,
        340,
        [150, 70, 90, 80, 142],
        [
            ["CUSTOMER ORDER NUMBER", "PKGS", "WEIGHT (LB)", "PALLET/SLIP", "ADDITIONAL INFO"],
            [PO, f"{CASES:,}", f"{WEIGHT_LB:,}", "Y", "Canned goods"],
            ["GRAND TOTAL", f"{CASES:,}", f"{WEIGHT_LB:,}", "", ""],
        ],
        bold_last=True,
    )

    top += 20
    c.text(LEFT, top - 6, "CARRIER INFORMATION", "F2", 8)
    top = table(
        c,
        top,
        [84, 84, 72, 30, 182, 40, 40],
        [
            [
                "HANDLING UNIT",
                "PACKAGE",
                "WEIGHT (LB)",
                "H.M.",
                "COMMODITY DESCRIPTION",
                "NMFC",
                "CLASS",
            ],
            [f"{PALLETS} PLT", f"{CASES:,} CS", f"{WEIGHT_LB:,}", "", COMMODITY, "", "60"],
            [f"{PALLETS} PLT", f"{CASES:,} CS", f"{WEIGHT_LB:,}", "", "GRAND TOTAL", "", ""],
        ],
        bold_last=True,
    )

    top += 18
    legal = (
        "RECEIVED, subject to individually determined rates or contracts that have been agreed "
        "upon in writing between the carrier and shipper, if applicable, otherwise to the "
        "classifications and rules that have been established by the carrier and are available "
        "to the shipper on request. The property described above is in apparent good order, "
        "except as noted."
    )
    for line in pdf.wrap(legal, RIGHT - LEFT, "F1", 7.6):
        c.text(LEFT, top, line, "F1", 7.6)
        top += 9.6

    top += 14
    col = (RIGHT - LEFT - 32) / 3
    columns = (
        (
            "SHIPPER SIGNATURE / DATE",
            [f"Signed by {SHIPPER_SIGNER}, {SHIPPER}", us_date(PICKUP_DATE)],
            True,
        ),
        ("TRAILER LOADED / FREIGHT COUNTED", ["Loaded by shipper", "Counted by driver"], False),
        (
            "CARRIER SIGNATURE / PICKUP DATE",
            [f"Signed by {DRIVER}, driver", f"{CARRIER}, {us_date(PICKUP_DATE)}"],
            True,
        ),
    )
    for n, (label, lines, signed) in enumerate(columns):
        x = LEFT + n * (col + 16)
        c.rect(x, top, col, 92)
        c.text(x + 6, top + 11, label, "F2", 7.2)
        if signed:
            signature(c, x + 4, top + 50, col - 8, 11 + n)
            c.line(x + 6, top + 52, x + col - 6, top + 52, 0.6)
        else:
            for m, mark in enumerate(("By shipper", "By driver")):
                c.rect(x + 8, top + 26 + m * 14, 9, 9, 0.6)
                check_mark(c, x + 8, top + 26 + m * 14)
                c.text(x + 22, top + 34 + m * 14, mark, "F1", 8.5)
        for m, line in enumerate(lines):
            c.text(x + 6, top + 68 + m * 11, line, "F1", 8)

    footer(c, f"{SHIPPER}  |  {PICKUP_STOP}, {PICKUP_CITY}  |  Bill of lading {BOL_NUMBER}")
    return c


# --- the scan ------------------------------------------------------------------------------


def rotated(page: Any, degrees: float, dx: float, dy: float) -> Any:
    """`page` turned by `degrees` around its center and shifted, like a sheet on a scanner."""
    a = math.radians(degrees)
    cos, sin = math.cos(a), math.sin(a)
    cx, cy = pdf.PAGE_W / 2, pdf.PAGE_H / 2
    e = cx - cos * cx + sin * cy + dx
    f = cy - sin * cx - cos * cy + dy
    out = pdf.Canvas()
    out.ops = [f"q {cos:.6f} {sin:.6f} {-sin:.6f} {cos:.6f} {e:.3f} {f:.3f} cm", *page.ops, "Q"]
    return out


def scan(page: Any, dpi: int = 150) -> Any:
    """Render `page` with pypdfium2 and age it like a scan; returns an Image, or None."""
    try:
        import pypdfium2 as pdfium  # type: ignore[import-not-found]
    except ImportError:
        return None
    source = pdf.write_pdf([page], title="scan source", author="", created="D:20261014094500")
    document = pdfium.PdfDocument(source)
    bitmap = document[0].render(scale=dpi / 72, rev_byteorder=True)
    width, height, stride, channels = bitmap.width, bitmap.height, bitmap.stride, bitmap.n_channels
    raw = bytes(bitmap.buffer)
    rgb = bytearray(width * height * 3)
    for y in range(height):
        row = raw[y * stride : y * stride + width * channels]
        for ch in range(3):
            rgb[y * width * 3 + ch : (y + 1) * width * 3 : 3] = row[ch::channels]
    document.close()

    # Paper and toner: ink is never pure black, paper is slightly warm and gray.
    tables = [
        bytes(int(lo + v * (hi - lo) / 255) for v in range(256))
        for lo, hi in ((36, 244), (35, 241), (33, 232))
    ]
    toned = bytearray(len(rgb))
    for ch in range(3):
        toned[ch::3] = bytes(rgb[ch::3]).translate(tables[ch])

    # Dust specks and the shadow of the scanner lid along the left edge.
    rng = random.Random(int(PO))
    for _ in range(320):
        x, y = rng.randrange(width - 3), rng.randrange(height - 3)
        size = rng.choice((1, 1, 1, 2, 2, 3))
        shade = rng.randrange(70, 190)
        for yy in range(y, y + size):
            for xx in range(x, x + size):
                i = (yy * width + xx) * 3
                toned[i : i + 3] = bytes((shade, shade, shade - 4))
    for y in range(height):
        for x in range(14):
            i = (y * width + x) * 3
            factor = 0.55 + 0.45 * x / 14
            toned[i : i + 3] = bytes(int(v * factor) for v in toned[i : i + 3])
    return pdf.Image(width, height, bytes(toned))


# --- expected values -----------------------------------------------------------------------


def check(
    field: str, kind: str, expected: str | None, found: str | None, ok: bool
) -> dict[str, Any]:
    return {"field": field, "check": kind, "expected": expected, "found": found, "ok": ok}


def pod_expected(*, file: str, po: str, exceptions: str, scanned: bool) -> dict[str, Any]:
    po_ok = po == PO
    reasons = [] if po_ok else [f"PO {po} on the POD does not match the load's PO {PO}"]
    return {
        "file": file,
        "load_id": LOAD_ID,
        "type_id": "pod",
        "pages": 1,
        "text_layer": not scanned,
        "vision_path": scanned,
        "extracted": {
            "receiver": DELIVERY_STOP,
            "signed": True,
            "delivered_at": DELIVERED_AT,
            "exceptions": exceptions,
            "po_numbers": po,
        },
        "not_in_standard_type": ["po_numbers"],
        "also_on_document": {
            "received_by": RECEIVED_BY,
            "load_number": LOAD_ID,
            "bol_number": BOL_NUMBER,
            "carrier_name": CARRIER,
            "mc_number": MC,
            "driver": DRIVER,
            "shipper": SHIPPER,
            "trailer_number": TRAILER,
            "seal_number": SEAL,
            "seal_intact": True,
            "pieces": f"{CASES} cases",
            "weight": WEIGHT_LB,
        },
        "checks": [
            check("receiver", "stop_name", DELIVERY_STOP, DELIVERY_STOP, True),
            check("signed", "signature", "true", "true", True),
            check("delivered_at", "date_in_window", DELIVERY_APPOINTMENT, DELIVERED_AT, True),
            check("po_numbers", "po_number", PO, po, po_ok),
        ],
        "exceptions_noted": bool(exceptions),
        "status": "processed" if po_ok else "needs_review",
        "review_reasons": reasons,
    }


def bol_expected() -> dict[str, Any]:
    return {
        "file": "bol.pdf",
        "load_id": LOAD_ID,
        "type_id": "bol",
        "pages": 1,
        "text_layer": True,
        "vision_path": False,
        "extracted": {
            "bol_number": BOL_NUMBER,
            "shipper": SHIPPER,
            "consignee": DELIVERY_STOP,
            "po_numbers": PO,
            "seal_number": SEAL,
            "weight": WEIGHT_LB,
            "pickup_date": PICKUP_DATE,
            "signed": True,
        },
        "not_in_standard_type": [],
        "also_on_document": {
            "load_number": LOAD_ID,
            "carrier_name": CARRIER,
            "mc_number": MC,
            "trailer_number": TRAILER,
            "pickup_stop": PICKUP_STOP,
            "handling_units": f"{PALLETS} pallets",
            "pieces": f"{CASES} cases",
            "commodity": COMMODITY,
            "freight_class": "60",
            "delivery_appointment": DELIVERY_APPOINTMENT,
        },
        "checks": [
            check("shipper", "stop_name", SHIPPER, SHIPPER, True),
            check("consignee", "stop_name", DELIVERY_STOP, DELIVERY_STOP, True),
            check("po_numbers", "po_number", PO, PO, True),
            check("seal_number", "seal_number", None, SEAL, True),
            check("pickup_date", "date_in_window", "/".join(PICKUP_WINDOW), PICKUP_DATE, True),
            check("signed", "signature", "true", "true", True),
        ],
        "exceptions_noted": False,
        "status": "processed",
        "review_reasons": [],
    }


# --- main ----------------------------------------------------------------------------------


def write(name: str, data: bytes) -> None:
    (HERE / name).write_bytes(data)
    print(f"wrote {name}: {len(data):,} bytes")


def write_json(name: str, value: dict[str, Any]) -> None:
    (HERE / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {name}")


def main() -> None:
    delivered = "D:20261014094500-07'00'"
    picked_up = "D:20261013101500-07'00'"
    clean = draw_pod(po=PO, exceptions="")
    write(
        "pod_clean.pdf",
        pdf.write_pdf([clean], title="Proof of delivery", author=CARRIER, created=delivered),
    )
    write(
        "pod_exceptions.pdf",
        pdf.write_pdf(
            [draw_pod(po=PO_TYPO, exceptions=EXCEPTIONS)],
            title="Proof of delivery",
            author=CARRIER,
            created=delivered,
        ),
    )
    write(
        "bol.pdf",
        pdf.write_pdf([draw_bol()], title="Bill of lading", author=SHIPPER, created=picked_up),
    )

    image = scan(rotated(clean, 0.45, 3.0, -4.0))
    if image is None:
        print("skipped pod_scanned.pdf: run with `uv run --with pypdfium2` to render the scan")
    else:
        page = pdf.Canvas()
        page.image("Im0", image, 0, 0, pdf.PAGE_W, pdf.PAGE_H)
        write(
            "pod_scanned.pdf",
            pdf.write_pdf([page], title="Scan", author="", created=delivered, fonts=False),
        )

    write_json(
        "pod_clean.expected.json",
        pod_expected(file="pod_clean.pdf", po=PO, exceptions="", scanned=False),
    )
    write_json(
        "pod_exceptions.expected.json",
        pod_expected(file="pod_exceptions.pdf", po=PO_TYPO, exceptions=EXCEPTIONS, scanned=False),
    )
    write_json(
        "pod_scanned.expected.json",
        pod_expected(file="pod_scanned.pdf", po=PO, exceptions="", scanned=True),
    )
    write_json("bol.expected.json", bol_expected())


if __name__ == "__main__":
    main()
