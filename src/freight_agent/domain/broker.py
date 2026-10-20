"""Brokers as data: the catalog a broker's runs follow, versioned for canary rollouts.

Owner: card W3-BRK (catalog validation, the methods); C0 wrote the shape and a working
`standard_broker()`.

A broker is onboarded without code: its intents (what an event can mean), its
obligation kinds (what it waits for, how often it follows up, who gets the case when
the follow-ups are used up), its document types and the documents a stage requires,
and the limits of its runs. Every broker starts as a copy of the standard catalog
(`new_broker`), which carries today's 12 intents, the `eta_request` obligation and the
four document types of `docs/answers.md` 1.2.

Each save of a broker bumps `config_version`; the repository keeps every version, so a
canary can send a share of the loads to the new version and the rest to the previous
one (`choose_version`, sticky per load id).
"""

import hashlib
from functools import cache
from typing import Literal

from pydantic import Field

from freight_agent.domain.base import DomainModel
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Stage
from freight_agent.domain.obligations import ObligationKind
from freight_agent.domain.validation import ValidationConfig

EscalationTarget = Literal["dispatcher_email", "hero_task", "broker_slack"]
FieldKind = Literal["text", "number", "date", "datetime", "bool"]
FieldCheckKind = Literal[
    "none", "po_number", "mc_number", "stop_name", "date_in_window", "seal_number", "signature"
]

STANDARD_BROKER_ID = "standard"
FOLLOW_UP_SUFFIX = "_follow_up_due"


class IntentDef(DomainModel):
    """One thing an event can mean.

    `priority` orders the message intents in the gate prompt (the first that applies
    wins; lower first). `must_act` intents always go on to the agent, whatever the gate
    model said. `noop_eligible` intents may end a run without action when nothing is
    open. `system` intents come only from code (a risk change, a timer, a processed
    document), never from a message.
    """

    intent_id: str
    title: str
    description: str
    examples: tuple[str, ...] = ()
    priority: int = 100
    must_act: bool = True
    noop_eligible: bool = False
    system: bool = False


class ObligationDef(DomainModel):
    """Something the load owes the broker (an ETA, a POD) and how the agent chases it.

    `follow_up_intent` is the intent of the event its follow-up timer produces; empty
    means `<kind>_follow_up_due`. `on_exhausted` is who gets the case once
    `max_attempts` requests went unanswered.
    """

    kind: str
    title: str
    description: str
    follow_up_minutes: int = 30
    max_attempts: int = 2
    follow_up_intent: str = ""
    on_exhausted: tuple[EscalationTarget, ...] = ("hero_task",)


class FieldDef(DomainModel):
    """One value to extract from a document, and the code check it goes through."""

    name: str
    description: str
    kind: FieldKind = "text"
    check: FieldCheckKind = "none"


class DocumentTypeDef(DomainModel):
    type_id: str
    title: str
    description: str
    fields: tuple[FieldDef, ...]


class DocumentRequirement(DomainModel):
    """A document the load owes once it reaches `at_stage` (a POD at delivery).

    With `opens_obligation`, entering the stage opens that obligation (attempts 0), with
    a follow-up timer after `within_minutes` when it is set; a clean processed document
    of `type_id` closes it as satisfied.
    """

    type_id: str
    at_stage: Stage
    within_minutes: int | None = None
    opens_obligation: str | None = None


class BrokerLimits(DomainModel):
    """The validator's limits and the agent loop's budget for this broker's runs.

    `max_steps` is the number of tool rounds; 0 means one plan in the answer and no
    action tools (the pre-wave-3 behaviour, a per-broker safety switch).
    """

    validation: ValidationConfig = ValidationConfig()
    max_steps: int = 8
    max_cost_usd: float = 0.05
    max_rejections: int = 3


class Rollout(DomainModel):
    """`percent` of the loads run on the current version, the rest on `previous_version`.

    Publishing sets 100 with the version it replaces; rollback is `percent: 0`.
    """

    percent: int = Field(default=100, ge=0, le=100)
    previous_version: int | None = None


class Broker(DomainModel):
    broker_id: str
    name: str
    triage: bool = True  # False: no gate model call; every message goes to the agent
    intents: tuple[IntentDef, ...]
    obligations: tuple[ObligationDef, ...]
    document_types: tuple[DocumentTypeDef, ...] = ()
    documents: tuple[DocumentRequirement, ...] = ()
    limits: BrokerLimits = BrokerLimits()
    sop_version: str | None = None  # the published SOP version this config runs with
    config_version: int = 1
    rollout: Rollout = Rollout()

    def intent(self, intent_id: str) -> IntentDef | None:
        return next((i for i in self.intents if i.intent_id == intent_id), None)

    def obligation(self, kind: str) -> ObligationDef | None:
        return next((o for o in self.obligations if o.kind == kind), None)

    def document_type(self, type_id: str) -> DocumentTypeDef | None:
        return next((d for d in self.document_types if d.type_id == type_id), None)

    def message_intents(self) -> tuple[IntentDef, ...]:
        """The intents a message can have, in priority order (catalog order breaks ties)."""
        return tuple(sorted((i for i in self.intents if not i.system), key=lambda i: i.priority))

    def system_intents(self) -> tuple[IntentDef, ...]:
        return tuple(i for i in self.intents if i.system)

    def follow_up_intent(self, kind: str) -> str | None:
        """What a due follow-up timer of an obligation of `kind` means; None: unknown kind."""
        obligation = self.obligation(kind)
        if obligation is None:
            return None
        return obligation.follow_up_intent or f"{kind}{FOLLOW_UP_SUFFIX}"

    def obligation_for_follow_up(self, intent: str) -> ObligationDef | None:
        """The obligation whose follow-up timer produces `intent`."""
        return next((o for o in self.obligations if self.follow_up_intent(o.kind) == intent), None)

    def must_act(self, intent: str) -> bool:
        found = self.intent(intent)
        return found is not None and found.must_act

    def noop_eligible(self, intent: str) -> bool:
        found = self.intent(intent)
        return found is not None and found.noop_eligible

    def requirements_at(self, stage: Stage) -> tuple[DocumentRequirement, ...]:
        return tuple(r for r in self.documents if r.at_stage is stage)

    def validation_config(self) -> ValidationConfig:
        """The broker's validation limits, with each obligation kind's attempt cap."""
        base = self.limits.validation
        caps = {**base.max_attempts, **{o.kind: o.max_attempts for o in self.obligations}}
        return base.model_copy(update={"max_attempts": caps})


def new_broker(broker_id: str, name: str) -> Broker:
    """A new broker: a copy of the standard catalog under its own id, version 1."""
    return standard_broker().model_copy(update={"broker_id": broker_id, "name": name})


def choose_version(current: Broker, previous: Broker | None, load_id: str) -> Broker:
    """The version a load runs on during a rollout.

    `current.rollout.percent` of the loads get `current`. A load always lands in the same
    bucket (the sha256 of its id), so it keeps its version from one run to the next.
    """
    percent = current.rollout.percent
    if previous is None or percent >= 100:
        return current
    if percent <= 0:
        return previous
    bucket = int.from_bytes(hashlib.sha256(load_id.encode()).digest()[:8], "big") % 100
    return current if bucket < percent else previous


# --- the standard catalog ---------------------------------------------------------------
# Message intent descriptions are the gate prompt's (application/prompts.py), word for word.

_MESSAGE_INTENTS = (
    IntentDef(
        intent_id=Intent.ETA_PROVIDED,
        title="ETA provided",
        description=(
            'gives an arrival time at the next stop, as a clock time or a duration: "be there '
            '9:40", "30 mins out", "ETA 10am". Also a vague reply to our ETA request ("soon", '
            '"on my way"); the ETA procedure judges if it is precise enough.'
        ),
        examples=("be there 9:40", "30 mins out", "ETA 10am"),
        priority=10,
    ),
    IntentDef(
        intent_id=Intent.ETA_DEFERRED,
        title="ETA promised for later",
        description=(
            'promises the ETA or the answer later: "I\'ll tell you in 5 min", "let me check '
            'and get back to you".'
        ),
        examples=("I'll tell you in 5 min", "let me check and get back to you"),
        priority=20,
    ),
    IntentDef(
        intent_id=Intent.QUESTION,
        title="Question",
        description=(
            'asks us something: "What\'s the PO number?", "Where do I check in?", "Is the '
            'appointment still 9:30?".'
        ),
        examples=(
            "What's the PO number?",
            "Where do I check in?",
            "Is the appointment still 9:30?",
        ),
        priority=30,
    ),
    IntentDef(
        intent_id=Intent.DRIVER_DEPARTED,
        title="Driver departed a stop",
        description=(
            'the truck has left or is leaving a stop: "leaving the shipper now", "loaded, '
            'heading out", "just left the receiver". "loaded, heading out" is driver_departed, '
            "not status_update, because the truck is leaving the stop."
        ),
        examples=("leaving the shipper now", "loaded, heading out", "just left the receiver"),
        priority=40,
    ),
    IntentDef(
        intent_id=Intent.DOCUMENT,
        title="Document",
        description=(
            "sends a document or is mainly about one (BOL, POD, rate confirmation, receipt): "
            '"here is the BOL", a photo with no text. A message with an attachment is usually '
            "a document."
        ),
        examples=("here is the BOL",),
        priority=50,
    ),
    IntentDef(
        intent_id=Intent.STATUS_UPDATE,
        title="Status update",
        description=(
            "other news about the load or the truck that is not an ETA and not a departure: "
            '"at the gate", "stuck in traffic", "running late", "waiting for paperwork".'
        ),
        examples=("at the gate", "stuck in traffic", "running late", "waiting for paperwork"),
        priority=60,
        must_act=False,
    ),
    IntentDef(
        intent_id=Intent.ACKNOWLEDGEMENT,
        title="Acknowledgement",
        description=(
            'only acknowledges, with nothing new and no question: "ok", "thanks", "got it", '
            '"👍". An acknowledgement is never an ETA.'
        ),
        examples=("ok", "thanks", "got it", "👍"),
        priority=70,
        must_act=False,
        noop_eligible=True,
    ),
    IntentDef(
        intent_id=Intent.OTHER,
        title="Other",
        description=(
            "anything else: a wrong number, small talk, a request that no intent above covers."
        ),
        priority=1000,  # always last: a broker's own intents (default 100) come before it
        must_act=False,
    ),
)

_SYSTEM_INTENTS = (
    IntentDef(
        intent_id=Intent.DELAY_RISK_HIGH,
        title="Delay risk is high",
        description="The load's delay risk changed to high. Set by code from the location pings.",
        system=True,
    ),
    IntentDef(
        intent_id=Intent.DELAY_RISK_LOW,
        title="Delay risk is back to normal",
        description="The load's delay risk dropped from high. Set by code from the location pings.",
        system=True,
    ),
    IntentDef(
        intent_id=Intent.ETA_FOLLOW_UP_DUE,
        title="ETA follow-up due",
        description=(
            "The follow-up timer of an open ETA request is due: its wait ended without an ETA."
        ),
        system=True,
    ),
    IntentDef(
        intent_id=Intent.DOCUMENT_PROCESSED,
        title="Document processed",
        description=(
            "The document pipeline read an attachment: its type, its values and the checks "
            "against the load."
        ),
        system=True,
    ),
)

_ETA_REQUEST = ObligationDef(
    kind=ObligationKind.ETA_REQUEST,
    title="ETA request",
    description=(
        "We asked the driver for the arrival time at the next stop and wait for it. Each "
        "request SMS is one attempt."
    ),
    follow_up_minutes=30,
    max_attempts=2,
    # Named before the default `<kind>_follow_up_due` existed; SOP files and scenarios use it.
    follow_up_intent=Intent.ETA_FOLLOW_UP_DUE,
    on_exhausted=("dispatcher_email", "hero_task"),
)

_DOCUMENT_TYPES = (
    DocumentTypeDef(
        type_id="bol",
        title="Bill of lading",
        description="Signed at pickup: what was loaded, for whom, under which seal.",
        fields=(
            FieldDef(name="bol_number", description="The BOL number"),
            FieldDef(name="shipper", description="The shipper's name", check="stop_name"),
            FieldDef(name="consignee", description="The consignee's name", check="stop_name"),
            FieldDef(name="po_numbers", description="The PO numbers listed", check="po_number"),
            FieldDef(
                name="seal_number", description="The trailer seal number", check="seal_number"
            ),
            FieldDef(name="weight", description="The total weight", kind="number"),
            FieldDef(
                name="pickup_date",
                description="The pickup date",
                kind="date",
                check="date_in_window",
            ),
            FieldDef(
                name="signed",
                description="Shipper and driver signatures are present",
                kind="bool",
                check="signature",
            ),
        ),
    ),
    DocumentTypeDef(
        type_id="pod",
        title="Proof of delivery",
        description="Signed at delivery: who received the load, when, and any exceptions.",
        fields=(
            FieldDef(name="receiver", description="The receiver's name", check="stop_name"),
            FieldDef(
                name="signed",
                description="The receiver's signature is present",
                kind="bool",
                check="signature",
            ),
            FieldDef(
                name="delivered_at",
                description="The delivery date and time",
                kind="datetime",
                check="date_in_window",
            ),
            FieldDef(
                name="exceptions",
                description="Shortage, damage or refusal noted on the POD; empty when none",
            ),
        ),
    ),
    DocumentTypeDef(
        type_id="rate_confirmation",
        title="Rate confirmation",
        description="The carrier's agreed rate, stops and appointments. Never forwarded.",
        fields=(
            FieldDef(name="carrier_name", description="The carrier's name"),
            FieldDef(name="mc_number", description="The carrier's MC number", check="mc_number"),
            FieldDef(name="rate", description="The total rate", kind="number"),
            FieldDef(name="line_items", description="The rate's line items"),
            FieldDef(name="stops", description="The stops listed", check="stop_name"),
            FieldDef(name="appointments", description="The appointments listed"),
        ),
    ),
    DocumentTypeDef(
        type_id="receipt",
        title="Receipt",
        description="A lumper, scale or fuel receipt the carrier asks to be repaid for.",
        fields=(
            FieldDef(name="kind", description="lumper, scale or fuel"),
            FieldDef(name="amount", description="The amount paid", kind="number"),
            FieldDef(
                name="date",
                description="The date of the receipt",
                kind="date",
                check="date_in_window",
            ),
            FieldDef(name="vendor", description="Who was paid"),
        ),
    ),
)


@cache
def standard_broker() -> Broker:
    """The catalog every broker starts from, and the one a load without a broker runs on."""
    return Broker(
        broker_id=STANDARD_BROKER_ID,
        name="Standard",
        intents=(*_MESSAGE_INTENTS, *_SYSTEM_INTENTS),
        obligations=(_ETA_REQUEST,),
        document_types=_DOCUMENT_TYPES,
    )
