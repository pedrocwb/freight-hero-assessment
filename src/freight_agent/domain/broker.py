"""Brokers as data: the catalog a broker's runs follow.

Owner: card W3-BRK.

A broker is onboarded without code: its intents (what an event can mean), its
obligation kinds (what it waits for, how often it follows up, who gets the case when
the follow-ups are used up), its document types and the documents a stage requires,
and the limits of its runs. Every broker starts from the standard catalog
(`standard_broker`), which carries today's 12 intents, the `eta_request` obligation and
the four document types of `docs/answers.md` 1.2. A client that needs more writes only
what changes (`CatalogOverrides`, read from `sops/clients/<id>/broker.yaml` next to its
SOP sections); `client_broker` applies it.

A catalog is checked whenever a `Broker` is validated (`catalog_problems`): ids are
snake_case and unique, the intents code emits are there with the behaviour code relies
on, requirements name types and kinds of the catalog, and the limits are sane. So a
broker read from a client's file is usable by every run, or refused with every problem
named. `model_copy` skips pydantic validation, so a change goes through `Broker.revised`.
"""

import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from functools import cache
from typing import Any, Final, Literal, Self

from pydantic import model_validator

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

# The intents code gives an event, whatever the broker: a risk change, the ETA request's
# follow-up timer, a processed document (`application/gate.py`). Each obligation's
# follow-up intent is a system intent too, emitted by its timer.
SYSTEM_INTENTS_FROM_CODE: Final[tuple[str, ...]] = (
    Intent.DELAY_RISK_HIGH,
    Intent.DELAY_RISK_LOW,
    Intent.ETA_FOLLOW_UP_DUE,
    Intent.DOCUMENT_PROCESSED,
)
# Message intents code gives too: a photo with no text is a document, and the gate
# corrects an answer the catalog lacks to `other`.
MESSAGE_INTENTS_FROM_CODE: Final[tuple[str, ...]] = (Intent.DOCUMENT, Intent.OTHER)

# Limits: a typo must not let one run loop, spend or text without bound.
MAX_STEPS: Final = 20  # tool rounds per run
MAX_COST_USD: Final = 1.0  # model spend per run
MAX_REJECTIONS: Final = 20
MAX_ATTEMPTS: Final = 10  # requests per obligation
MAX_WAIT_MINUTES: Final = 7 * 24 * 60  # a follow-up or a document deadline: one week
MAX_RATE_WINDOW_MINUTES: Final = 24 * 60

_SNAKE_CASE: Final = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_BROKER_ID: Final = re.compile(r"[a-z0-9][a-z0-9_-]*")
_MAX_ID_LENGTH: Final = 64
# The field kinds each check can read; a check left out reads any kind.
_CHECK_KINDS: Final[dict[str, tuple[FieldKind, ...]]] = {
    "signature": ("bool",),
    "date_in_window": ("date", "datetime"),
    "po_number": ("number", "text"),
    "mc_number": ("number", "text"),
    "seal_number": ("number", "text"),
    "stop_name": ("text",),
}


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
    """The validator's limits and the agent loop's budget for this broker's runs, which
    the run graph checks after every round: `max_steps` rounds of tool calls (then one
    last round must answer), `max_cost_usd` for the agent's rounds (once spent, the next
    round must answer), `max_rejections` refused action calls (over it the run stops and
    a Hero takes the event). The catalog checks bound each (`catalog_problems`); at
    least one round, since the agent acts only through its tools.
    """

    validation: ValidationConfig = ValidationConfig()
    max_steps: int = 8
    max_cost_usd: float = 0.05
    max_rejections: int = 3


class Broker(DomainModel):
    """A broker's whole configuration.

    Validating one runs `catalog_problems` and refuses the broker with every problem
    found; `model_copy` does not validate, so change a broker with `revised`.
    """

    broker_id: str
    name: str
    triage: bool = True  # False: no gate model call; every message goes to the agent
    intents: tuple[IntentDef, ...]
    obligations: tuple[ObligationDef, ...]
    document_types: tuple[DocumentTypeDef, ...] = ()
    documents: tuple[DocumentRequirement, ...] = ()
    limits: BrokerLimits = BrokerLimits()

    @model_validator(mode="after")
    def _check_catalog(self) -> Self:
        problems = catalog_problems(self)
        if problems:
            raise ValueError(f"invalid broker {self.broker_id!r}: " + "; ".join(problems))
        return self

    def revised(self, **changes: Any) -> "Broker":
        """A copy with `changes` (field values, models or their dicts), validated."""
        return Broker.model_validate({**self.model_dump(), **changes})

    def intent(self, intent_id: str) -> IntentDef | None:
        return next((i for i in self.intents if i.intent_id == intent_id), None)

    def obligation(self, kind: str) -> ObligationDef | None:
        return next((o for o in self.obligations if o.kind == kind), None)

    def message_intents(self) -> tuple[IntentDef, ...]:
        """The intents a message can have, in priority order (catalog order breaks ties)."""
        return tuple(sorted((i for i in self.intents if not i.system), key=lambda i: i.priority))

    def system_intents(self) -> tuple[IntentDef, ...]:
        return tuple(i for i in self.intents if i.system)

    def follow_up_intent(self, kind: str) -> str | None:
        """What a due follow-up timer of an obligation of `kind` means; None: unknown kind."""
        obligation = self.obligation(kind)
        return None if obligation is None else _follow_up_of(obligation)

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
    """A new broker: a copy of the standard catalog under its own id."""
    return standard_broker().revised(broker_id=broker_id, name=name)


class CatalogOverrides(DomainModel):
    """How a client's catalog differs from the standard one: what its
    `sops/clients/<id>/broker.yaml` holds, read by `client_broker`.

    `intents`, `obligations` and `document_types` are whole definitions: one whose id is
    in the standard catalog replaces that entry where it stands, a new one comes after
    the standard entries, in the order given. `documents` are the client's document
    requirements (the standard catalog has none). `name`, `triage` and `limits`, when
    given, replace the standard ones. An obligation's follow-up intent is a system
    intent the file lists too: its text reaches the agent's prompt.
    """

    name: str | None = None
    triage: bool | None = None
    intents: tuple[IntentDef, ...] = ()
    obligations: tuple[ObligationDef, ...] = ()
    document_types: tuple[DocumentTypeDef, ...] = ()
    documents: tuple[DocumentRequirement, ...] = ()
    limits: BrokerLimits | None = None

    @model_validator(mode="after")
    def _ids_once(self) -> Self:
        """An id given twice would silently lose one definition when it replaces."""
        problems = [
            *_id_problems("intent id", (i.intent_id for i in self.intents)),
            *_id_problems("obligation kind", (o.kind for o in self.obligations)),
            *_id_problems("document type id", (d.type_id for d in self.document_types)),
        ]
        if problems:
            raise ValueError("; ".join(problems))
        return self


def client_broker(broker_id: str, overrides: CatalogOverrides) -> Broker:
    """The client's catalog: a new broker under the client's id (and its `name`, or the
    id), with `overrides`, validated as a whole (`catalog_problems`)."""
    base = new_broker(broker_id, overrides.name or broker_id)
    changes: dict[str, Any] = {
        "intents": _replaced(base.intents, overrides.intents, lambda i: i.intent_id),
        "obligations": _replaced(base.obligations, overrides.obligations, lambda o: o.kind),
        "document_types": _replaced(
            base.document_types, overrides.document_types, lambda d: d.type_id
        ),
        "documents": (*base.documents, *overrides.documents),
    }
    if overrides.triage is not None:
        changes["triage"] = overrides.triage
    if overrides.limits is not None:
        changes["limits"] = overrides.limits
    return base.revised(**changes)


def _replaced[T](
    base: Sequence[T], overrides: Sequence[T], key: Callable[[T], str]
) -> tuple[T, ...]:
    """`base` with each override in place of the entry of its id, then the new ids."""
    known = {key(entry) for entry in base}
    by_id = {key(entry): entry for entry in overrides if key(entry) in known}
    added = tuple(entry for entry in overrides if key(entry) not in known)
    return (*(by_id.get(key(entry), entry) for entry in base), *added)


# --- catalog validation -------------------------------------------------------------------


def catalog_problems(broker: Broker) -> tuple[str, ...]:
    """What keeps `broker` from working with the code that runs it; empty: nothing.

    `Broker` validation runs it, so a broker that exists was checked. Call it directly
    to lint a catalog built without validation (`model_copy`), such as a draft.
    """
    return (
        *_identity_problems(broker),
        *_intent_problems(broker),
        *_obligation_problems(broker),
        *_document_problems(broker),
        *_limit_problems(broker.limits),
    )


def _follow_up_of(obligation: ObligationDef) -> str:
    return obligation.follow_up_intent or f"{obligation.kind}{FOLLOW_UP_SUFFIX}"


def _blank(text: str) -> bool:
    return not text.strip()


def _id_problems(label: str, ids: Iterable[str]) -> list[str]:
    """Each id must be snake_case and appear once."""
    counts = Counter(ids)
    problems = [
        f"{label} {value!r} must be snake_case (a-z, 0-9 and single underscores, starting"
        f" with a letter, at most {_MAX_ID_LENGTH} characters)"
        for value in counts
        if len(value) > _MAX_ID_LENGTH or not _SNAKE_CASE.fullmatch(value)
    ]
    problems += [f"{label} {value} appears {n} times" for value, n in counts.items() if n > 1]
    return problems


def _between(name: str, value: float, low: float, high: float) -> list[str]:
    if math.isfinite(value) and low <= value <= high:
        return []
    return [f"{name} must be between {low:g} and {high:g}"]


def _positive(name: str, value: float, high: float) -> list[str]:
    """Above 0: a budget of nothing would force the agent's first round to answer, so it
    could never act."""
    if math.isfinite(value) and 0 < value <= high:
        return []
    return [f"{name} must be above 0 and at most {high:g}"]


def _identity_problems(broker: Broker) -> list[str]:
    """The id and the name."""
    problems: list[str] = []
    broker_id = broker.broker_id
    if len(broker_id) > _MAX_ID_LENGTH or not _BROKER_ID.fullmatch(broker_id):
        problems.append(
            f"broker id {broker_id!r} must be lowercase letters, digits, - and _, starting"
            f" with a letter or a digit, at most {_MAX_ID_LENGTH} characters"
        )
    if _blank(broker.name):
        problems.append(f"broker {broker_id} has no name")
    return problems


def _intent_problems(broker: Broker) -> list[str]:
    """Ids, wording, and the intents code emits with the behaviour code relies on: a
    system event always reaches the agent, and `other` is the gate's last resort."""
    problems = _id_problems("intent id", (i.intent_id for i in broker.intents))
    for intent in broker.intents:
        if _blank(intent.title):
            problems.append(f"intent {intent.intent_id} has no title")
        if _blank(intent.description):
            problems.append(f"intent {intent.intent_id} has no description")
    for intent_id in SYSTEM_INTENTS_FROM_CODE:
        found = broker.intent(intent_id)
        if found is None:
            problems.append(f"system intent {intent_id} is missing: code emits it")
        elif not found.system:
            problems.append(f"intent {intent_id} must be a system intent: code emits it")
    follow_ups = {_follow_up_of(o) for o in broker.obligations}
    for intent in broker.system_intents():
        if not intent.must_act or intent.noop_eligible:
            problems.append(
                f"system intent {intent.intent_id} must act (must_act true, noop_eligible"
                " false): code emits it for the agent to handle"
            )
        emitted = intent.intent_id in SYSTEM_INTENTS_FROM_CODE or intent.intent_id in follow_ups
        if not emitted:
            problems.append(
                f"system intent {intent.intent_id}: nothing emits it (no code, no"
                " obligation's follow-up); remove it or add its obligation"
            )
    for intent_id in MESSAGE_INTENTS_FROM_CODE:
        found = broker.intent(intent_id)
        if found is None:
            problems.append(f"intent {intent_id} is missing: the gate gives it from code")
        elif found.system:
            problems.append(
                f"intent {intent_id} must be a message intent: the gate gives it to messages"
            )
    other = broker.intent(Intent.OTHER)
    if other is not None and not other.system:
        problems += [
            f"intent other must come last (priority {other.priority}): {i.intent_id} has"
            f" priority {i.priority}"
            for i in broker.message_intents()
            if i.intent_id != Intent.OTHER and i.priority >= other.priority
        ]
    return problems


def _obligation_problems(broker: Broker) -> list[str]:
    problems = _id_problems("obligation kind", (o.kind for o in broker.obligations))
    following: dict[str, list[str]] = {}  # follow-up intent -> the kinds that use it
    for obligation in broker.obligations:
        following.setdefault(_follow_up_of(obligation), []).append(obligation.kind)
    problems += [
        f"obligations {' and '.join(kinds)} both follow up as {intent_id}"
        for intent_id, kinds in following.items()
        if len(kinds) > 1
    ]
    for obligation in broker.obligations:
        kind = obligation.kind
        if _blank(obligation.title):
            problems.append(f"obligation {kind} has no title")
        if _blank(obligation.description):
            problems.append(f"obligation {kind} has no description")
        numbers = [
            *_between("follow_up_minutes", obligation.follow_up_minutes, 1, MAX_WAIT_MINUTES),
            *_between("max_attempts", obligation.max_attempts, 1, MAX_ATTEMPTS),
        ]
        problems += [f"obligation {kind}: {p}" for p in numbers]
        if not obligation.on_exhausted:
            problems.append(f"obligation {kind}: on_exhausted names nobody")
        for target, n in Counter(obligation.on_exhausted).items():
            if n > 1:
                problems.append(f"obligation {kind}: on_exhausted names {target} twice")
        if following[_follow_up_of(obligation)][0] == kind:  # a shared one is reported once
            problems += _follow_up_problems(broker, obligation)
    eta = broker.obligation(ObligationKind.ETA_REQUEST)
    if eta is None:
        problems.append("obligation eta_request is missing: an SMS asking for an ETA opens it")
    elif _follow_up_of(eta) != Intent.ETA_FOLLOW_UP_DUE:
        problems.append(
            "obligation eta_request must follow up as eta_follow_up_due: the standard SOPs"
            " follow up on it"
        )
    return problems


def _follow_up_problems(broker: Broker, obligation: ObligationDef) -> list[str]:
    """The intent an obligation's timer emits: its own, in the catalog, as a system intent."""
    kind, intent_id = obligation.kind, _follow_up_of(obligation)
    if intent_id in SYSTEM_INTENTS_FROM_CODE:
        if (kind, intent_id) == (ObligationKind.ETA_REQUEST, Intent.ETA_FOLLOW_UP_DUE):
            return []  # checked with the intents code emits
        return [
            f"obligation {kind} cannot follow up as {intent_id}: code emits it for something else"
        ]
    found = broker.intent(intent_id)
    if found is None:
        return [
            f"obligation {kind} follows up as {intent_id}, which is not in the catalog: add it"
            " as a system intent"
        ]
    if not found.system:
        return [
            f"intent {intent_id} must be a system intent: the follow-up timer of {kind} emits it"
        ]
    return []


def _document_problems(broker: Broker) -> list[str]:
    problems = _id_problems("document type id", (d.type_id for d in broker.document_types))
    for doc in broker.document_types:
        where = f"document type {doc.type_id}"
        if _blank(doc.title):
            problems.append(f"{where} has no title")
        if _blank(doc.description):
            problems.append(f"{where} has no description")
        names = _id_problems("field name", (f.name for f in doc.fields))
        problems += [f"{where}: {p}" for p in names]
        for field in doc.fields:
            if _blank(field.description):
                problems.append(f"{where}: field {field.name} has no description")
            kinds = _CHECK_KINDS.get(field.check)
            if kinds is not None and field.kind not in kinds:
                problems.append(
                    f"{where}: field {field.name} has kind {field.kind}, but a {field.check}"
                    f" check needs {' or '.join(kinds)}"
                )
    problems += _requirement_problems(broker)
    return problems


def _requirement_problems(broker: Broker) -> list[str]:
    problems: list[str] = []
    types = {d.type_id for d in broker.document_types}
    kinds = {o.kind for o in broker.obligations}
    for requirement in broker.documents:
        where = f"requirement {requirement.type_id} at {requirement.at_stage}"
        if requirement.type_id not in types:
            problems.append(f"{where}: unknown document type {requirement.type_id}")
        opens = requirement.opens_obligation
        if opens is not None and opens not in kinds:
            problems.append(f"{where}: unknown obligation kind {opens}")
        if requirement.within_minutes is not None:
            if opens is None:
                problems.append(
                    f"{where}: within_minutes needs opens_obligation (the timer belongs to the"
                    " obligation)"
                )
            within = _between("within_minutes", requirement.within_minutes, 1, MAX_WAIT_MINUTES)
            problems += [f"{where}: {p}" for p in within]
    counts = Counter((r.type_id, r.at_stage) for r in broker.documents)
    problems += [
        f"requirement {type_id} at {stage} appears {n} times"
        for (type_id, stage), n in counts.items()
        if n > 1
    ]
    return problems


def _limit_problems(limits: BrokerLimits) -> list[str]:
    problems = [
        *_between("limits.max_steps", limits.max_steps, 1, MAX_STEPS),
        *_positive("limits.max_cost_usd", limits.max_cost_usd, MAX_COST_USD),
        *_between("limits.max_rejections", limits.max_rejections, 0, MAX_REJECTIONS),
    ]
    validation = limits.validation
    if validation.sms_max_per_window < 1:
        problems.append(
            "limits.validation.sms_max_per_window must be at least 1, not"
            f" {validation.sms_max_per_window}"
        )
    problems += _between(
        "limits.validation.sms_window_minutes",
        validation.sms_window_minutes,
        1,
        MAX_RATE_WINDOW_MINUTES,
    )
    deferral = validation.deferral
    if min(deferral.max_minutes, deferral.max_per_obligation, deferral.grace_minutes) < 0:
        problems.append("limits.validation.deferral values must not be negative")
    for kind, cap in validation.max_attempts.items():
        problems += _between(f"limits.validation.max_attempts[{kind}]", cap, 1, MAX_ATTEMPTS)
    return problems


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
            FieldDef(
                name="receiver",
                description="The receiving facility (consignee) name, not the person who signed",
                check="stop_name",
            ),
            FieldDef(name="po_numbers", description="The PO numbers listed", check="po_number"),
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
