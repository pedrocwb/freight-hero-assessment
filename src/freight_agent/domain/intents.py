"""What an event means, as classified by the gate."""

from enum import StrEnum

from freight_agent.domain.base import DomainModel


class Intent(StrEnum):
    ETA_PROVIDED = "eta_provided"
    ETA_DEFERRED = "eta_deferred"
    QUESTION = "question"
    ACKNOWLEDGEMENT = "acknowledgement"
    STATUS_UPDATE = "status_update"
    DOCUMENT = "document"
    DELAY_RISK_HIGH = "delay_risk_high"
    DELAY_RISK_LOW = "delay_risk_low"
    ETA_FOLLOW_UP_DUE = "eta_follow_up_due"
    DRIVER_DEPARTED = "driver_departed"
    OTHER = "other"


# Intents that may end a run without action when nothing is open.
NOOP_ELIGIBLE = frozenset({Intent.ACKNOWLEDGEMENT})

# Intents that always need a decision, whatever the gate model says.
MUST_ACT = frozenset(
    {
        Intent.ETA_PROVIDED,
        Intent.ETA_DEFERRED,
        Intent.QUESTION,
        Intent.DOCUMENT,
        Intent.DELAY_RISK_HIGH,
        Intent.DELAY_RISK_LOW,
        Intent.ETA_FOLLOW_UP_DUE,
        Intent.DRIVER_DEPARTED,
    }
)


class GateDecision(DomainModel):
    """Structured output of the gate model."""

    intent: Intent
    act: bool
    reason: str
