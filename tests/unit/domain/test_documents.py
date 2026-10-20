"""Reading an extraction: values in their kinds, checks against the load, the status.

Owner: card W3-DOC. The fixtures' `*.expected.json` files (evals/fixtures/docs) give the
values a correct extraction returns and the checks it must pass or fail.
"""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from freight_agent.domain.broker import DocumentTypeDef, FieldDef, standard_broker
from freight_agent.domain.documents import (
    Document,
    DocumentStatus,
    ExtractedValue,
    Extraction,
    FieldCheck,
    Reading,
    load_po_numbers,
    parse_when,
    read_extraction,
    reported_seal,
)
from freight_agent.domain.events import Direction, Event, EventType
from freight_agent.domain.load import Channel, Load

pytestmark = pytest.mark.unit

DOCS = Path(__file__).resolve().parents[3] / "evals" / "fixtures" / "docs"
LA = ZoneInfo("America/Los_Angeles")
TYPES = standard_broker().document_types


def expected(name: str) -> dict[str, Any]:
    return json.loads((DOCS / f"{name}.expected.json").read_text())


def extraction_of(name: str, *, confidence: float = 0.95) -> Extraction:
    """What a correct model answers for the fixture: its type and every value, as text."""
    spec = expected(name)
    return Extraction(
        type_id=spec["type_id"],
        confidence=confidence,
        fields=tuple(ExtractedValue(name=k, value=v) for k, v in spec["extracted"].items()),
    )


def with_values(base: Extraction, **values: str | None) -> Extraction:
    kept = [f for f in base.fields if f.name not in values]
    return base.model_copy(
        update={"fields": (*kept, *(ExtractedValue(name=k, value=v) for k, v in values.items()))}
    )


def checks_of(reading: Reading) -> dict[str, dict[str, Any]]:
    """By field: the fixtures list the checks in their own order, the reading in the type's."""
    return {c.field: c.model_dump() for c in reading.checks}


def expected_checks(name: str) -> dict[str, dict[str, Any]]:
    return {c["field"]: c for c in expected(name)["checks"]}


def a_type(*fields: FieldDef) -> DocumentTypeDef:
    return DocumentTypeDef(type_id="slip", title="Slip", description="A test slip.", fields=fields)


def message(body: str, event_id: str = "m1") -> Event:
    return Event(
        event_id=event_id,
        load_id="481207",
        type=EventType.INBOUND_MESSAGE,
        occurred_at=datetime(2026, 10, 13, 9, 0, tzinfo=LA),
        channel=Channel.SMS,
        sender_contact_id="drv_20931",
        body=body,
    )


# --- the fixtures -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["pod_clean", "pod_scanned", "bol"])
def test_a_clean_fixture_is_processed_with_the_expected_values_and_checks(
    name: str, load: Load
) -> None:
    spec = expected(name)

    reading = read_extraction(extraction_of(name), TYPES, load)

    assert reading.status is DocumentStatus.PROCESSED
    assert reading.reasons == ()
    assert reading.type_id == spec["type_id"]
    assert reading.extracted == spec["extracted"]
    assert checks_of(reading) == expected_checks(spec["file"].removesuffix(".pdf"))


def test_the_pod_with_exceptions_and_the_wrong_po_needs_review(load: Load) -> None:
    spec = expected("pod_exceptions")

    reading = read_extraction(extraction_of("pod_exceptions"), TYPES, load)

    assert reading.status is DocumentStatus.NEEDS_REVIEW
    assert reading.extracted == spec["extracted"]
    assert checks_of(reading) == expected_checks(spec["file"].removesuffix(".pdf"))
    assert reading.reasons == (
        "PO 55812 on the document does not match the load's PO 55821",
        "the document notes exceptions: 2 cases damaged, 1 short",
    )
    assert reading.summary() == (
        "pod: needs_review: PO 55812 on the document does not match the load's PO 55821; "
        "the document notes exceptions: 2 cases damaged, 1 short"
    )


def test_only_the_chosen_types_fields_are_kept_in_its_order(load: Load) -> None:
    answer = with_values(extraction_of("pod_clean"), weight="38000", seal_number="SL-44120")

    reading = read_extraction(answer, TYPES, load)

    assert list(reading.extracted) == [
        "receiver",
        "po_numbers",
        "signed",
        "delivered_at",
        "exceptions",
    ]


# --- the reasons for review -------------------------------------------------------------------


def test_an_uncertain_reading_needs_review_even_when_every_check_passes(load: Load) -> None:
    reading = read_extraction(extraction_of("pod_clean", confidence=0.4), TYPES, load)

    assert reading.status is DocumentStatus.NEEDS_REVIEW
    assert all(c.ok for c in reading.checks)
    assert reading.reasons == ("the reading is uncertain (confidence 0.40)",)


def test_the_confidence_is_kept_between_zero_and_one(load: Load) -> None:
    assert read_extraction(extraction_of("pod_clean", confidence=7), TYPES, load).confidence == 1
    assert read_extraction(extraction_of("pod_clean", confidence=-1), TYPES, load).confidence == 0


@pytest.mark.parametrize(
    ("type_id", "reason"),
    [
        (None, "the document is none of the broker's document types"),
        ("invoice", "the type 'invoice' is not one of the broker's document types"),
    ],
)
def test_a_type_the_broker_does_not_have_needs_review_with_nothing_extracted(
    type_id: str | None, reason: str, load: Load
) -> None:
    answer = extraction_of("pod_clean").model_copy(update={"type_id": type_id})

    reading = read_extraction(answer, TYPES, load)

    assert (reading.status, reading.type_id, reading.extracted) == (
        DocumentStatus.NEEDS_REVIEW,
        None,
        {},
    )
    assert reading.reasons == (reason,)


def test_a_missing_value_needs_review_and_fails_its_check(load: Load) -> None:
    answer = with_values(extraction_of("pod_clean"), receiver=None)

    reading = read_extraction(answer, TYPES, load)

    assert reading.status is DocumentStatus.NEEDS_REVIEW
    assert reading.extracted["receiver"] is None
    assert reading.checks[0] == FieldCheck(
        field="receiver",
        expected="Riverbend Foods | Riverbend plant | Valley DC",
        found=None,
        ok=False,
        check="stop_name",
    )
    assert reading.reasons == ("receiver is missing",)


def test_an_empty_text_is_missing_but_an_unchecked_field_has_no_check(load: Load) -> None:
    answer = with_values(extraction_of("bol"), bol_number="  ")

    reading = read_extraction(answer, TYPES, load)

    assert reading.reasons == ("bol_number is missing",)
    assert "bol_number" not in {c.field for c in reading.checks}


@pytest.mark.parametrize("nothing", [None, "", "NONE", "n/a", "No exceptions"])
def test_an_exception_field_with_nothing_noted_is_empty_and_clean(
    nothing: str | None, load: Load
) -> None:
    answer = with_values(extraction_of("pod_clean"), exceptions=nothing)

    reading = read_extraction(answer, TYPES, load)

    assert reading.status is DocumentStatus.PROCESSED
    assert reading.extracted["exceptions"] == ""


def test_a_value_that_is_not_of_its_kind_is_kept_as_text_and_needs_review(load: Load) -> None:
    answer = with_values(extraction_of("bol"), weight="heavy", pickup_date="last Tuesday")

    reading = read_extraction(answer, TYPES, load)

    assert reading.extracted["weight"] == "heavy"
    assert reading.extracted["pickup_date"] == "last Tuesday"
    assert reading.reasons == (
        "weight 'heavy' is not a number",
        "pickup_date 'last Tuesday' is not a date",
    )
    pickup = next(c for c in reading.checks if c.field == "pickup_date")
    assert (pickup.ok, pickup.found, pickup.expected) == (
        False,
        "last Tuesday",
        "2026-10-13T06:00/2026-10-13T14:00",
    )


def test_the_first_value_given_for_a_field_wins(load: Load) -> None:
    answer = with_values(extraction_of("pod_clean"))
    answer = answer.model_copy(
        update={
            "fields": (
                ExtractedValue(name="receiver", value=None),
                ExtractedValue(name=" receiver ", value="Valley DC"),
                ExtractedValue(name="receiver", value="Somewhere else"),
                *(f for f in answer.fields if f.name != "receiver"),
            )
        }
    )

    assert read_extraction(answer, TYPES, load).extracted["receiver"] == "Valley DC"


# --- values -----------------------------------------------------------------------------------


def test_a_scripted_value_may_be_a_bool_or_a_number() -> None:
    assert ExtractedValue(name="signed", value=True).value == "true"  # pyright: ignore[reportArgumentType]
    assert ExtractedValue(name="signed", value=False).value == "false"  # pyright: ignore[reportArgumentType]
    assert ExtractedValue(name="weight", value=38000).value == "38000"  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("kind", "raw", "stored"),
    [
        ("number", "38,000 lb", 38000),
        ("number", "$1,250.50", 1250.5),
        ("bool", "Yes", True),
        ("bool", "not signed", False),
        ("date", "10/13/2026", "2026-10-13"),
        ("date", "10/13/26", "2026-10-13"),
        ("date", "2026-10-13T08:15", "2026-10-13"),
        ("datetime", "10/14/2026 09:42", "2026-10-14T09:42"),
        ("datetime", "10/14/2026 9:42 PM", "2026-10-14T21:42"),
        ("datetime", "10/14/2026 12:05 AM", "2026-10-14T00:05"),
        ("datetime", "2026-10-14 09:42:31", "2026-10-14T09:42"),
        ("datetime", "2026-10-14T09:42-07:00", "2026-10-14T09:42-07:00"),
        ("datetime", "2026-10-14", "2026-10-14"),
        ("text", "  Valley DC ", "Valley DC"),
    ],
)
def test_a_value_is_stored_in_its_fields_kind(
    kind: Any, raw: str, stored: object, load: Load
) -> None:
    slip = a_type(FieldDef(name="value", description="A value", kind=kind))
    answer = Extraction(
        type_id="slip", confidence=1, fields=(ExtractedValue(name="value", value=raw),)
    )

    reading = read_extraction(answer, (slip,), load)

    assert reading.extracted == {"value": stored}
    assert reading.status is DocumentStatus.PROCESSED


@pytest.mark.parametrize("text", ["13/45/2026", "2/30/2026 10:00", "soon", "2026-13-01"])
def test_an_impossible_date_is_not_a_date(text: str) -> None:
    assert parse_when(text) is None


def test_dates_are_read_as_iso_or_us_forms() -> None:
    assert parse_when("2026-10-14") == date(2026, 10, 14)
    assert parse_when("2026-10-14T09:42") == datetime(2026, 10, 14, 9, 42)
    assert parse_when("10/14/2026") == date(2026, 10, 14)


# --- checks -----------------------------------------------------------------------------------


def check(load: Load, field: FieldDef, raw: str | None, *, seal: str | None = None) -> FieldCheck:
    answer = Extraction(
        type_id="slip", confidence=1, fields=(ExtractedValue(name=field.name, value=raw),)
    )
    (result,) = read_extraction(answer, (a_type(field),), load, reported_seal=seal).checks
    return result


PO = FieldDef(name="po", description="PO", check="po_number")
MC = FieldDef(name="mc", description="MC", check="mc_number")
STOP = FieldDef(name="stop", description="A stop", check="stop_name")
SEAL = FieldDef(name="seal", description="Seal", check="seal_number")
SIGNED = FieldDef(name="signed", description="Signed", kind="bool", check="signature")
SIGNED_TEXT = FieldDef(name="signed", description="Signed", check="signature")
DELIVERED = FieldDef(
    name="delivered_at", description="When", kind="datetime", check="date_in_window"
)
PICKED_UP = FieldDef(name="pickup_date", description="When", kind="date", check="date_in_window")
DATED = FieldDef(name="date", description="When", kind="date", check="date_in_window")


def test_the_po_check_wants_every_po_of_the_load_on_the_document(load: Load) -> None:
    assert check(load, PO, "PO# 55821, 55830").ok
    assert check(load, PO, "55821").expected == "55821"
    assert not check(load, PO, "55812").ok
    two_pos = load.model_copy(update={"instructions": "PO 55821 and P.O. #55830 at the gate"})
    assert load_po_numbers(two_pos) == ("55821", "55830")
    assert not check(two_pos, PO, "55821").ok
    assert check(two_pos, PO, "55830 / 55821").expected == "55821, 55830"


def test_a_load_without_a_po_passes_the_po_check(load: Load) -> None:
    no_po = load.model_copy(update={"instructions": "Call 1 hr before arrival. POD needed."})

    assert load_po_numbers(no_po) == ()
    assert check(no_po, PO, "55812") == FieldCheck(
        field="po", expected=None, found="55812", ok=True, check="po_number"
    )


def test_the_mc_check_finds_the_carriers_number_among_the_numbers(load: Load) -> None:
    assert check(load, MC, "MC-778120").ok
    assert check(load, MC, "MC 0778120 / USDOT 3318842").ok
    result = check(load, MC, "MC 778121")
    assert (result.ok, result.expected) == (False, "778120")


def test_the_stop_check_matches_the_shipper_or_any_stop_name(load: Load) -> None:
    assert check(load, STOP, "RIVERBEND FOODS").expected == "Riverbend Foods"
    assert check(load, STOP, "Valley DC, Reno NV").expected == "Valley DC"
    assert check(load, STOP, "riverbend-plant").ok
    result = check(load, STOP, "Valley")
    assert (result.ok, result.expected) == (False, "Riverbend Foods | Riverbend plant | Valley DC")


def test_a_delivery_date_is_checked_against_the_delivery_appointment(load: Load) -> None:
    assert check(load, DELIVERED, "2026-10-14T23:59").ok  # the appointment's day
    assert check(load, DELIVERED, "2026-10-14T16:42Z").ok  # 09:42 in Reno
    result = check(load, DELIVERED, "2026-10-13T09:42")  # the pickup day is not the delivery
    assert (result.ok, result.expected) == (False, "2026-10-14T09:30")


def test_a_pickup_date_is_checked_against_the_pickup_window(load: Load) -> None:
    assert check(load, PICKED_UP, "10/13/2026").ok
    assert not check(load, PICKED_UP, "2026-10-14").ok


def test_a_date_with_no_stop_in_its_name_may_fall_on_any_stop(load: Load) -> None:
    assert check(load, DATED, "2026-10-13").ok
    assert check(load, DATED, "2026-10-14").ok
    result = check(load, DATED, "2026-10-15")
    assert result.expected == "2026-10-13T06:00/2026-10-13T14:00 | 2026-10-14T09:30"


def test_a_time_in_a_window_must_be_inside_it(load: Load) -> None:
    picked = FieldDef(
        name="picked_up_at", description="When", kind="datetime", check="date_in_window"
    )

    assert check(load, picked, "2026-10-13T13:59").ok
    assert not check(load, picked, "2026-10-13T14:01").ok


def test_a_date_check_on_a_text_field_reads_the_text_as_a_date(load: Load) -> None:
    text_date = FieldDef(name="delivery_date", description="When", check="date_in_window")

    assert check(load, text_date, "10/14/2026").ok
    assert not check(load, text_date, "tomorrow").ok


def test_a_load_without_appointments_fails_a_date_check(load: Load) -> None:
    stop = load.stops[1]
    blank = stop.model_copy(
        update={"appointment": stop.appointment.model_copy(update={"at": None})}
    )
    no_appointment = load.model_copy(update={"stops": (blank,)})

    result = check(no_appointment, DELIVERED, "2026-10-14T09:42")

    assert (result.ok, result.expected) == (False, None)


def test_a_window_without_its_end_matches_nothing(load: Load) -> None:
    stop = load.stops[0]
    open_window = stop.model_copy(
        update={"appointment": stop.appointment.model_copy(update={"end": None})}
    )
    no_end = load.model_copy(update={"stops": (open_window,)})

    assert check(no_end, PICKED_UP, "2026-10-13").expected is None


def test_the_seal_check_compares_with_the_reported_seal(load: Load) -> None:
    assert check(load, SEAL, "SL-44120").ok  # nothing reported: nothing contradicts it
    assert check(load, SEAL, "sl 44120", seal="SL-44120").ok
    result = check(load, SEAL, "SL-44121", seal="SL-44120")
    assert (result.ok, result.expected) == (False, "SL-44120")


def test_a_reported_seal_is_the_last_one_a_message_of_the_load_names() -> None:
    events = [
        message("seal is intact, all good", "m1"),
        message("Seal #: SL-44119", "m2"),
        message("sorry, seal number is SL-44120", "m3"),
        message("leaving now", "m4"),
        Event(
            event_id="act:1",
            load_id="481207",
            type=EventType.AGENT_ACTION,
            occurred_at=datetime(2026, 10, 13, 9, 1, tzinfo=LA),
            channel=Channel.SMS,
            direction=Direction.OUTBOUND,
            body="What is the seal number? SL-00000?",
        ),
    ]

    assert reported_seal(events) == "SL-44120"
    assert reported_seal(events[:1]) is None


def test_the_signature_check_wants_a_signature(load: Load) -> None:
    assert check(load, SIGNED, "yes").ok
    result = check(load, SIGNED, "false")
    assert (result.ok, result.expected, result.found) == (False, "true", "false")
    assert check(load, SIGNED_TEXT, "Signed").ok
    assert not check(load, SIGNED_TEXT, "illegible scrawl").ok


@pytest.mark.parametrize(
    ("field", "raw", "seal", "reason"),
    [
        (MC, "MC 1", None, "MC MC 1 on the document is not the carrier's MC 778120"),
        (STOP, "Elsewhere", None, "stop 'Elsewhere' is neither the shipper nor a stop of the load"),
        (
            DELIVERED,
            "2026-10-15T08:00",
            None,
            "delivered_at 2026-10-15T08:00 is outside the appointment (2026-10-14T09:30)",
        ),
        (SEAL, "SL-1", "SL-2", "seal SL-1 on the document is not the seal reported (SL-2)"),
        (SIGNED, "no", None, "signed: the document is not signed"),
    ],
)
def test_a_failed_check_says_why(
    field: FieldDef, raw: str, seal: str | None, reason: str, load: Load
) -> None:
    answer = Extraction(
        type_id="slip", confidence=1, fields=(ExtractedValue(name=field.name, value=raw),)
    )

    (why,) = read_extraction(answer, (a_type(field),), load, reported_seal=seal).reasons

    assert why.startswith(reason)


@pytest.mark.parametrize(
    ("field", "expected_value"),
    [
        (PO, "55821"),
        (MC, "778120"),
        (SEAL, "SL-44120"),
        (SIGNED, "true"),
        (DELIVERED, "2026-10-14T09:30"),
    ],
)
def test_a_missing_value_fails_its_check_with_what_was_expected(
    field: FieldDef, expected_value: str, load: Load
) -> None:
    result = check(load, field, None, seal="SL-44120")

    assert (result.ok, result.found, result.expected) == (False, None, expected_value)


# --- the document -----------------------------------------------------------------------------


def test_a_document_read_carries_the_reading(load: Load) -> None:
    pending = Document.pending(
        document_id="doc_1",
        load_id="481207",
        attachment_id="a1",
        broker_id="northline",
        blob_ref="sha256:abc",
        filename="pod.pdf",
        content_type="application/pdf",
        received_at=datetime(2026, 10, 14, 9, 45, tzinfo=LA),
    )
    reading = read_extraction(extraction_of("pod_clean"), TYPES, load)
    at = datetime(2026, 10, 14, 9, 46, tzinfo=LA)

    document = pending.read(reading, text="PROOF OF DELIVERY", pages=1, at=at)

    assert document.clean
    assert (document.type_id, document.confidence, document.checks) == ("pod", 0.95, reading.checks)
    assert document.extracted == reading.extracted
    assert (document.text, document.pages, document.processed_at) == ("PROOF OF DELIVERY", 1, at)
    failed = pending.read(Reading.failed("the file is missing"), text="", pages=0, at=at)
    assert (failed.status, failed.clean) == (DocumentStatus.FAILED, False)
    assert (
        Reading.failed("the file is missing").summary()
        == "unknown type: failed: the file is missing"
    )
