"""What an event means, as classified by the gate.

An intent is a string id from the broker's catalog (`domain.broker.Broker.intents`):
brokers add their own at runtime. `Intent` keeps the ids of the standard catalog
(`domain.broker.standard_broker`) as constants, so code and tests that name a standard
intent stay typo-proof. Which intents must act or may end a run without action is
catalog data (`IntentDef.must_act`, `IntentDef.noop_eligible`), not a constant here.
"""

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
    DOCUMENT_PROCESSED = "document_processed"


class GateDecision(DomainModel):
    """Structured output of the gate model. `intent` is an intent id of the catalog."""

    intent: str
    act: bool
    reason: str


class GateScores(DomainModel):
    """What a decision model weighed when it triaged a message (the `jev` gate engine):
    the probability of each message intent of the catalog, in the catalog's order, and
    the probability that the message needs the agent to act now. `threshold` is the act
    probability under which the policy let a quiet intent end the run."""

    model: str
    intents: dict[str, float]
    act: float
    threshold: float
    intent_confidence: float | None = None
