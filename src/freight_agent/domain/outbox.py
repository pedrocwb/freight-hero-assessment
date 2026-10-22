"""Outbox: side effects recorded in the same transaction as state, delivered later."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from freight_agent.domain.base import DomainModel


class OutboxKind(StrEnum):
    SMS = "sms"
    EMAIL = "email"
    BROKER_SLACK = "broker_slack"
    TMS_NOTE = "tms_note"
    HERO_TASK = "hero_task"
    INTERNAL_SLACK = "internal_slack"
    STAGE_UPDATE = "stage_update"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutboxItem(DomainModel):
    """`idempotency_key` is `{event_id}:{action_index}`, so re-running an event never duplicates."""

    outbox_id: str
    idempotency_key: str
    run_id: str
    load_id: str
    kind: OutboxKind
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = 0
    last_error: str | None = None
    accepted_at: datetime | None = None
