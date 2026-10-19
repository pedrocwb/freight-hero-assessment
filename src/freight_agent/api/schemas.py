"""Request and response bodies of the platform API (`docs/api.md`).

Domain objects (loads, snapshots, runs, outbox items, SOP sections) go out as they are,
dumped with `model_dump(mode="json")`; the models here are the views the platform adds.
Datetimes are ISO 8601 with their offset, in the simulation's timezone.
"""

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from freight_agent.application.contracts import InboundMessage, LocationPing
from freight_agent.application.metrics import (
    CostMetrics,
    HealthLevel,
    HealthReason,
    OutboxMetrics,
    QueueMetrics,
    RunMetrics,
    StalledObligation,
    TimerLagMetrics,
)
from freight_agent.domain.events import Attachment
from freight_agent.domain.load import Channel, DelayRisk, Stage
from freight_agent.domain.outbox import OutboxStatus
from freight_agent.domain.runs import RunRecord
from freight_agent.domain.sop import ResolvedSection
from freight_agent.settings import LlmMode

ConversationChannel = Literal["sms", "email", "slack"]
TaskStatus = Literal["open", "done"]


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- simulation -------------------------------------------------------------------------------


class ResetRequest(_Body):
    start: datetime | None = Field(
        default=None, description="Default 2026-10-13T06:55 in the timezone; no offset: local"
    )
    llm_mode: LlmMode | None = Field(default=None, description="Default: the LLM_MODE setting")
    timezone: str | None = Field(default=None, description="IANA name; America/Los_Angeles")


class AdvanceRequest(_Body):
    minutes: float | None = Field(default=None, ge=0)
    to: datetime | None = Field(default=None, description="No offset: the simulation timezone")

    @model_validator(mode="after")
    def _one_target(self) -> Self:
        if (self.minutes is None) == (self.to is None):
            raise ValueError('give exactly one of "minutes" and "to"')
        return self


class LoadSummary(_Body):
    load_id: str
    broker_id: str
    broker_name: str
    stage: Stage
    delay_risk: DelayRisk
    open_obligations: int
    pending_timers: int
    cost_usd: float


class SimState(_Body):
    now: datetime
    timezone: str
    next_timer_due_at: datetime | None
    llm_mode: str
    queue_size: int
    loads: tuple[LoadSummary, ...]


class RunView(RunRecord):
    """A run record plus its total model cost, which the record computes but does not store."""

    @computed_field  # pyright: ignore[reportUnknownMemberType]
    @property
    def cost_usd(self) -> float:
        return sum(u.cost_usd for u in self.usage)

    @classmethod
    def of(cls, run: RunRecord) -> "RunView":
        return cls.model_validate(run.model_dump())


class StepResult(_Body):
    state: SimState
    runs: tuple[RunView, ...]


# --- ingress ----------------------------------------------------------------------------------


class MessageIn(_Body):
    """`InboundMessage` with the ids and the time optional: the API fills them."""

    message_id: str | None = Field(default=None, description="Default: a new uuid")
    load_id: str
    channel: Channel
    sender_contact_id: str | None = None
    body: str
    occurred_at: datetime | None = Field(default=None, description="Default: simulation now")
    attachments: tuple[Attachment, ...] = ()

    def to_contract(self, *, message_id: str, occurred_at: datetime) -> InboundMessage:
        return InboundMessage(
            message_id=message_id,
            load_id=self.load_id,
            channel=self.channel,
            sender_contact_id=self.sender_contact_id,
            body=self.body,
            occurred_at=occurred_at,
            attachments=self.attachments,
        )


class PingIn(_Body):
    """`LocationPing` with the id and the time optional: the API fills them."""

    ping_id: str | None = Field(default=None, description="Default: a new uuid")
    load_id: str
    lat: float
    lon: float
    occurred_at: datetime | None = Field(default=None, description="Default: simulation now")
    delay_risk: DelayRisk

    def to_contract(self, *, ping_id: str, occurred_at: datetime) -> LocationPing:
        return LocationPing(
            ping_id=ping_id,
            load_id=self.load_id,
            lat=self.lat,
            lon=self.lon,
            occurred_at=occurred_at,
            delay_risk=self.delay_risk,
        )


# --- channels and the Hero console ------------------------------------------------------------


class ConversationEntry(_Body):
    at: datetime
    direction: Literal["in", "out"]
    channel: ConversationChannel
    who: str = Field(description='The sender\'s contact id, or "agent"')
    role: str = Field(description="driver, dispatcher, broker, agent or unknown")
    text: str
    subject: str | None = None
    status: OutboxStatus | None = Field(default=None, description="Delivery, outbound only")
    to: tuple[str, ...] | None = Field(default=None, description="Recipients, SMS and email out")


class TmsNote(_Body):
    at: datetime
    text: str


class HeroTask(_Body):
    task_id: str = Field(description="The outbox id of the agent's create_hero_task")
    load_id: str
    title: str
    description: str
    urgent: bool
    created_at: datetime
    status: TaskStatus
    done_at: datetime | None = None
    note: str | None = None


class CompleteTaskRequest(_Body):
    note: str | None = None


class Fixture(_Body):
    name: str
    metadata: dict[str, Any]


# --- SOPs and health --------------------------------------------------------------------------


class SopView(_Body):
    version: str
    client_id: str
    load_types: tuple[str, ...]
    sections: tuple[ResolvedSection, ...]


class HealthReport(_Body):
    status: HealthLevel
    reasons: tuple[HealthReason, ...]
    queue: QueueMetrics
    runs: RunMetrics
    cost: CostMetrics
    outbox: OutboxMetrics
    timers: TimerLagMetrics
    stalled: tuple[StalledObligation, ...]
    settings: dict[str, str] = Field(description="Settings.describe(): secrets only as set/unset")
