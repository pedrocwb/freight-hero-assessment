from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from freight_agent.domain.errors import DomainError
from freight_agent.domain.load import (
    AppointmentKind,
    DelayRisk,
    Load,
    Role,
    Stage,
    StopType,
)

LA = ZoneInfo("America/Los_Angeles")

METADATA = {
    "load_id": "481207",
    "broker": {"id": "northline", "name": "Northline Logistics"},
    "shipper": "Riverbend Foods",
    "carrier": {"name": "Prairie Haul LLC", "mc_number": "778120"},
    "commodity": "Canned tomato sauce, 38,000 lb, dry van",
    "stage": "to_pickup",
    "delay_risk": "low",
    "contacts": [
        {"id": "drv_20931", "role": "driver", "channels": ["sms"]},
        {"id": "dsp_4410", "role": "dispatcher", "channels": ["email"]},
        {"id": "brk_0087", "role": "broker", "channels": ["email", "slack"]},
    ],
    "stops": [
        {
            "sequence": 1,
            "type": "pickup",
            "name": "Riverbend plant",
            "address": "Stockton, CA",
            "timezone": "America/Los_Angeles",
            "appointment": {
                "type": "window",
                "start": "2026-10-13T06:00",
                "end": "2026-10-13T14:00",
            },
        },
        {
            "sequence": 2,
            "type": "delivery",
            "name": "Valley DC",
            "address": "Reno, NV",
            "timezone": "America/Los_Angeles",
            "appointment": {"type": "fixed", "time": "2026-10-14T09:30"},
        },
    ],
    "instructions": "Call 1 hr before arrival. PO 55821 required at gate.",
}


@pytest.mark.unit
def test_parses_the_assignment_metadata() -> None:
    load = Load.from_metadata(METADATA)

    assert load.load_id == "481207"
    assert load.broker_id == "northline"
    assert load.stage is Stage.TO_PICKUP
    assert load.delay_risk is DelayRisk.LOW
    assert load.carrier_name == "Prairie Haul LLC"
    assert "PO 55821" in load.instructions


@pytest.mark.unit
def test_appointments_are_timezone_aware_in_the_stop_timezone() -> None:
    load = Load.from_metadata(METADATA)

    pickup, delivery = load.stops
    assert pickup.type is StopType.PICKUP
    assert pickup.appointment.kind is AppointmentKind.WINDOW
    assert pickup.appointment.start == datetime(2026, 10, 13, 6, 0, tzinfo=LA)
    assert pickup.appointment.end == datetime(2026, 10, 13, 14, 0, tzinfo=LA)
    assert delivery.appointment.kind is AppointmentKind.FIXED
    assert delivery.appointment.at == datetime(2026, 10, 14, 9, 30, tzinfo=LA)


@pytest.mark.unit
def test_contact_lookup_by_id_and_role() -> None:
    load = Load.from_metadata(METADATA)

    driver = load.contact("drv_20931")
    assert driver is not None
    assert driver.role is Role.DRIVER
    assert [c.id for c in load.contacts_with_role(Role.BROKER)] == ["brk_0087"]
    assert load.contact("nobody") is None


@pytest.mark.unit
def test_load_types_default_to_empty_and_can_be_declared() -> None:
    assert Load.from_metadata(METADATA).load_types == ()
    declared = Load.from_metadata({**METADATA, "load_types": ["reefer"]})
    assert declared.load_types == ("reefer",)


@pytest.mark.unit
def test_current_stop_is_the_first_stop_of_the_active_stage() -> None:
    load = Load.from_metadata(METADATA)
    assert load.current_stop().sequence == 1
    delivery_bound = load.model_copy(update={"stage": Stage.TO_DELIVERY})
    assert delivery_bound.current_stop().sequence == 2


@pytest.mark.unit
def test_metadata_without_stops_is_rejected_with_a_clear_error() -> None:
    """Every run reads the current stop (its appointment, its timezone), escalations too."""
    with pytest.raises(DomainError, match="invalid load metadata: a load needs at least one stop"):
        Load.from_metadata({**METADATA, "stops": []})


@pytest.mark.unit
def test_a_load_cannot_be_built_without_stops() -> None:
    load = Load.from_metadata(METADATA)

    with pytest.raises(ValidationError, match="stops"):
        Load.model_validate({**load.model_dump(), "stops": ()})


@pytest.mark.unit
def test_unknown_role_is_rejected() -> None:
    bad = {**METADATA, "contacts": [{"id": "x", "role": "pilot", "channels": ["sms"]}]}
    with pytest.raises(DomainError):
        Load.from_metadata(bad)
