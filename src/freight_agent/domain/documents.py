"""Documents: attachments the pipeline reads, classifies and checks against the load.

Owner: card W3-DOC.

A message's attachment is saved as a `pending` document in the unit of work that logs
the message (`AgentRuntime.ingest_message`). The document pipeline
(`application.documents`, `ports.documents.DocumentProcessor`) reads its pages, asks the
`extract` model for the type and the values of the broker's `DocumentTypeDef`
(`Extraction`, the structured output), and this module turns that answer into a
`Reading`: each value in its field's kind, each value checked against the load in code
(`FieldDef.check`), and the document's status:

- `processed`: a known type, every value present and readable, every check ok, the
  model sure of itself (`MIN_CONFIDENCE`) and no exception noted (a field named in
  `EXCEPTION_FIELDS`, such as a POD's `exceptions`, that is not empty);
- `needs_review`: anything else that was read; `Reading.reasons` says why;
- `failed`: the file or the model could not be read (`Reading.failed`).

The pipeline then logs one `document_processed` event (`doc:{document_id}`), and that
event's run acts on the document; the message's own run never does.

The checks compare what the document says with what the load says, never with what the
model was told: the prompt carries no value of the load, so a model cannot "correct" a
wrong PO into the right one. The rules (`docs/decisions.md`, Documents, and
`evals/fixtures/docs/README.md`):

- `po_number`: every PO of the load's instructions ("PO 55821 required at gate") is on
  the document; a load without a PO passes.
- `mc_number`: the carrier's MC number is one of the numbers in the value.
- `stop_name`: the value names the shipper or one of the stops (case, punctuation and
  extra words ignored: "Valley DC, Reno" names "Valley DC").
- `date_in_window`: the value falls in the appointment of one of the stops the field is
  about: a field named with `deliver` is about the deliveries, with `pickup` or `ship`
  about the pickups, otherwise about every stop. A fixed appointment: the same day (late
  is not a document error); a window: inside it (a date: on one of its days).
- `seal_number`: the same seal as the last one a message of the load reported
  (`reported_seal`); with none reported it passes and the value is kept.
- `signature`: the value is true.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from freight_agent.domain.base import DomainModel
from freight_agent.domain.broker import DocumentTypeDef, FieldCheckKind, FieldDef, FieldKind
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.load import AppointmentKind, Load, Stop, StopType

# Below this, the reading goes to a person even when every check passes.
MIN_CONFIDENCE = 0.7
# Text fields where a value means trouble (a shortage, damage, a refusal), whatever the
# checks say. "None", "n/a" and the like count as empty and are stored as "".
EXCEPTION_FIELDS = frozenset({"exceptions"})
_NO_EXCEPTION = frozenset({"", "none", "no", "nil", "n/a", "na", "-", "no exceptions"})


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class Page(DomainModel):
    """One page as the page reader returns it: its text, and an image when the text is
    too thin to trust (a scan or a photo), for the vision path."""

    number: int  # 1-based
    text: str = ""
    image_png: bytes | None = None


class FieldCheck(DomainModel):
    """One extracted value checked against the load (a PO number, the MC number)."""

    field: str
    expected: str | None
    found: str | None
    ok: bool
    check: FieldCheckKind = "none"  # the field's `FieldDef.check`


# --- the extract model's answer -------------------------------------------------------------


class ExtractedValue(BaseModel):
    """One field as the model read it. Every value travels as text, so the answer schema
    is the same for every kind and every broker; code converts it (`read_extraction`)."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="The field's name, exactly as listed for the type.")
    value: str | None = Field(
        description=(
            "The value as text, in the format of the field's kind. null when the document "
            "does not show it or it cannot be read."
        )
    )

    @field_validator("value", mode="before")
    @classmethod
    def _as_text(cls, value: object) -> object:
        """A model may answer `true` or `38000` for a text field: the same as the text."""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int | float):
            return str(value)
        return value


class Extraction(BaseModel):
    """The `extract` model's answer for one document (structured output): its type among
    the broker's document types and the values of that type's fields.

    The schema names no type and no field: they are in the prompt. So it stays the same
    when a broker adds a type, and `read_extraction` validates the answer against the
    chosen type in code.
    """

    model_config = ConfigDict(frozen=True)

    type_id: str | None = Field(
        description="The id of the document's type, from the list. null when none fits."
    )
    confidence: float = Field(
        description="From 0 to 1: how sure you are of the type and of the values."
    )
    fields: tuple[ExtractedValue, ...] = Field(
        default=(), description="One entry per field of the type, with its name and value."
    )
    notes: str = Field(
        default="",
        description=(
            "Anything a person checking the document should know: a corrected value, a "
            "handwritten remark, a part you could not read. Empty when there is nothing."
        ),
    )


# --- the pipeline's conclusion ----------------------------------------------------------------


class Reading(DomainModel):
    """What one document turned out to be: its status, type, values (each in its field's
    kind) and checks, and why a person must look at it (empty when it is clean)."""

    status: DocumentStatus
    type_id: str | None = None
    confidence: float | None = None
    extracted: dict[str, Any] = Field(default_factory=dict)
    checks: tuple[FieldCheck, ...] = ()
    reasons: tuple[str, ...] = ()

    @classmethod
    def failed(cls, reason: str) -> "Reading":
        """The file or the model could not be read."""
        return cls(status=DocumentStatus.FAILED, reasons=(reason,))

    def summary(self) -> str:
        """One line for the `document_processed` event: type, status and the reasons."""
        head = f"{self.type_id or 'unknown type'}: {self.status.value}"
        return f"{head}: {'; '.join(self.reasons)}" if self.reasons else head


class Document(DomainModel):
    document_id: str
    load_id: str
    attachment_id: str
    broker_id: str
    blob_ref: str
    filename: str
    content_type: str
    status: DocumentStatus = DocumentStatus.PENDING
    type_id: str | None = None  # a `DocumentTypeDef.type_id` of the broker, once classified
    confidence: float | None = None
    extracted: dict[str, Any] = Field(default_factory=dict)
    checks: tuple[FieldCheck, ...] = ()
    text: str = ""  # every page's text, for read_document
    pages: int = 0
    received_at: datetime
    processed_at: datetime | None = None

    @classmethod
    def pending(
        cls,
        *,
        document_id: str,
        load_id: str,
        attachment_id: str,
        broker_id: str,
        blob_ref: str,
        filename: str,
        content_type: str,
        received_at: datetime,
    ) -> "Document":
        """An attachment just received, before the pipeline reads it."""
        return cls(
            document_id=document_id,
            load_id=load_id,
            attachment_id=attachment_id,
            broker_id=broker_id,
            blob_ref=blob_ref,
            filename=filename,
            content_type=content_type,
            received_at=received_at,
        )

    @property
    def clean(self) -> bool:
        """Processed with every check ok: what satisfies a document requirement."""
        return self.status is DocumentStatus.PROCESSED

    def read(self, reading: Reading, *, text: str, pages: int, at: datetime) -> "Document":
        """The document as the pipeline finished it at `at`."""
        return self.model_copy(
            update={
                "status": reading.status,
                "type_id": reading.type_id,
                "confidence": reading.confidence,
                "extracted": dict(reading.extracted),
                "checks": reading.checks,
                "text": text,
                "pages": pages,
                "processed_at": at,
            }
        )


def read_extraction(
    extraction: Extraction,
    types: Sequence[DocumentTypeDef],
    load: Load,
    *,
    reported_seal: str | None = None,
    min_confidence: float = MIN_CONFIDENCE,
) -> Reading:
    """The model's answer, validated against the chosen type and checked against the load.

    Only the chosen type's fields are kept, in the type's order; a field the model left
    out is None. A missing or unreadable value, a failed check, an exception noted, an
    unknown type or a confidence under `min_confidence` each add a reason, and any reason
    makes the document `needs_review`.
    """
    confidence = min(max(extraction.confidence, 0.0), 1.0)
    named = (extraction.type_id or "").strip()
    type_def = next((t for t in types if t.type_id == named), None)
    if type_def is None:
        reason = (
            f"the type {named!r} is not one of the broker's document types"
            if named
            else "the document is none of the broker's document types"
        )
        return Reading(status=DocumentStatus.NEEDS_REVIEW, confidence=confidence, reasons=(reason,))

    given = _given(extraction.fields)
    extracted: dict[str, Any] = {}
    checks: list[FieldCheck] = []
    reasons: list[str] = []
    if confidence < min_confidence:
        reasons.append(f"the reading is uncertain (confidence {confidence:.2f})")
    for field in type_def.fields:
        value = _Value.parse(field, given.get(field.name))
        extracted[field.name] = value.stored
        problem = value.problem(field)
        if problem is not None:
            reasons.append(problem)
        if field.check != "none":
            check, failure = _check(field, value, load, reported_seal)
            checks.append(check)
            if failure is not None and problem is None:
                reasons.append(failure)
        if field.name in EXCEPTION_FIELDS and isinstance(value.stored, str) and value.stored:
            reasons.append(f"the document notes exceptions: {value.stored}")
    return Reading(
        status=DocumentStatus.NEEDS_REVIEW if reasons else DocumentStatus.PROCESSED,
        type_id=type_def.type_id,
        confidence=confidence,
        extracted=extracted,
        checks=tuple(checks),
        reasons=tuple(reasons),
    )


def _given(fields: Iterable[ExtractedValue]) -> dict[str, str]:
    """Name -> value, the first value given for a name; null values count as not given."""
    given: dict[str, str] = {}
    for item in fields:
        name = item.name.strip()
        if item.value is not None and name not in given:
            given[name] = item.value
    return given


# --- values -----------------------------------------------------------------------------------

_TRUE = frozenset({"true", "yes", "y", "signed", "present", "1"})
_FALSE = frozenset({"false", "no", "n", "unsigned", "not signed", "absent", "missing", "0"})
_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_US_DATE = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})"
    r"(?:[ T,]+(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?)?"
)
_KIND_WORDS: Mapping[FieldKind, str] = {
    "text": "text",
    "number": "number",
    "date": "date",
    "datetime": "date and time",
    "bool": "yes or no",
}

type When = date | datetime  # a datetime is a date too: test for datetime first


class _Value:
    """One extracted value: the raw text, what goes in `Document.extracted` (`stored`)
    and the Python value the checks compare (`native`; None when unreadable)."""

    def __init__(self, raw: str | None, stored: Any, native: Any) -> None:
        self.raw = raw
        self.stored = stored
        self.native = native

    @classmethod
    def parse(cls, field: FieldDef, raw: str | None) -> "_Value":
        """An exception field with nothing noted (null, empty, "none") is `""`; any other
        empty value is missing."""
        if field.kind == "text" and field.name in EXCEPTION_FIELDS:
            text = (raw or "").strip()
            text = "" if text.casefold() in _NO_EXCEPTION else text
            return cls(raw or "", text, text)
        if raw is None or not raw.strip():
            return cls(None, None, None)
        text = raw.strip()
        match field.kind:
            case "text":
                return cls(raw, text, text)
            case "number":
                number = _number(text)
                return cls(raw, text if number is None else number, number)
            case "bool":
                flag = _flag(text)
                return cls(raw, text if flag is None else flag, flag)
            case "date":
                when = parse_when(text)
                day = when.date() if isinstance(when, datetime) else when
                return cls(raw, text if day is None else day.isoformat(), day)
            case "datetime":
                pass
        when = parse_when(text)
        return cls(raw, text if when is None else _iso(when), when)

    def problem(self, field: FieldDef) -> str | None:
        """Why this value cannot be used: missing, or not readable as its kind."""
        if self.raw is None:
            return f"{field.name} is missing"
        if self.native is None:
            return f"{field.name} {self.raw.strip()!r} is not a {_KIND_WORDS[field.kind]}"
        return None

    def found(self) -> str | None:
        """The value as `FieldCheck.found` shows it."""
        if self.stored is None:
            return None
        if isinstance(self.stored, bool):
            return "true" if self.stored else "false"
        return str(self.stored)


def _number(text: str) -> int | float | None:
    match = _NUMBER.search(text.replace(" ", ""))
    if match is None:
        return None
    number = float(match.group().replace(",", ""))
    return int(number) if number.is_integer() else number


def _flag(text: str) -> bool | None:
    word = text.casefold()
    if word in _TRUE:
        return True
    if word in _FALSE:
        return False
    return None


def parse_when(text: str) -> When | None:
    """A date or a date and time as a model or a person writes it: ISO
    (`2026-10-14`, `2026-10-14T09:42`, with or without an offset) or US
    (`10/14/2026`, `10/14/26 9:42 AM`). A time without offset stays naive (local)."""
    text = text.strip()
    try:
        if _ISO_DATE.fullmatch(text):
            return date.fromisoformat(text)
        return datetime.fromisoformat(text)
    except ValueError:  # not ISO, or a month or a day out of range
        pass
    us = _US_DATE.fullmatch(text)
    if us is None:
        return None
    month, day, year, hour, minute, meridiem = us.groups()
    full_year = int(year) + (2000 if len(year) == 2 else 0)
    try:
        if hour is None:
            return date(full_year, int(month), int(day))
        clock = int(hour)
        if meridiem:  # 12:05 AM is 00:05, 12:05 PM is 12:05
            clock = clock % 12 + (12 if meridiem.casefold() == "pm" else 0)
        return datetime(full_year, int(month), int(day), clock, int(minute))
    except ValueError:  # a day or an hour out of range
        return None


def _iso(when: When) -> str:
    """`YYYY-MM-DD`, `YYYY-MM-DDTHH:MM` (local) or with its offset when it has one."""
    if not isinstance(when, datetime):
        return when.isoformat()
    if when.tzinfo is None:
        return when.strftime("%Y-%m-%dT%H:%M")
    return when.isoformat(timespec="minutes")


# --- checks against the load ------------------------------------------------------------------

_TOKEN = re.compile(r"[A-Za-z0-9-]*\d[A-Za-z0-9-]*")
_PO = re.compile(
    r"\bP\.?O\.?(?![A-Za-z])(?:\s*(?:#|no\.?|number))?\s*[:#]?\s*([A-Za-z0-9-]*\d[A-Za-z0-9-]*)",
    re.IGNORECASE,
)
_SEAL = re.compile(
    r"\bseal\b(?:\s*(?:number|no\.?|num|#))?\s*(?:is|was|:|#|=)?\s*([A-Za-z0-9][A-Za-z0-9-]*)",
    re.IGNORECASE,
)


def load_po_numbers(load: Load) -> tuple[str, ...]:
    """The PO numbers the load's instructions give ("PO 55821 required at gate")."""
    found = (m.group(1).upper() for m in _PO.finditer(load.instructions))
    return tuple(dict.fromkeys(found))


def reported_seal(events: Iterable[Event]) -> str | None:
    """The seal number the last message of the load that names one reported."""
    seal: str | None = None
    for event in events:
        if event.type is not EventType.INBOUND_MESSAGE:
            continue
        for match in _SEAL.finditer(event.body):
            if any(c.isdigit() for c in match.group(1)):
                seal = match.group(1).upper()
    return seal


def _check(
    field: FieldDef, value: _Value, load: Load, seal: str | None
) -> tuple[FieldCheck, str | None]:
    """The field's check and, when it fails, why (a sentence for the reviewer)."""
    found = value.found()
    if value.native is None:  # missing or unreadable: `_Value.problem` gave the reason
        expected = _expected_without_value(field, load, seal)
        return _result(field, expected, found, False), None
    match field.check:
        case "po_number":
            pos = load_po_numbers(load)
            tokens = {t.upper() for t in _TOKEN.findall(str(value.native))}
            ok = all(p in tokens for p in pos)
            expected = ", ".join(pos) or None
            why = f"PO {found} on the document does not match the load's PO {expected}"
        case "mc_number":
            expected = load.mc_number
            ok = _digits(expected) in {_digits(t) for t in _TOKEN.findall(str(value.native))}
            why = f"MC {found} on the document is not the carrier's MC {expected}"
        case "stop_name":
            names = _names(load)
            matched = next((n for n in names if _names_match(n, str(value.native))), None)
            ok = matched is not None
            expected = matched or " | ".join(names)
            why = (
                f"{field.name} {found!r} is neither the shipper nor a stop of the load ({expected})"
            )
        case "date_in_window":
            when = value.native if isinstance(value.native, date) else parse_when(str(value.native))
            ok, expected = _in_window(field, when, load)
            why = f"{field.name} {found} is outside the appointment ({expected})"
        case "seal_number":
            expected = seal
            ok = seal is None or _compact(str(value.native)) == _compact(seal)
            why = f"seal {found} on the document is not the seal reported ({expected})"
        case "signature":
            expected = "true"
            ok = value.native is True or _flag(str(value.native)) is True
            why = f"{field.name}: the document is not signed"
        case "none":  # pragma: no cover - `read_extraction` checks only checked fields
            return _result(field, None, found, True), None
    return _result(field, expected, found, ok), None if ok else why


def _result(field: FieldDef, expected: str | None, found: str | None, ok: bool) -> FieldCheck:
    return FieldCheck(field=field.name, expected=expected, found=found, ok=ok, check=field.check)


def _expected_without_value(field: FieldDef, load: Load, seal: str | None) -> str | None:
    match field.check:
        case "po_number":
            return ", ".join(load_po_numbers(load)) or None
        case "mc_number":
            return load.mc_number
        case "stop_name":
            return " | ".join(_names(load))
        case "date_in_window":
            return _in_window(field, None, load)[1]
        case "seal_number":
            return seal
        case "signature":
            return "true"
        case "none":  # pragma: no cover
            return None


def _digits(text: str) -> str:
    return "".join(c for c in text if c.isdigit()).lstrip("0")


def _compact(text: str) -> str:
    return "".join(c for c in text.upper() if c.isalnum())


def _names(load: Load) -> tuple[str, ...]:
    return tuple(dict.fromkeys((load.shipper, *(s.name for s in load.stops))))


def _words(text: str) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text.casefold()).split())


def _names_match(name: str, value: str) -> bool:
    """`value` names `name`: the same words, maybe among others ("Valley DC, Reno")."""
    wanted = _words(name)
    return bool(wanted) and f" {wanted} " in f" {_words(value)} "


def _stops_for(field: FieldDef, load: Load) -> tuple[Stop, ...]:
    """The stops a date field is about, from its name; every stop when it says nothing."""
    name = field.name.casefold()
    if "deliver" in name:
        wanted: StopType | None = StopType.DELIVERY
    elif "pickup" in name or "pick_up" in name or "ship" in name:
        wanted = StopType.PICKUP
    else:
        wanted = None
    stops = tuple(s for s in load.stops if wanted is None or s.type is wanted)
    return stops or load.stops


def _in_window(field: FieldDef, when: When | None, load: Load) -> tuple[bool, str | None]:
    """Whether `when` falls in the appointment of one of the field's stops, and the
    appointment it matched (or every candidate's, when none matched)."""
    shown: list[str] = []
    for stop in _stops_for(field, load):
        zone = ZoneInfo(stop.timezone)
        appointment = stop.appointment
        if appointment.kind is AppointmentKind.FIXED and appointment.at is not None:
            window = _local(appointment.at, zone)
            ok = when is not None and _day(when, zone) == appointment.at.astimezone(zone).date()
        elif appointment.start is not None and appointment.end is not None:
            start, end = appointment.start, appointment.end
            window = f"{_local(start, zone)}/{_local(end, zone)}"
            ok = when is not None and _inside(when, start, end, zone)
        else:
            continue
        if ok:
            return True, window
        shown.append(window)
    return False, " | ".join(shown) or None


def _local(at: datetime, zone: ZoneInfo) -> str:
    return at.astimezone(zone).strftime("%Y-%m-%dT%H:%M")


def _inside(when: When, start: datetime, end: datetime, zone: ZoneInfo) -> bool:
    """A time between the window's ends; a date on one of the window's days."""
    if isinstance(when, datetime):
        instant = when.replace(tzinfo=zone) if when.tzinfo is None else when
        return start <= instant <= end
    return start.astimezone(zone).date() <= when <= end.astimezone(zone).date()


def _day(when: When, zone: ZoneInfo) -> date:
    if isinstance(when, datetime):
        return (when if when.tzinfo is None else when.astimezone(zone)).date()
    return when
