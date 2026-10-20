# Document fixtures for load 481207

Paperwork for the load in `../load_481207.json`: PO 55821, carrier Prairie Haul LLC
(MC 778120), shipper Riverbend Foods, pickup at the Riverbend plant in Stockton, CA
(window 2026-10-13 06:00 to 14:00), delivery at Valley DC in Reno, NV (fixed appointment
2026-10-14 09:30), canned tomato sauce, 38,000 lb. Used by the document pipeline (card
DOC), the `docs` eval suite (card EVK) and the end-to-end demo (plan steps 7 and 8).

| File | Type | Pages | Text layer | Expected status | What it tests |
|---|---|---|---|---|---|
| `pod_clean.pdf` | `pod` | 1 | yes | `processed` | The clean POD: signed by the receiver, delivered 10/14/2026 09:42, PO 55821, no exceptions |
| `pod_exceptions.pdf` | `pod` | 1 | yes | `needs_review` | The same POD with exceptions ("2 cases damaged, 1 short", Shortage and Damage ticked) and a PO typo (55812) |
| `pod_scanned.pdf` | `pod` | 1 | **no** | `processed` | The clean POD as one page image (a slightly rotated, toned 150 dpi scan). A text extractor finds no text, so the pipeline must take the vision path (`LlmRequest.images`) |
| `bol.pdf` | `bol` | 1 | yes | `processed` | The bill of lading signed at pickup on 10/13/2026: BOL RBF-30417, seal SL-44120, 38,000 lb |

Each PDF has a `<name>.expected.json` next to it, and the generator writes both from the
same constants.

## Rebuild

```
uv run --with pypdfium2 python evals/fixtures/docs/make_fixtures.py
```

- `pdfmini.py` is a small PDF writer with no dependency: standard Helvetica fonts
  (WinAnsi), plain uncompressed content streams, FlateDecode RGB images and a correct
  xref table. `../sops/make_fixtures.py` uses it too.
- pypdfium2 is needed only to render the clean POD to pixels for `pod_scanned.pdf`. It
  comes from `uv run --with pypdfium2` and is **not** added to the project. Without it,
  the script writes the three text PDFs and the four JSON files, and says it skipped the
  scan. Pillow is not used.
- The output is deterministic: a rebuild gives the same bytes. The scan was rendered with
  pypdfium2 5.14.0 (PDFium 156.0.8076.0); another PDFium version can shift a few pixels.

Checked when the fixtures were made: every xref offset points at its object, pypdf reads
each file in strict mode, and pypdfium2 extracts 1,235 characters from `pod_clean.pdf`,
1,255 from `pod_exceptions.pdf`, 1,614 from `bol.pdf` and **0** from `pod_scanned.pdf`.

## Field names

The names are those of the standard document types in C0's `standard_broker()`
(`src/freight_agent/domain/broker.py`, from `docs/answers.md` 1.2):

| Type | Field | Kind | Check |
|---|---|---|---|
| `bol` | `bol_number` | text | none |
| | `shipper` | text | `stop_name` |
| | `consignee` | text | `stop_name` |
| | `po_numbers` | text | `po_number` |
| | `seal_number` | text | `seal_number` |
| | `weight` | number | none |
| | `pickup_date` | date | `date_in_window` |
| | `signed` | bool | `signature` |
| `pod` | `receiver` | text | `stop_name` |
| | `signed` | bool | `signature` |
| | `delivered_at` | datetime | `date_in_window` |
| | `exceptions` | text (empty when none) | none |
| | `po_numbers` | text | `po_number`: **not in C0's `pod` type**, see below |

## The expected files

| Key | Meaning |
|---|---|
| `type_id` | The type a correct classification returns |
| `text_layer`, `vision_path` | Whether the page has text, and whether the pipeline must send the page image to the model |
| `extracted` | The values a correct extraction returns, in the field's kind: dates as `YYYY-MM-DD`, datetimes as local `YYYY-MM-DDTHH:MM` (the stops are in America/Los_Angeles), numbers without separators, `exceptions` as `""` when the POD says NONE |
| `not_in_standard_type` | Keys of `extracted` that the standard type does not ask for yet. A grader that follows the broker's `DocumentTypeDef` skips them |
| `also_on_document` | Other values printed on the page (receiver's printed name, load and trailer numbers...), for graders that read more fields. Not required |
| `checks` | One entry per checked field: `field`, `check` (the `FieldDef.check`), `expected` (from the load), `found` (from the document), `ok`. Compare `field` and `ok`; `expected` and `found` are there for a person reading the result |
| `exceptions_noted` | The POD lists an exception |
| `status`, `review_reasons` | The document status after the checks, and why it needs review |

## Expected values

| Field | `pod_clean` / `pod_scanned` | `pod_exceptions` |
|---|---|---|
| `receiver` | `Valley DC` (ok) | `Valley DC` (ok) |
| `signed` | `true` (ok) | `true` (ok) |
| `delivered_at` | `2026-10-14T09:42` (ok) | `2026-10-14T09:42` (ok) |
| `exceptions` | `""` | `2 cases damaged, 1 short` |
| `po_numbers` | `55821` (ok) | `55812` (**fails**: the load's PO is 55821) |
| status | `processed` | `needs_review` |

| Field (`bol`) | Value | Check |
|---|---|---|
| `bol_number` | `RBF-30417` | |
| `shipper` | `Riverbend Foods` | ok: the load's shipper (the next line on the BOL is the pickup stop, Riverbend plant) |
| `consignee` | `Valley DC` | ok: the delivery stop |
| `po_numbers` | `55821` | ok |
| `seal_number` | `SL-44120` | ok: the load reports no seal, so nothing contradicts it |
| `weight` | `38000` | |
| `pickup_date` | `2026-10-13` | ok: inside the pickup window |
| `signed` | `true` | ok: shipper and carrier signatures |
| status | `processed` | |

## The check rules these values assume

- `stop_name`: the value matches, ignoring case, the load's `shipper` or the name of one of
  its stops. "Riverbend Foods" matches only the shipper, so a check that reads only the
  stop names would fail the BOL.
- `po_number`: the value carries the PO of the load instructions ("PO 55821 required at
  gate").
- `date_in_window`: the date falls inside the stop's appointment. For a window, between
  its start and end; for a fixed appointment, on its day. The POD's 09:42 is on the day of
  the 09:30 appointment, so it passes. Being 12 minutes late is not a document error, and
  Acme's SOP (section 3) also calls a POD clean when it is "dated on the delivery
  appointment day".
- `seal_number`: the value equals the seal the driver reported, when one was reported.
  With none reported it passes and the value is kept. A scenario that reports a different
  seal makes `bol.pdf` fail this check.
- `signature`: `signed` is true. The text layer says so in words under each signature
  line ("Signed by Dana Whitfield, 10/14/2026 09:42"), and the drawn signature is there
  for the vision path.

## Open points for the owning cards

- **The `pod` type has no PO field** in C0's `standard_broker()`. Without
  `FieldDef(name="po_numbers", description="The PO numbers listed", check="po_number")` on
  `pod`, the PO typo in `pod_exceptions.pdf` goes unchecked. The pipeline would then
  return `processed`, unless it also treats a non-empty `exceptions` as a reason for
  review. That field is why `not_in_standard_type` lists `po_numbers`.
- **`receiver` is ambiguous.** The POD prints the facility ("Receiver (consignee): Valley
  DC") and the person who signed ("Print name: Dana Whitfield"). The `stop_name` check
  needs the facility, so the field description should say "the receiving facility
  (consignee)", not "the receiver's name".
- **No check covers exceptions.** `exceptions` has `check: none`. The status of
  `pod_exceptions.pdf` comes from the PO check. The exceptions are acted on by the
  `document_processed` run (Acme SOP section 3, step 6: a Hero task with the values).
