"""Properties of broker catalogs over arbitrary inputs.

Owner: card W3-BRK. A client's catalog built from generated overrides passes the checks
and comes back equal from JSON (what the API returns); breaking it is always refused.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from freight_agent.domain.broker import (
    Broker,
    CatalogOverrides,
    DocumentRequirement,
    EscalationTarget,
    IntentDef,
    ObligationDef,
    catalog_problems,
    client_broker,
    standard_broker,
)
from freight_agent.domain.load import Stage

pytestmark = pytest.mark.property


# --- catalogs ------------------------------------------------------------------------------

_TAKEN = frozenset(i.intent_id for i in standard_broker().intents)
_FOLLOW_UP = "_follow_up_due"
_TARGETS: tuple[EscalationTarget, ...] = ("dispatcher_email", "hero_task", "broker_slack")
snake_ids = st.from_regex(r"[a-z][a-z0-9]{0,8}(_[a-z0-9]{1,5}){0,2}", fullmatch=True)
texts = st.text(min_size=1, max_size=30).filter(lambda s: bool(s.strip()))


@st.composite
def brokers(draw: st.DrawFn) -> Broker:
    """A client's catalog: the standard one plus its own intents, obligations (each with
    its follow-up intent) and requirements."""
    intent_ids = draw(
        st.lists(
            snake_ids.filter(lambda s: s not in _TAKEN and not s.endswith(_FOLLOW_UP)),
            unique=True,
            max_size=4,
        )
    )
    kinds = draw(
        st.lists(
            snake_ids.filter(lambda s: s != "eta_request" and f"{s}{_FOLLOW_UP}" not in _TAKEN),
            unique=True,
            max_size=3,
        )
    )
    intents = tuple(
        IntentDef(
            intent_id=intent_id,
            title=draw(texts),
            description=draw(texts),
            examples=tuple(draw(st.lists(texts, max_size=2))),
            priority=draw(st.integers(-1000, 999)),
            must_act=draw(st.booleans()),
            noop_eligible=draw(st.booleans()),
        )
        for intent_id in intent_ids
    )
    obligations = tuple(
        ObligationDef(
            kind=kind,
            title=draw(texts),
            description=draw(texts),
            follow_up_minutes=draw(st.integers(1, 10080)),
            max_attempts=draw(st.integers(1, 10)),
            on_exhausted=tuple(
                draw(
                    st.lists(
                        st.sampled_from(_TARGETS),
                        min_size=1,
                        unique=True,
                    )
                )
            ),
        )
        for kind in kinds
    )
    stages = draw(st.lists(st.sampled_from(Stage), unique=True, min_size=len(kinds)))
    requirements = tuple(
        DocumentRequirement(
            type_id="pod",
            at_stage=stage,
            within_minutes=draw(st.none() | st.integers(1, 10080)),
            opens_obligation=kind,
        )
        for kind, stage in zip(kinds, stages, strict=False)
    )
    follow_ups = tuple(
        IntentDef(
            intent_id=f"{kind}{_FOLLOW_UP}",
            title=f"{kind} follow-up due",
            description=f"The follow-up timer of an open {kind} obligation is due.",
            system=True,
        )
        for kind in kinds
    )
    overrides = CatalogOverrides(
        name="Acme",
        intents=(*intents, *follow_ups),
        obligations=obligations,
        documents=requirements,
        triage=draw(st.booleans()),
    )
    return client_broker("acme", overrides)


@given(brokers())
def test_a_valid_catalog_comes_back_equal_from_json(broker: Broker) -> None:
    assert catalog_problems(broker) == ()
    assert Broker.model_validate_json(broker.model_dump_json()) == broker
    assert Broker.model_validate(broker.model_dump(mode="json")) == broker


@given(brokers(), st.data())
def test_a_duplicated_intent_is_always_refused(broker: Broker, data: st.DataObject) -> None:
    twice = data.draw(st.sampled_from(broker.intents))

    problems = catalog_problems(broker.model_copy(update={"intents": (*broker.intents, twice)}))

    assert f"intent id {twice.intent_id} appears 2 times" in problems


@given(brokers(), st.data())
def test_an_obligation_without_its_follow_up_intent_is_always_refused(
    broker: Broker, data: st.DataObject
) -> None:
    obligation = data.draw(st.sampled_from(broker.obligations[1:] or broker.obligations))
    follow_up = broker.follow_up_intent(obligation.kind)
    intents = tuple(i for i in broker.intents if i.intent_id != follow_up)

    problems = catalog_problems(broker.model_copy(update={"intents": intents}))

    assert any(str(follow_up) in p for p in problems), problems
