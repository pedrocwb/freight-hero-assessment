"""Mapping between domain models and rows.

Models go out through `model_dump(mode="json")` and come back through `model_validate`,
so a row holds exactly what the model would serialize. Two exceptions keep the columns
typed: timestamps stay aware datetimes for `timestamptz`, and nested values are wrapped
as `jsonb`. A timestamp column comes back in UTC (`connection.begin_command`): the same
instant, not the zone it was written in. Timestamps inside a JSONB document keep their
offset.
"""

from collections.abc import Collection
from datetime import datetime
from typing import Any

from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from freight_agent.domain.events import Event
from freight_agent.domain.load import Load
from freight_agent.domain.obligations import Obligation
from freight_agent.domain.outbox import OutboxItem
from freight_agent.domain.ping_filter import RiskTracker
from freight_agent.domain.runs import RunRecord
from freight_agent.domain.timers import Timer


def columns(model: BaseModel, *, jsonb: Collection[str] = ()) -> dict[str, Any]:
    """One parameter per field of a flat model, named after the field."""
    values = model.model_dump(mode="json")
    params: dict[str, Any] = {}
    for name, value in values.items():
        raw = getattr(model, name)
        if isinstance(raw, datetime):
            params[name] = _aware(raw, f"{type(model).__name__}.{name}")
        elif name in jsonb:
            params[name] = Jsonb(value)
        else:
            params[name] = value
    return params


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        # The database would read it in the session's zone and shift it silently.
        raise ValueError(f"{field} must be timezone-aware")
    return value


def load_params(load: Load) -> dict[str, Any]:
    return {
        "load_id": load.load_id,
        "broker_id": load.broker_id,
        "stage": load.stage.value,
        "delay_risk": load.delay_risk.value,
        "metadata": Jsonb(load.model_dump(mode="json")),
    }


def load_from(row: DictRow) -> Load:
    return Load.model_validate(row["metadata"])


def tracker_params(load_id: str, tracker: RiskTracker) -> dict[str, Any]:
    return {"load_id": load_id, **columns(tracker)}


def tracker_from(row: DictRow) -> RiskTracker:
    return RiskTracker.model_validate(row)


def event_params(event: Event) -> dict[str, Any]:
    return columns(event, jsonb=("attachments", "data"))


def event_from(row: DictRow) -> Event:
    return Event.model_validate(row)


def obligation_params(obligation: Obligation) -> dict[str, Any]:
    return columns(obligation)


def obligation_from(row: DictRow) -> Obligation:
    return Obligation.model_validate(row)


def timer_params(timer: Timer) -> dict[str, Any]:
    return columns(timer)


def timer_from(row: DictRow) -> Timer:
    return Timer.model_validate(row)


def outbox_params(item: OutboxItem) -> dict[str, Any]:
    return columns(item, jsonb=("payload",))


def outbox_from(row: DictRow) -> OutboxItem:
    return OutboxItem.model_validate(row)


def run_params(run: RunRecord) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "event_id": run.event_id,
        "load_id": run.load_id,
        "started_at": _aware(run.started_at, "RunRecord.started_at"),
        "status": run.status.value,
        "record": Jsonb(run.model_dump(mode="json")),
    }


def run_from(row: DictRow) -> RunRecord:
    return RunRecord.model_validate(row["record"])
