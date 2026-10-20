"""The broker catalog: the standard broker, its lookups and the canary choice.

Owner: card W3-BRK (C0 wrote the tests of the shape it shipped; BRK adds the catalog
validation).
"""

import pytest

from freight_agent.application.prompts import GATE_SYSTEM_PROMPT
from freight_agent.domain.broker import (
    STANDARD_BROKER_ID,
    Broker,
    DocumentRequirement,
    ObligationDef,
    Rollout,
    choose_version,
    new_broker,
    standard_broker,
)
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Stage
from freight_agent.domain.obligations import ObligationKind

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


def _versions(percent: int) -> tuple[Broker, Broker]:
    previous = new_broker("acme", "Acme")
    current = previous.model_copy(
        update={"config_version": 2, "rollout": Rollout(percent=percent, previous_version=1)}
    )
    return current, previous


@pytest.mark.unit
def test_the_standard_catalog_has_every_standard_intent_once() -> None:
    broker = standard_broker()

    ids = [i.intent_id for i in broker.intents]
    assert sorted(ids) == sorted(Intent)
    assert broker.broker_id == STANDARD_BROKER_ID
    assert broker.config_version == 1


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
    pod = ObligationDef(kind="pod", title="POD", description="The signed POD.")
    broker = standard_broker().model_copy(update={"obligations": (pod,)})

    assert broker.follow_up_intent("pod") == "pod_follow_up_due"
    assert broker.obligation("pod") == pod


@pytest.mark.unit
def test_the_validation_config_takes_each_kind_s_attempt_cap() -> None:
    pod = ObligationDef(kind="pod", title="POD", description="The signed POD.", max_attempts=3)
    broker = standard_broker()
    with_pod = broker.model_copy(update={"obligations": (*broker.obligations, pod)})

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
    pod = DocumentRequirement(type_id="pod", at_stage=Stage.AT_DELIVERY, within_minutes=120)
    broker = standard_broker().model_copy(update={"documents": (pod,)})

    assert broker.requirements_at(Stage.AT_DELIVERY) == (pod,)
    assert broker.requirements_at(Stage.AT_PICKUP) == ()


@pytest.mark.unit
def test_a_new_broker_copies_the_standard_catalog_under_its_own_id() -> None:
    broker = new_broker("acme", "Acme Logistics")

    assert (broker.broker_id, broker.name, broker.config_version) == ("acme", "Acme Logistics", 1)
    assert broker.intents == standard_broker().intents
    assert broker.obligations == standard_broker().obligations


@pytest.mark.unit
def test_full_rollout_and_no_previous_version_give_the_current_one() -> None:
    current, previous = _versions(100)

    assert choose_version(current, previous, "481207") is current
    assert choose_version(current, None, "481207") is current


@pytest.mark.unit
def test_a_rollback_gives_every_load_the_previous_version() -> None:
    current, previous = _versions(0)

    assert all(choose_version(current, previous, f"load-{i}") is previous for i in range(50))


@pytest.mark.unit
def test_a_canary_splits_the_loads_and_keeps_each_load_on_its_version() -> None:
    current, previous = _versions(30)
    loads = [f"load-{i}" for i in range(400)]

    first = [choose_version(current, previous, load) for load in loads]
    again = [choose_version(current, previous, load) for load in loads]

    assert first == again
    on_current = sum(1 for version in first if version is current)
    assert 80 < on_current < 160  # about 30% of 400
