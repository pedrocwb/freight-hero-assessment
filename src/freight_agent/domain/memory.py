"""Facility memory: what people told us about a pickup or a delivery place, kept across loads.

A load's own memory (its event log, ledger and documents) ends with the load. What a
driver or a Hero learns about a facility on one load ("check in at gate 4", "the PO is
required at the gate", "the dock closes at 15:00") is still true for the next load that
stops there, so it is kept as `FacilityFact`s, per client and per facility:

- A facility is a stop's place, keyed by its normalized name and address
  (`facility_key`): the same plant on two loads has the same key.
- Facts are per client (the broker id). One broker's facts are never shown to another
  broker's loads: what Northline's drivers were told at a dock is Northline's
  information, and one client's notes must not steer another client's agent.

Facts come from a load once it is delivered: a model reads its inbound messages and
proposes operations (`ProposedOperation`: add, update or delete a fact, each citing the
event it comes from), and code checks each one here (`check_operation`) before anything
is written. A fact is untrusted data, like the message it was read from: the agent sees it
as a note, never as an instruction, and the SOP, the load instructions and the rules
always win over it.
"""

import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from freight_agent.domain.base import DomainModel
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.load import Load, Stage, Stop
from freight_agent.domain.outbox import OutboxKind

# What one fact holds at most; a longer one is dropped, not cut (a cut fact can say
# something else).
MAX_FACT_CHARS = 200
# Facts kept per facility; an add over it is dropped until a fact is deleted.
MAX_FACTS_PER_FACILITY = 12
# Operations one extraction may apply; the rest are dropped.
MAX_OPERATIONS = 10
# Facts per stop the agent's prompt shows.
FACTS_PER_STOP = 5


class FactKind(StrEnum):
    CHECK_IN = "check_in"  # where and how to check in: a gate, a window, a door
    HOURS = "hours"  # when the facility receives, ships or closes
    REQUIREMENT = "requirement"  # what the facility asks for: a PO at the gate, a seal
    ACCESS = "access"  # how to get in or around: an entrance, parking, a door
    CONTACT = "contact"  # whom to ask for there (a role or a desk, never a number)
    OTHER = "other"


class FacilityFact(DomainModel):
    """One fact about one facility, for one client. The source is the inbound event of the
    load it was read from; `source_at` is when that event happened."""

    fact_id: str
    client_id: str
    facility_key: str
    facility_name: str
    text: str
    kind: FactKind = FactKind.OTHER
    source_load_id: str
    source_event_id: str
    source_at: datetime
    created_at: datetime
    updated_at: datetime


class FactDelete(DomainModel):
    client_id: str
    facility_key: str
    fact_id: str


class ExtractionStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class ExtractionJob(DomainModel):
    """A load whose facility facts are still to be read: queued when the load is delivered.

    `attempts` counts the tries that failed; after `MAX_EXTRACTION_ATTEMPTS` the job is
    `failed` for good, so a model outage cannot retry forever. The counts and `dropped`
    (one reason per refused operation) say what the extraction did."""

    load_id: str
    client_id: str
    queued_at: datetime
    status: ExtractionStatus = ExtractionStatus.PENDING
    attempts: int = 0
    finished_at: datetime | None = None
    error: str | None = None
    added: int = 0
    updated: int = 0
    deleted: int = 0
    dropped: tuple[str, ...] = ()
    cost_usd: float = 0.0


MAX_EXTRACTION_ATTEMPTS = 3


# --- facility keys ------------------------------------------------------------------------


def _slug(text: str) -> str:
    """Lower case ASCII words joined by hyphens: no accents, no punctuation, no periods."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return "-".join(re.findall(r"[a-z0-9]+", plain.lower()))


def facility_key(stop: Stop) -> str:
    """The stop's place: its normalized name and address, `riverbend-plant~stockton-ca`.
    Deterministic, so the same facility on two loads gets the same key, and safe as a
    store namespace label (no periods)."""
    return f"{_slug(stop.name) or 'unnamed'}~{_slug(stop.address) or 'unknown'}"


def namespace_label(text: str) -> str:
    """A client id as a store namespace label (no periods, never empty)."""
    return _slug(text) or "unknown"


def next_stop(load: Load) -> Stop | None:
    """The stop after the current one, by sequence."""
    current = load.current_stop().sequence
    later = sorted((s for s in load.stops if s.sequence > current), key=lambda s: s.sequence)
    return later[0] if later else None


def stops_in_view(load: Load) -> tuple[Stop, ...]:
    """The stops a run reads facts for: the current stop, then the next one."""
    after = next_stop(load)
    return (load.current_stop(), *((after,) if after is not None else ()))


# --- what the memory model answers ---------------------------------------------------------


class ProposedOperation(BaseModel):
    """One change to the facility facts, as the memory model proposes it."""

    model_config = ConfigDict(frozen=True)

    op: Literal["add", "update", "delete"] = Field(
        description="add a new fact, update an existing fact (by fact_id), or delete one."
    )
    facility_key: str = Field(description="The facility's key, as listed.")
    fact_id: str | None = Field(
        default=None, description="For update and delete: the id of the existing fact."
    )
    text: str | None = Field(
        default=None,
        description="For add and update: the fact, one short sentence in plain words.",
    )
    kind: FactKind = Field(default=FactKind.OTHER, description="What the fact is about.")
    event_id: str = Field(description="The id of the message the fact comes from, as listed.")


class MemoryExtraction(BaseModel):
    """The memory model's answer: the operations to apply, none when nothing new was
    learned."""

    model_config = ConfigDict(frozen=True)

    operations: tuple[ProposedOperation, ...] = Field(
        default=(), description="The changes to make, at most 10. Empty when there are none."
    )


# --- the guardrails -----------------------------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_PHONE = re.compile(
    r"\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"  # 209-555-0147, (209) 555 0147
    r"|\+\d[\d\s().-]{7,}\d"  # +1 209 555 0147
    r"|\b\d{10,}\b"  # 2095550147
)
_MONEY = re.compile(
    r"[$€£]\s?\d"
    r"|\b\d+(\.\d+)?\s?(usd|dollars?|bucks)\b"
    r"|\b(rates?|rate\s?con(firmation)?|prices?|pricing|pay|paid|payment|invoices?|"
    r"costs?|fees?|charges?|cash)\b",
    re.IGNORECASE,
)
_INSTRUCTION = re.compile(
    r"\b(ignore|disregard|forget|override)\b.{0,40}\b(rules?|instructions?|sop|prompt|"
    r"previous|above)\b"
    r"|\bsystem prompt\b|\byou are (now )?(an? )?(ai|assistant|agent|model)\b",
    re.IGNORECASE,
)


def unsafe_text(text: str) -> str | None:
    """Why a fact's text may not be stored, or None. A fact never carries a phone number,
    an email address, money (rates, prices, pay) or words aimed at the agent, and stays
    short."""
    stripped = text.strip()
    if not stripped:
        return "empty text"
    if len(stripped) > MAX_FACT_CHARS:
        return f"longer than {MAX_FACT_CHARS} characters"
    if _EMAIL.search(stripped):
        return "has an email address"
    if _PHONE.search(stripped):
        return "has a phone number"
    if _MONEY.search(stripped):
        return "talks about money (rates, prices or pay)"
    if _INSTRUCTION.search(stripped):
        return "reads as an instruction to the agent"
    return None


def _stop_for_stage(load: Load, stage: Stage) -> Stop:
    """The stop a load at `stage` is at or heading to (the load's own current stop logic,
    without its pinned sequence)."""
    return load.model_copy(update={"stage": stage, "current_stop_sequence": None}).current_stop()


def _stage_updates(events: Sequence[Event]) -> list[tuple[int, Stage]]:
    """(position, new stage) of every stage update the agent made, in log order."""
    found: list[tuple[int, Stage]] = []
    for index, event in enumerate(events):
        if (
            event.type is not EventType.AGENT_ACTION
            or event.data.get("kind") != OutboxKind.STAGE_UPDATE
        ):
            continue
        payload = event.data.get("payload")
        raw = payload.get("stage") if isinstance(payload, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        try:
            found.append((index, Stage(str(raw))))  # pyright: ignore[reportUnknownArgumentType]
        except ValueError:
            continue
    return found


def _stage_before(stage: Stage) -> Stage:
    """The stage a load is in just before it moves to `stage`: arriving at or delivering
    to the delivery comes from the road to it; any other move is still around the pickup."""
    if stage in (Stage.AT_DELIVERY, Stage.DELIVERED):
        return Stage.TO_DELIVERY
    return Stage.AT_PICKUP


# Words of a stop name too generic to say which stop a message is about.
_GENERIC = frozenset(
    {
        "plant",
        "warehouse",
        "distribution",
        "center",
        "centre",
        "facility",
        "terminal",
        "dock",
        "yard",
        "depot",
        "shipper",
        "receiver",
        "store",
        "the",
        "and",
    }
)


def _named(stop: Stop, text: str) -> bool:
    words = set(_slug(text).split("-"))
    return any(w in words for w in _slug(stop.name).split("-") if len(w) >= 4 and w not in _GENERIC)


def _is_source(event: Event) -> bool:
    return event.type in (EventType.INBOUND_MESSAGE, EventType.HERO_UPDATE) and bool(
        event.body.strip()
    )


def source_events(load: Load, events: Sequence[Event]) -> dict[str, tuple[Event, frozenset[str]]]:
    """The events a fact may cite, by id, each with the facility keys it may be about.

    A source is an inbound message or a Hero update of the load, with text. It is about
    the stop the load was at or heading to when it arrived (read from the stage updates
    logged around it), and about any stop it names. Before the first stage update, the
    stage is the one that update leaves (`_stage_before`); a load with no stage update at
    all may be at any of its stops.
    """
    updates = _stage_updates(events)
    every = frozenset(facility_key(s) for s in load.stops)
    found: dict[str, tuple[Event, frozenset[str]]] = {}
    for index, event in enumerate(events):
        if event.load_id != load.load_id or not _is_source(event):
            continue
        before = [stage for at, stage in updates if at < index]
        after = [stage for at, stage in updates if at > index]
        if before:
            keys = {facility_key(_stop_for_stage(load, before[-1]))}
        elif after:
            keys = {facility_key(_stop_for_stage(load, _stage_before(after[0])))}
        else:
            keys = set(every)
        keys |= {facility_key(s) for s in load.stops if _named(s, event.body)}
        found[event.event_id] = (event, frozenset(keys))
    return found


def check_operation(
    op: ProposedOperation,
    *,
    load: Load,
    sources: Mapping[str, tuple[Event, frozenset[str]]],
    facts: Mapping[str, Sequence[FacilityFact]],
) -> str | None:
    """Why `op` may not be applied, or None. `sources` are the load's citable events
    (`source_events`); `facts` the client's current facts of each of the load's
    facilities, by facility key.

    The checks: the facility is a stop of this load (so the write stays in this client's
    namespace and on this load's places); the cited event is an inbound event of this
    load about that stop; an update or a delete names an existing fact of that facility;
    the text passes `unsafe_text`; an add is new and fits the facility's cap.
    """
    stop_keys = {facility_key(s) for s in load.stops}
    if op.facility_key not in stop_keys:
        return f"facility {op.facility_key!r} is not a stop of load {load.load_id}"
    source = sources.get(op.event_id)
    if source is None:
        return f"event {op.event_id!r} is not an inbound message of load {load.load_id}"
    if op.facility_key not in source[1]:
        return f"event {op.event_id!r} is not about facility {op.facility_key!r}"
    known = {f.fact_id: f for f in facts.get(op.facility_key, ())}
    if op.op in ("update", "delete"):
        if op.fact_id is None or op.fact_id not in known:
            return f"{op.op} names no existing fact of {op.facility_key!r} ({op.fact_id!r})"
        if op.op == "delete":
            return None
    text = (op.text or "").strip()
    problem = unsafe_text(text)
    if problem is not None:
        return problem
    same = _normalized(text)
    if any(_normalized(f.text) == same for f in known.values() if f.fact_id != op.fact_id):
        return "the facility already has this fact"
    if op.op == "add" and len(known) >= MAX_FACTS_PER_FACILITY:
        return f"the facility already has {MAX_FACTS_PER_FACILITY} facts"
    return None


def _normalized(text: str) -> str:
    return " ".join(_slug(text).split("-"))


def stop_by_key(load: Load) -> dict[str, Stop]:
    return {facility_key(s): s for s in load.stops}
