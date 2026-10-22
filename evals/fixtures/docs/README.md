# Document fixtures for load 481207

Paperwork for the load in `../load_481207.json`: PO 55821, carrier Prairie Haul LLC
(MC 778120), shipper Riverbend Foods, pickup at the Riverbend plant in Stockton, CA
(window 2026-10-13 06:00 to 14:00), delivery at Valley DC in Reno, NV (fixed appointment
2026-10-14 09:30), canned tomato sauce, 38,000 lb. Used by the document pipeline's tests,
the `docs` eval suite. In the live workspace, use the scenario's sample-document
input button. The clean and exception PODs are generated for the live load ID and
receipt time by `orchestration/live_inputs.py`; the original fixtures stay unchanged.
The other attachments retain their fixture bytes.

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
  xref table.
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

The names below reflect the current standard document types in `standard_broker()`
(`src/freight_agent/domain/broker.py`). A workspace run uses the document catalog
frozen in that load's SOP snapshot:

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
| `pod` | `receiver` | text | `stop_name`; receiving facility, not the signer's name |
| | `signed` | bool | `signature` |
| | `delivered_at` | datetime | `date_in_window` |
| | `exceptions` | text (empty when none) | Non-empty exceptions produce a domain review reason |
| | `po_numbers` | text | `po_number` |

## The expected files

| Key | Meaning |
|---|---|
| `type_id` | The type a correct classification returns |
| `text_layer`, `vision_path` | Whether the page has text, and whether the pipeline must send the page image to the model |
| `extracted` | The values a correct extraction returns, in the field's kind: dates as `YYYY-MM-DD`, datetimes as local `YYYY-MM-DDTHH:MM` (the stops are in America/Los_Angeles), numbers without separators, `exceptions` as `""` when the POD says NONE |
| `not_in_standard_type` | Historical fixture annotation, not the current catalog contract. The POD metadata/generator still list `po_numbers` here, although it is now a standard checked field; do not use this list to skip the runtime's PO check |
| `also_on_document` | Other values printed on the page (receiver's printed name, load and trailer numbers...), for graders that read more fields. Not required |
| `checks` | One entry per checked field: `field`, `check` (the `FieldDef.check`), `expected` (from the load), `found` (from the document), `ok`. Compare `field` and `ok`; `expected` and `found` are there for a person reading the result |
| `exceptions_noted` | The POD lists an exception |
| `status`, `review_reasons` | Fixture expectations written by the generator. Current runtime validation also records a reason for non-empty exceptions; this reason is not enumerated in the legacy generator's list |

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

## Current runtime checks and legacy metadata

The previously noted catalog gaps are implemented:

- The standard POD type includes `po_numbers` with `check="po_number"`; the typo
  55812 fails against the load's PO 55821.
- The `receiver` field explicitly asks for the receiving facility (consignee), not
  the person who signed. Valley DC is the expected facility; Dana Whitfield is the
  signer printed on the page.
- `domain/documents.py::read_extraction` adds a review reason for a non-empty
  exception value even though that field has no comparison-style `FieldCheck`.
  Damage/shortage can therefore require review even when the PO matches.

The fixture generator and its JSON annotations retain the earlier
`not_in_standard_type` marker and PO-only `review_reasons` list. Those annotations
describe the fixture's original expectations; the current broker catalog and domain
validator determine runtime behavior. The `document_processed` SOP then decides how
to handle the checked document, including the Acme Hero escalation.
