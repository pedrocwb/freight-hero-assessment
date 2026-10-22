"""The load: metadata that is available when the agent starts."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import Field, ValidationError

from freight_agent.domain.base import DomainModel
from freight_agent.domain.errors import DomainError


class Stage(StrEnum):
    DISPATCHED = "dispatched"
    TO_PICKUP = "to_pickup"
    AT_PICKUP = "at_pickup"
    TO_DELIVERY = "to_delivery"
    AT_DELIVERY = "at_delivery"
    DELIVERED = "delivered"


class DelayRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Role(StrEnum):
    DRIVER = "driver"
    DISPATCHER = "dispatcher"
    BROKER = "broker"


class Channel(StrEnum):
    SMS = "sms"
    EMAIL = "email"
    SLACK = "slack"
    SYSTEM = "system"
    HERO = "hero"


class StopType(StrEnum):
    PICKUP = "pickup"
    DELIVERY = "delivery"


class AppointmentKind(StrEnum):
    FIXED = "fixed"
    WINDOW = "window"


_PICKUP_STAGES = {Stage.DISPATCHED, Stage.TO_PICKUP, Stage.AT_PICKUP}


class Contact(DomainModel):
    id: str
    role: Role
    channels: tuple[Channel, ...]


class Appointment(DomainModel):
    kind: AppointmentKind
    at: datetime | None = None
    start: datetime | None = None
    end: datetime | None = None


class Stop(DomainModel):
    sequence: int
    type: StopType
    name: str
    address: str
    timezone: str
    appointment: Appointment


class Load(DomainModel):
    load_id: str
    broker_id: str
    broker_name: str
    shipper: str
    carrier_name: str
    mc_number: str
    commodity: str
    stage: Stage
    delay_risk: DelayRisk
    contacts: tuple[Contact, ...]
    # Every run reads the current stop (its appointment and timezone), the escalation
    # path included, so a load without one could never be handled.
    stops: tuple[Stop, ...] = Field(min_length=1)
    instructions: str = ""
    load_types: tuple[str, ...] = ()
    current_stop_sequence: int | None = None
    # Serialized, validated SOP/catalog pinned by the durable composition root.
    # JSON avoids a cycle: SOP sections and broker catalogs themselves refer to Stage.
    sop_snapshot: dict[str, Any] | None = None
    automation_stop_requested_at: datetime | None = None
    automation_stopped_at: datetime | None = None

    def contact(self, contact_id: str) -> Contact | None:
        return next((c for c in self.contacts if c.id == contact_id), None)

    def contacts_with_role(self, role: Role) -> tuple[Contact, ...]:
        return tuple(c for c in self.contacts if c.role is role)

    def current_stop(self) -> Stop:
        if self.current_stop_sequence is not None:
            for stop in self.stops:
                if stop.sequence == self.current_stop_sequence:
                    return stop
        wanted = StopType.PICKUP if self.stage in _PICKUP_STAGES else StopType.DELIVERY
        for stop in self.stops:
            if stop.type is wanted:
                return stop
        return self.stops[-1]

    @classmethod
    def from_metadata(cls, data: Mapping[str, Any]) -> "Load":
        """Build a load from the JSON metadata shape given in the assignment."""
        try:
            if not data["stops"]:
                raise DomainError("invalid load metadata: a load needs at least one stop")
            return cls(
                load_id=data["load_id"],
                broker_id=data["broker"]["id"],
                broker_name=data["broker"]["name"],
                shipper=data["shipper"],
                carrier_name=data["carrier"]["name"],
                mc_number=data["carrier"]["mc_number"],
                commodity=data["commodity"],
                stage=Stage(data["stage"]),
                delay_risk=DelayRisk(data["delay_risk"]),
                contacts=tuple(
                    Contact(
                        id=c["id"],
                        role=Role(c["role"]),
                        channels=tuple(Channel(ch) for ch in c["channels"]),
                    )
                    for c in data["contacts"]
                ),
                stops=tuple(_parse_stop(s) for s in data["stops"]),
                instructions=data.get("instructions", ""),
                load_types=tuple(data.get("load_types", ())),
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise DomainError(f"invalid load metadata: {exc}") from exc


def _parse_stop(raw: Mapping[str, Any]) -> Stop:
    tz = ZoneInfo(raw["timezone"])
    return Stop(
        sequence=raw["sequence"],
        type=StopType(raw["type"]),
        name=raw["name"],
        address=raw["address"],
        timezone=raw["timezone"],
        appointment=_parse_appointment(raw["appointment"], tz),
    )


def _parse_appointment(raw: Mapping[str, Any], tz: ZoneInfo) -> Appointment:
    kind = AppointmentKind(raw["type"])

    def local(value: str) -> datetime:
        return datetime.fromisoformat(value).replace(tzinfo=tz)

    if kind is AppointmentKind.FIXED:
        return Appointment(kind=kind, at=local(raw["time"]))
    return Appointment(kind=kind, start=local(raw["start"]), end=local(raw["end"]))


def contacts_of(load: Load, ids: Sequence[str]) -> tuple[Contact | None, ...]:
    return tuple(load.contact(i) for i in ids)
