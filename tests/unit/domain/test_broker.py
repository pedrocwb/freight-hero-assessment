"""The broker catalog: the standard broker, its lookups, a client's overrides and the
validation.

Owner: card W3-BRK. Properties over generated catalogs are in
`tests/property/test_broker_properties.py`.
"""

import math
from typing import Any

import pytest
from pydantic import ValidationError

from freight_agent.application.prompts import GATE_SYSTEM_PROMPT
from freight_agent.domain.broker import (
    MAX_COST_USD,
    MAX_STEPS,
    STANDARD_BROKER_ID,
    SYSTEM_INTENTS_FROM_CODE,
    Broker,
    BrokerLimits,
    CatalogOverrides,
    DocumentRequirement,
    DocumentTypeDef,
    FieldDef,
    IntentDef,
    ObligationDef,
    catalog_problems,
    client_broker,
    new_broker,
    standard_broker,
)
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Stage
from freight_agent.domain.obligations import DeferralPolicy, ObligationKind
from freight_agent.domain.validation import ValidationConfig

# What `domain.intents.MUST_ACT` and `NOOP_ELIGIBLE` held before brokers were data.
MUST_ACT_BEFORE = {
    Intent.ETA_PROVIDED,
    Intent.ETA_DEFERRED,
    Intent.QUESTION,
    Intent.DOCUMENT,
    Intent.DELAY_RISK_HIGH,
    Intent.DELAY_RISK_LOW,
    Intent.ETA_FOLLOW_UP_DUE,
    Intent.DRIVER_DEPARTED,
}
# The gate prompt's order: "pick the first that applies in this order".
GATE_ORDER = (
    Intent.ETA_PROVIDED,
    Intent.ETA_DEFERRED,
    Intent.QUESTION,
    Intent.DRIVER_DEPARTED,
    Intent.DOCUMENT,
    Intent.STATUS_UPDATE,
    Intent.ACKNOWLEDGEMENT,
    Intent.OTHER,
)

POD = ObligationDef(
    kind="pod",
    title="Proof of delivery",
    description="The signed POD, due after the truck arrives at the receiver.",
)
POD_DUE = IntentDef(
    intent_id="pod_follow_up_due",
    title="POD follow-up due",
    description="The follow-up timer of an open POD obligation is due.",
    system=True,
)


def _with_pod(**pod: Any) -> Broker:
    """The standard catalog plus a `pod` obligation and its follow-up intent: valid."""
    broker = standard_broker()
    return broker.revised(
        obligations=(*broker.obligations, POD.model_copy(update=pod)),
        intents=(*broker.intents, POD_DUE),
    )


def _changed(**changes: Any) -> dict[str, Any]:
    """The standard catalog's fields with `changes`, ready for `Broker.model_validate`."""
    return {**standard_broker().model_dump(), **changes}


def _problems(**changes: Any) -> tuple[str, ...]:
    """What the catalog checks find in the standard catalog with `changes` (unvalidated)."""
    return catalog_problems(standard_broker().model_copy(update=changes))


def _intents(**replaced: IntentDef | None) -> tuple[IntentDef, ...]:
    """The standard intents with some replaced (an `IntentDef`) or removed (None)."""
    kept: list[IntentDef] = []
    for intent in standard_broker().intents:
        if intent.intent_id not in replaced:
            kept.append(intent)
        elif (new := replaced[intent.intent_id]) is not None:
            kept.append(new)
    return tuple(kept)


def _intent(standard_id: str, /, **update: Any) -> IntentDef:
    """A standard intent with `update` (which may change its id)."""
    found = standard_broker().intent(standard_id)
    assert found is not None
    return found.model_copy(update=update)


def _with_pod_obligation(pod: ObligationDef) -> dict[str, Any]:
    """The changes that add `pod` and its follow-up intent to the standard catalog."""
    return {"obligations": (*standard_broker().obligations, pod), "intents": (*_intents(), POD_DUE)}


@pytest.mark.unit
def test_the_standard_catalog_has_every_standard_intent_once() -> None:
    broker = standard_broker()

    ids = [i.intent_id for i in broker.intents]
    assert sorted(ids) == sorted(Intent)
    assert broker.broker_id == STANDARD_BROKER_ID


@pytest.mark.unit
def test_message_intents_follow_the_gate_prompt_order_and_wording() -> None:
    broker = standard_broker()

    assert tuple(i.intent_id for i in broker.message_intents()) == GATE_ORDER
    for intent in broker.message_intents():
        assert f"- {intent.intent_id}: {intent.description}" in GATE_SYSTEM_PROMPT


@pytest.mark.unit
def test_system_intents_come_from_code() -> None:
    assert {i.intent_id for i in standard_broker().system_intents()} == {
        Intent.DELAY_RISK_HIGH,
        Intent.DELAY_RISK_LOW,
        Intent.ETA_FOLLOW_UP_DUE,
        Intent.DOCUMENT_PROCESSED,
    }
    assert set(SYSTEM_INTENTS_FROM_CODE) == {
        i.intent_id for i in standard_broker().system_intents()
    }


@pytest.mark.unit
def test_must_act_and_noop_eligible_are_what_the_constants_were() -> None:
    broker = standard_broker()

    assert {i for i in Intent if broker.must_act(i)} == {
        *MUST_ACT_BEFORE,
        Intent.DOCUMENT_PROCESSED,
    }
    assert {i for i in Intent if broker.noop_eligible(i)} == {Intent.ACKNOWLEDGEMENT}
    assert not broker.must_act("no_such_intent")
    assert not broker.noop_eligible("no_such_intent")


@pytest.mark.unit
def test_the_eta_request_follows_up_as_eta_follow_up_due() -> None:
    broker = standard_broker()

    assert broker.follow_up_intent(ObligationKind.ETA_REQUEST) == Intent.ETA_FOLLOW_UP_DUE
    found = broker.obligation_for_follow_up(Intent.ETA_FOLLOW_UP_DUE)
    assert found is not None and found.kind == ObligationKind.ETA_REQUEST
    assert broker.follow_up_intent("no_such_kind") is None
    assert broker.obligation_for_follow_up(Intent.QUESTION) is None


@pytest.mark.unit
def test_a_kind_without_its_own_follow_up_intent_gets_the_default_name() -> None:
    broker = _with_pod()

    assert broker.follow_up_intent("pod") == "pod_follow_up_due"
    assert broker.obligation("pod") == POD
    found = broker.obligation_for_follow_up("pod_follow_up_due")
    assert found is not None and found.kind == "pod"


@pytest.mark.unit
def test_the_validation_config_takes_each_kind_s_attempt_cap() -> None:
    broker = standard_broker()
    with_pod = _with_pod(max_attempts=3)

    config = with_pod.validation_config()

    assert config.max_attempts == {"eta_request": 2, "pod": 3}
    assert config.attempt_cap("pod") == 3
    assert config.attempt_cap("other") is None
    assert config.sms_window_minutes == broker.limits.validation.sms_window_minutes


@pytest.mark.unit
def test_the_standard_catalog_has_the_four_document_types_and_no_requirement() -> None:
    broker = standard_broker()

    assert [d.type_id for d in broker.document_types] == [
        "bol",
        "pod",
        "rate_confirmation",
        "receipt",
    ]
    pod = broker.document_type("pod")
    assert pod is not None
    checks = {f.name: f.check for f in pod.fields}
    assert checks["po_numbers"] == "po_number"  # a POD with the wrong PO is caught
    assert (checks["receiver"], checks["signed"]) == ("stop_name", "signature")
    assert broker.document_type("nope") is None
    assert broker.requirements_at(Stage.AT_DELIVERY) == ()


@pytest.mark.unit
def test_requirements_are_listed_by_stage() -> None:
    pod = DocumentRequirement(
        type_id="pod", at_stage=Stage.AT_DELIVERY, within_minutes=120, opens_obligation="pod"
    )
    broker = _with_pod().revised(documents=(pod,))

    assert broker.requirements_at(Stage.AT_DELIVERY) == (pod,)
    assert broker.requirements_at(Stage.AT_PICKUP) == ()


@pytest.mark.unit
def test_a_new_broker_copies_the_standard_catalog_under_its_own_id() -> None:
    broker = new_broker("acme", "Acme Logistics")

    assert (broker.broker_id, broker.name) == ("acme", "Acme Logistics")
    assert broker.intents == standard_broker().intents
    assert broker.obligations == standard_broker().obligations


# --- a client's overrides ---------------------------------------------------------------

DETENTION = IntentDef(
    intent_id="detention_request",
    title="Detention request",
    description="The truck waited more than 2 hours at a facility.",
    priority=55,
)
POD_AT_DELIVERY = DocumentRequirement(
    type_id="pod", at_stage=Stage.AT_DELIVERY, within_minutes=120, opens_obligation="pod"
)


@pytest.mark.unit
def test_no_overrides_give_the_standard_catalog_under_the_client_s_id() -> None:
    broker = client_broker("acme", CatalogOverrides())

    assert (broker.broker_id, broker.name) == ("acme", "acme")
    assert broker.revised(broker_id="standard", name="Standard") == standard_broker()


@pytest.mark.unit
def test_a_standard_id_replaces_its_entry_in_place_and_a_new_one_comes_after() -> None:
    faster = (
        standard_broker()
        .obligations[0]
        .model_copy(update={"follow_up_minutes": 20, "max_attempts": 3})
    )
    overrides = CatalogOverrides(
        name="Acme Freight",
        intents=(DETENTION, POD_DUE),
        obligations=(POD, faster),
        documents=(POD_AT_DELIVERY,),
    )

    broker = client_broker("acme", overrides)

    assert broker.name == "Acme Freight"
    assert broker.obligations == (faster, POD)  # eta_request where it stood, then pod
    standard = standard_broker().intents
    assert broker.intents == (*standard, DETENTION, POD_DUE)
    assert [i.intent_id for i in broker.message_intents()][4:6] == [
        "document",
        "detention_request",
    ]  # by priority: 55 sits between document (50) and status_update (60)
    assert broker.documents == (POD_AT_DELIVERY,)
    assert broker.document_types == standard_broker().document_types
    assert broker.validation_config().attempt_cap(ObligationKind.ETA_REQUEST) == 3


@pytest.mark.unit
def test_triage_and_limits_replace_the_standard_ones_when_given() -> None:
    limits = BrokerLimits(max_steps=2)

    broker = client_broker("acme", CatalogOverrides(triage=False, limits=limits))

    assert (broker.triage, broker.limits) == (False, limits)


@pytest.mark.unit
def test_the_overridden_catalog_is_validated_as_a_whole() -> None:
    with pytest.raises(ValidationError, match="pod_follow_up_due, which is not in the catalog"):
        client_broker("acme", CatalogOverrides(obligations=(POD,)))
    with pytest.raises(ValidationError, match="broker id"):
        client_broker("Acme Corp", CatalogOverrides())


@pytest.mark.unit
def test_an_id_given_twice_in_the_overrides_is_refused() -> None:
    with pytest.raises(ValidationError, match="intent id detention_request appears 2 times"):
        CatalogOverrides(intents=(DETENTION, DETENTION))
    with pytest.raises(ValidationError, match="obligation kind pod appears 2 times"):
        CatalogOverrides(obligations=(POD, POD))


# --- catalog validation -------------------------------------------------------------------


@pytest.mark.unit
def test_the_standard_catalog_and_a_new_broker_are_valid() -> None:
    assert catalog_problems(standard_broker()) == ()
    assert catalog_problems(new_broker("acme", "Acme")) == ()
    assert catalog_problems(_with_pod()) == ()


@pytest.mark.unit
def test_a_broker_survives_a_json_round_trip() -> None:
    """What the API returns comes back equal."""
    broker = _with_pod(max_attempts=3)

    assert Broker.model_validate_json(broker.model_dump_json()) == broker


@pytest.mark.unit
def test_validation_runs_on_every_way_in_but_model_copy() -> None:
    """`model_copy` skips the checks (pydantic's design): change a broker with `revised`."""
    with pytest.raises(ValidationError, match="appears 2 times"):
        Broker.model_validate(_changed(intents=(*standard_broker().intents, POD_DUE, POD_DUE)))
    with pytest.raises(ValidationError, match="must be snake_case"):
        standard_broker().revised(intents=_intents(other=_intent("other", intent_id="Other")))
    with pytest.raises(ValidationError, match="broker id"):
        new_broker("Acme Corp", "Acme")
    unchecked = standard_broker().model_copy(update={"name": " "})
    assert catalog_problems(unchecked) == ("broker standard has no name",)


@pytest.mark.unit
def test_every_problem_is_reported_at_once() -> None:
    with pytest.raises(ValidationError) as caught:
        Broker.model_validate(_changed(name="", limits=BrokerLimits(max_steps=-1)))

    message = str(caught.value)
    assert "broker standard has no name" in message
    assert "limits.max_steps must be between 1 and 20" in message


@pytest.mark.unit
@pytest.mark.parametrize("broker_id", ["", "Acme", "acme corp", "-acme", "a/b", "x" * 65])
def test_a_broker_id_is_lowercase_letters_digits_dashes_and_underscores(broker_id: str) -> None:
    problems = _problems(broker_id=broker_id)

    assert len(problems) == 1 and "broker id" in problems[0], problems


@pytest.mark.unit
@pytest.mark.parametrize("broker_id", ["acme", "brk_0087", "north-line", "7eleven"])
def test_ordinary_broker_ids_are_accepted(broker_id: str) -> None:
    assert _problems(broker_id=broker_id) == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad", ["Detention", "detention-request", "1st_call", "_x", "x_", "a__b", "", "x" * 65]
)
def test_intent_ids_kinds_type_ids_and_field_names_are_snake_case(bad: str) -> None:
    detention = IntentDef(intent_id=bad, title="Detention", description="the truck waits.")
    bad_type = DocumentTypeDef(type_id=bad, title="T", description="d", fields=())
    field = FieldDef(name=bad, description="d")
    custom = DocumentTypeDef(type_id="custom", title="C", description="d", fields=(field,))
    pod = POD.model_copy(update={"kind": bad, "follow_up_intent": "pod_follow_up_due"})

    assert any("snake_case" in p for p in _problems(intents=(*_intents(), detention)))
    assert any("snake_case" in p for p in _problems(document_types=(bad_type,)))
    assert any("snake_case" in p for p in _problems(document_types=(custom,)))
    assert any("snake_case" in p for p in _problems(**_with_pod_obligation(pod)))


@pytest.mark.unit
def test_ids_must_be_unique_in_each_list() -> None:
    standard = standard_broker()
    bol = standard.document_types[0]
    signed_twice = bol.model_copy(update={"fields": (*bol.fields, bol.fields[-1])})

    assert _problems(intents=(*standard.intents, standard.intents[0])) == (
        "intent id eta_provided appears 2 times",
    )
    assert "obligation kind eta_request appears 2 times" in _problems(
        obligations=(*standard.obligations, *standard.obligations)
    )
    assert _problems(document_types=(*standard.document_types, bol)) == (
        "document type id bol appears 2 times",
    )
    assert _problems(document_types=(signed_twice,)) == (
        "document type bol: field name signed appears 2 times",
    )


@pytest.mark.unit
def test_names_and_descriptions_are_never_blank() -> None:
    blank = _intent("question", title=" ", description="")
    pod = POD.model_copy(update={"title": "", "description": " "})
    doc = DocumentTypeDef(
        type_id="custom", title="", description="", fields=(FieldDef(name="x", description=""),)
    )

    assert _problems(intents=_intents(question=blank)) == (
        "intent question has no title",
        "intent question has no description",
    )
    assert _problems(**_with_pod_obligation(pod)) == (
        "obligation pod has no title",
        "obligation pod has no description",
    )
    assert _problems(document_types=(doc,)) == (
        "document type custom has no title",
        "document type custom has no description",
        "document type custom: field x has no description",
    )


@pytest.mark.unit
@pytest.mark.parametrize("intent_id", SYSTEM_INTENTS_FROM_CODE)
def test_the_system_intents_code_emits_cannot_be_removed(intent_id: str) -> None:
    problems = _problems(intents=_intents(**{intent_id: None}))

    assert any(intent_id in p and "missing" in p for p in problems), problems


@pytest.mark.unit
@pytest.mark.parametrize("intent_id", SYSTEM_INTENTS_FROM_CODE)
@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"system": False}, "must be a system intent"),
        ({"must_act": False}, "must act"),
        ({"noop_eligible": True}, "must act"),
    ],
)
def test_the_system_intents_code_emits_keep_their_behaviour(
    intent_id: str, change: dict[str, Any], expected: str
) -> None:
    problems = _problems(intents=_intents(**{intent_id: _intent(intent_id, **change)}))

    assert any(intent_id in p and expected in p for p in problems), problems


@pytest.mark.unit
def test_a_system_intent_s_wording_may_change() -> None:
    reworded = _intent(Intent.DELAY_RISK_HIGH, title="Late truck", description="Running late.")

    assert _problems(intents=_intents(delay_risk_high=reworded)) == ()


@pytest.mark.unit
@pytest.mark.parametrize("intent_id", [Intent.DOCUMENT, Intent.OTHER])
def test_the_message_intents_code_gives_cannot_be_removed_or_made_system(intent_id: str) -> None:
    removed = _problems(intents=_intents(**{intent_id: None}))
    system = _problems(intents=_intents(**{intent_id: _intent(intent_id, system=True)}))

    assert any(intent_id in p and "missing" in p for p in removed), removed
    assert any(intent_id in p and "message intent" in p for p in system), system


@pytest.mark.unit
def test_other_stays_the_last_message_intent() -> None:
    late = IntentDef(intent_id="late", title="Late", description="d", priority=1000)
    early = late.model_copy(update={"priority": 999})

    assert _problems(intents=(*_intents(), late)) == (
        "intent other must come last (priority 1000): late has priority 1000",
    )
    assert _problems(intents=(*_intents(), early)) == ()


@pytest.mark.unit
def test_an_obligation_s_follow_up_intent_must_be_in_the_catalog_as_a_system_intent() -> None:
    obligations = (*standard_broker().obligations, POD)
    as_message = POD_DUE.model_copy(update={"system": False})

    assert _problems(obligations=obligations) == (
        "obligation pod follows up as pod_follow_up_due, which is not in the catalog:"
        " add it as a system intent",
    )
    assert _problems(obligations=obligations, intents=(*_intents(), as_message)) == (
        "intent pod_follow_up_due must be a system intent: the follow-up timer of pod emits it",
    )


@pytest.mark.unit
def test_a_system_intent_nothing_emits_is_refused() -> None:
    assert _problems(intents=(*_intents(), POD_DUE)) == (
        "system intent pod_follow_up_due: nothing emits it (no code, no obligation's"
        " follow-up); remove it or add its obligation",
    )


@pytest.mark.unit
def test_two_obligations_cannot_share_a_follow_up_intent() -> None:
    second = POD.model_copy(update={"kind": "pod_copy", "follow_up_intent": "pod_follow_up_due"})
    changes = _with_pod_obligation(POD)

    problems = _problems(**{**changes, "obligations": (*changes["obligations"], second)})

    assert problems == ("obligations pod and pod_copy both follow up as pod_follow_up_due",)


@pytest.mark.unit
def test_an_obligation_cannot_follow_up_as_an_intent_code_emits_for_something_else() -> None:
    hijack = POD.model_copy(update={"follow_up_intent": Intent.DELAY_RISK_HIGH})

    problems = _problems(obligations=(*standard_broker().obligations, hijack))

    assert problems == (
        "obligation pod cannot follow up as delay_risk_high: code emits it for something else",
    )


@pytest.mark.unit
def test_the_eta_request_stays_with_its_follow_up_intent() -> None:
    eta = standard_broker().obligations[0]
    renamed = eta.model_copy(update={"follow_up_intent": ""})
    due = IntentDef(intent_id="eta_request_follow_up_due", title="t", description="d", system=True)

    assert _problems(obligations=()) == (
        "obligation eta_request is missing: an SMS asking for an ETA opens it",
    )
    assert _problems(obligations=(renamed,), intents=(*_intents(), due)) == (
        "obligation eta_request must follow up as eta_follow_up_due: the standard SOPs follow"
        " up on it",
    )


@pytest.mark.unit
def test_a_broker_may_tune_the_eta_request() -> None:
    """Acme: every 20 minutes, at most 3 SMS (sops/clients/acme/broker.yaml)."""
    eta = standard_broker().obligations[0]
    acme = eta.model_copy(update={"follow_up_minutes": 20, "max_attempts": 3})

    broker = new_broker("acme", "Acme").revised(obligations=(acme,))

    assert broker.validation_config().attempt_cap(ObligationKind.ETA_REQUEST) == 3


@pytest.mark.unit
@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"follow_up_minutes": 0}, "follow_up_minutes must be between 1 and 10080"),
        ({"follow_up_minutes": 10081}, "follow_up_minutes must be between 1 and 10080"),
        ({"max_attempts": 0}, "max_attempts must be between 1 and 10"),
        ({"max_attempts": 11}, "max_attempts must be between 1 and 10"),
        ({"on_exhausted": ()}, "on_exhausted names nobody"),
        ({"on_exhausted": ("hero_task", "hero_task")}, "on_exhausted names hero_task twice"),
    ],
)
def test_an_obligation_s_numbers_and_escalation_are_sane(
    change: dict[str, Any], expected: str
) -> None:
    problems = _problems(**_with_pod_obligation(POD.model_copy(update=change)))

    assert problems == (f"obligation pod: {expected}",)


@pytest.mark.unit
def test_a_field_check_fits_the_field_s_kind() -> None:
    fields = (
        FieldDef(name="signed", description="d", kind="text", check="signature"),
        FieldDef(name="when", description="d", kind="bool", check="date_in_window"),
        FieldDef(name="po", description="d", kind="date", check="po_number"),
        FieldDef(name="ok", description="d", kind="number", check="mc_number"),
    )
    doc = DocumentTypeDef(type_id="custom", title="C", description="d", fields=fields)

    assert _problems(document_types=(doc,)) == (
        "document type custom: field signed has kind text, but a signature check needs bool",
        "document type custom: field when has kind bool, but a date_in_window check needs"
        " date or datetime",
        "document type custom: field po has kind date, but a po_number check needs number or text",
    )


@pytest.mark.unit
def test_a_requirement_names_a_document_type_and_an_obligation_of_the_catalog() -> None:
    receipt = DocumentRequirement(type_id="receipt", at_stage=Stage.AT_PICKUP)
    unknown = DocumentRequirement(
        type_id="invoice", at_stage=Stage.DELIVERED, opens_obligation="invoice"
    )

    assert _problems(documents=(receipt,)) == ()
    assert _problems(documents=(unknown,)) == (
        "requirement invoice at delivered: unknown document type invoice",
        "requirement invoice at delivered: unknown obligation kind invoice",
    )


@pytest.mark.unit
def test_a_requirement_deadline_needs_an_obligation_and_a_sane_number() -> None:
    no_obligation = DocumentRequirement(
        type_id="pod", at_stage=Stage.AT_DELIVERY, within_minutes=120
    )
    zero = DocumentRequirement(
        type_id="pod", at_stage=Stage.AT_DELIVERY, within_minutes=0, opens_obligation="pod"
    )
    broker = _with_pod()

    assert catalog_problems(broker.model_copy(update={"documents": (no_obligation,)})) == (
        "requirement pod at at_delivery: within_minutes needs opens_obligation (the timer"
        " belongs to the obligation)",
    )
    assert catalog_problems(broker.model_copy(update={"documents": (zero,)})) == (
        "requirement pod at at_delivery: within_minutes must be between 1 and 10080",
    )


@pytest.mark.unit
def test_a_document_is_required_once_per_stage() -> None:
    receipt = DocumentRequirement(type_id="receipt", at_stage=Stage.AT_PICKUP)

    assert _problems(documents=(receipt, receipt)) == (
        "requirement receipt at at_pickup appears 2 times",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        (BrokerLimits(max_steps=0), f"limits.max_steps must be between 1 and {MAX_STEPS}"),
        (BrokerLimits(max_steps=-1), f"limits.max_steps must be between 1 and {MAX_STEPS}"),
        (BrokerLimits(max_steps=MAX_STEPS + 1), "limits.max_steps must be between 1"),
        (BrokerLimits(max_cost_usd=-0.01), "limits.max_cost_usd must be above 0 and"),
        (BrokerLimits(max_cost_usd=0.0), "limits.max_cost_usd must be above 0 and"),
        (BrokerLimits(max_cost_usd=MAX_COST_USD * 2), "limits.max_cost_usd must be above 0"),
        (BrokerLimits(max_cost_usd=math.inf), "limits.max_cost_usd must be above 0"),
        (BrokerLimits(max_cost_usd=math.nan), "limits.max_cost_usd must be above 0"),
        (BrokerLimits(max_rejections=-1), "limits.max_rejections must be between 0 and 20"),
        (
            BrokerLimits(validation=ValidationConfig(sms_max_per_window=0)),
            "limits.validation.sms_max_per_window must be at least 1",
        ),
        (
            BrokerLimits(validation=ValidationConfig(sms_window_minutes=0)),
            "limits.validation.sms_window_minutes must be between 1 and 1440",
        ),
        (
            BrokerLimits(validation=ValidationConfig(max_attempts={"eta_request": 0})),
            "limits.validation.max_attempts[eta_request] must be between 1 and 10",
        ),
        (
            BrokerLimits(validation=ValidationConfig(deferral=DeferralPolicy(max_minutes=-1))),
            "limits.validation.deferral values must not be negative",
        ),
    ],
)
def test_limits_are_sane(limits: BrokerLimits, expected: str) -> None:
    problems = _problems(limits=limits)

    assert len(problems) == 1 and problems[0].startswith(expected), problems


@pytest.mark.unit
def test_the_strictest_limits_are_allowed() -> None:
    """One round is the least, since the agent acts only through its tools, and some spend
    (none would force the first round to answer); no refused call is accepted."""
    strict = BrokerLimits(max_steps=1, max_cost_usd=0.0001, max_rejections=0)

    assert _problems(limits=strict) == ()


@pytest.mark.unit
def test_revised_keeps_what_it_is_not_told_to_change() -> None:
    broker = new_broker("acme", "Acme")

    renamed = broker.revised(name="Acme Freight", triage=False)

    assert (renamed.name, renamed.triage) == ("Acme Freight", False)
    assert renamed.intents == broker.intents and renamed.limits == broker.limits
