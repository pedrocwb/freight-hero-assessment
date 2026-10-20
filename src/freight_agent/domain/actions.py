"""The agent's action vocabulary, the plan, and the agent's final answer.

The model proposes; code disposes. Nothing here has side effects. Ledger
changes are expressed by obligation *kind* and timers in *minutes from now*
so the model never handles ids or computes absolute times. A kind is a string id of
the broker's obligation catalog (`domain.broker.ObligationDef`).

An outbound message (SMS or email) that asks for something names the obligation it
opens or counts in `requests` (`send_sms(requests="pod")`). `purpose: eta_request` is
the older spelling of the same thing and still means `requests: eta_request`.
"""

from enum import StrEnum
from typing import Annotated, Any, Literal, cast

from pydantic import ConfigDict, Field, model_validator

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


def _requests_from_purpose(data: Any) -> Any:
    """`purpose: eta_request` means `requests: eta_request`. A message that says both and
    names another kind is refused: the executor could not tell which obligation it asks for."""
    if not isinstance(data, dict):
        return data
    fields = cast(dict[str, Any], data)
    if fields.get("purpose") != Purpose.ETA_REQUEST:
        return fields
    requests = fields.get("requests")
    if requests is None:
        return {**fields, "requests": ObligationKind.ETA_REQUEST.value}
    if requests != ObligationKind.ETA_REQUEST:
        raise ValueError(f"purpose eta_request asks for eta_request, not {requests}")
    return fields


class SendSms(DomainModel):
    type: Literal["send_sms"] = "send_sms"
    contact_id: str
    text: str = Field(min_length=1, max_length=600)
    purpose: Purpose = Purpose.REPLY
    requests: str | None = Field(
        default=None,
        description="The obligation kind this SMS asks for; it opens or counts that obligation",
    )

    @model_validator(mode="before")
    @classmethod
    def _purpose_requests(cls, data: Any) -> Any:
        return _requests_from_purpose(data)


class SendEmail(DomainModel):
    """An email in the load's main thread."""

    type: Literal["send_email"] = "send_email"
    to_contact_ids: tuple[str, ...] = Field(min_length=1)
    body: str = Field(min_length=1)
    subject: str | None = None
    purpose: Purpose = Purpose.INFO
    requests: str | None = Field(
        default=None, description="The obligation kind this email asks for"
    )

    @model_validator(mode="before")
    @classmethod
    def _purpose_requests(cls, data: Any) -> Any:
        return _requests_from_purpose(data)


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
    kind: str
    due_in_minutes: int = Field(ge=1, le=1440)


class DeferObligation(DomainModel):
    type: Literal["defer_obligation"] = "defer_obligation"
    kind: str
    minutes: int = Field(ge=1)


class CloseObligation(DomainModel):
    type: Literal["close_obligation"] = "close_obligation"
    kind: str
    status: CloseStatus


class OpenObligation(DomainModel):
    """Start waiting for something the load owes (a POD), with no message sent yet."""

    type: Literal["open_obligation"] = "open_obligation"
    kind: str


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
    | CloseObligation
    | OpenObligation,
    Field(discriminator="type"),
]

OUTBOUND_TYPES = frozenset({"send_sms", "send_email", "post_broker_slack"})


class Plan(DomainModel):
    """What the agent proposes, in order. An empty plan is a valid no-op.

    The rationale comes first: small models fill fields in schema order, and given the
    actions first they returned an empty list, then a rationale naming the right actions.
    """

    rationale: str = Field(
        default="",
        description=(
            "Write this before the actions: the SOP section and the step that apply, and "
            "the actions that step needs now (or why none)."
        ),
    )
    actions: tuple[Action, ...] = Field(
        default=(),
        description="Every action the rationale names, in order. Empty only when none.",
    )


FINISH = "finish"


class AgentOutcome(Plan):
    """The agent's final answer: a plan, plus how it read the event.

    The model gives it by calling `finish` (the schema's title names the answer tool).
    `actions` given here are validated like a plan's (the 22 core scenarios answer this
    way, and with no action tools it is the whole plan); actions the agent staged through
    its tools are recomputed from the transcript by the agent node. `intent` labels the
    event with a catalog id when the agent read it differently from the gate.
    `no_procedure` says no SOP section covers the event.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", title=FINISH)

    actions: tuple[Action, ...] = Field(
        default=(),
        description=(
            "Every action of the plan that you did not stage with an action tool, in order. "
            "With no action tools, every action the rationale names. Empty only when none."
        ),
    )
    intent: str | None = Field(
        default=None, description="The intent id of the event, when it differs from the gate"
    )
    no_procedure: bool = Field(
        default=False, description="True when no SOP section covers this event"
    )
