"""The agent's action vocabulary and the plan the decide step returns.

The model proposes; code disposes. Nothing here has side effects. Ledger
changes are expressed by obligation *kind* and timers in *minutes from now*
so the model never handles ids or computes absolute times.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from freight_agent.domain.base import DomainModel
from freight_agent.domain.load import Stage
from freight_agent.domain.obligations import ObligationKind


class Purpose(StrEnum):
    """Why an outbound message is sent. `eta_request` counts as an attempt."""

    ETA_REQUEST = "eta_request"
    REPLY = "reply"
    ESCALATION = "escalation"
    INFO = "info"


class CloseStatus(StrEnum):
    SATISFIED = "satisfied"
    EXHAUSTED = "exhausted"
    CANCELLED = "cancelled"


class SendSms(DomainModel):
    type: Literal["send_sms"] = "send_sms"
    contact_id: str
    text: str = Field(min_length=1, max_length=600)
    purpose: Purpose = Purpose.REPLY


class SendEmail(DomainModel):
    """An email in the load's main thread."""

    type: Literal["send_email"] = "send_email"
    to_contact_ids: tuple[str, ...] = Field(min_length=1)
    body: str = Field(min_length=1)
    subject: str | None = None
    purpose: Purpose = Purpose.INFO


class PostBrokerSlack(DomainModel):
    type: Literal["post_broker_slack"] = "post_broker_slack"
    text: str = Field(min_length=1)


class WriteTmsNote(DomainModel):
    type: Literal["write_tms_note"] = "write_tms_note"
    text: str = Field(min_length=1)


class CreateHeroTask(DomainModel):
    type: Literal["create_hero_task"] = "create_hero_task"
    title: str = Field(min_length=1)
    description: str
    urgent: bool = False


class PostInternalSlack(DomainModel):
    type: Literal["post_internal_slack"] = "post_internal_slack"
    text: str = Field(min_length=1)


class UpdateStage(DomainModel):
    type: Literal["update_stage"] = "update_stage"
    stage: Stage


class ScheduleTimer(DomainModel):
    type: Literal["schedule_timer"] = "schedule_timer"
    kind: ObligationKind
    due_in_minutes: int = Field(ge=1, le=1440)


class DeferObligation(DomainModel):
    type: Literal["defer_obligation"] = "defer_obligation"
    kind: ObligationKind
    minutes: int = Field(ge=1)


class CloseObligation(DomainModel):
    type: Literal["close_obligation"] = "close_obligation"
    kind: ObligationKind
    status: CloseStatus


Action = Annotated[
    SendSms
    | SendEmail
    | PostBrokerSlack
    | WriteTmsNote
    | CreateHeroTask
    | PostInternalSlack
    | UpdateStage
    | ScheduleTimer
    | DeferObligation
    | CloseObligation,
    Field(discriminator="type"),
]

OUTBOUND_TYPES = frozenset({"send_sms", "send_email", "post_broker_slack"})


class Plan(DomainModel):
    """Structured output of the decide model. An empty plan is a valid no-op."""

    actions: tuple[Action, ...] = ()
    rationale: str = ""
