"""Properties of the broker canary and of catalogs over arbitrary inputs.

Owner: card W3-BRK. The canary (`choose_version`) puts a load on the current version
when the sha256 of its id falls in the first `percent` buckets of 100, so the choice
depends on the load id and the percent alone. A catalog that passes the checks comes
back equal from JSON (what Postgres stores and the API returns).
"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from freight_agent.domain.broker import (
    Broker,
    DocumentRequirement,
    EscalationTarget,
    IntentDef,
    ObligationDef,
    Rollout,
    catalog_problems,
    choose_version,
    new_broker,
    standard_broker,
    with_follow_up_intents,
)
from freight_agent.domain.load import Stage

pytestmark = pytest.mark.property

load_ids = st.text(min_size=1, max_size=40)
percents = st.integers(min_value=0, max_value=100)
SAMPLE = 4000  # loads per share check: the share's spread is under 0.8 points


def _pair(percent: int, *, name: str = "Acme v2") -> tuple[Broker, Broker]:
    previous = new_broker("acme", "Acme")
    current = previous.revised(
        name=name, config_version=2, rollout=Rollout(percent=percent, previous_version=1)
    )
    return current, previous


def _on_current(percent: int, load_id: str) -> bool:
    current, previous = _pair(percent)
    return choose_version(current, previous, load_id) is current


@given(percents, load_ids)
def test_the_choice_is_one_of_the_two_versions_and_the_same_every_time(
    percent: int, load_id: str
) -> None:
    current, previous = _pair(percent)

    first = choose_version(current, previous, load_id)

    assert first is current or first is previous
    assert all(choose_version(current, previous, load_id) is first for _ in range(3))


@given(percents, load_ids, st.lists(load_ids, max_size=10))
def test_a_load_keeps_its_version_whatever_else_runs_or_changes(
    percent: int, load_id: str, others: list[str]
) -> None:
    """Sticky: the side depends on the load id and the percent only, not on the order of
    the runs nor on what the current version's config says."""
    current, previous = _pair(percent)
    renamed, _ = _pair(percent, name="Acme, renamed")
    before = choose_version(current, previous, load_id) is current

    for other in others:
        choose_version(current, previous, other)

    assert (choose_version(current, previous, load_id) is current) == before
    assert (choose_version(renamed, previous, load_id) is renamed) == before


@given(load_ids)
def test_zero_percent_is_the_previous_version_and_a_hundred_the_current_one(
    load_id: str,
) -> None:
    rolled_back, previous = _pair(0)
    full, _ = _pair(100)

    assert choose_version(rolled_back, previous, load_id) is previous
    assert choose_version(full, previous, load_id) is full


@given(percents, load_ids)
def test_without_a_previous_version_every_load_runs_the_current_one(
    percent: int, load_id: str
) -> None:
    current, _ = _pair(percent)

    assert choose_version(current, None, load_id) is current


@given(percents, percents, load_ids)
def test_growing_a_canary_never_moves_a_load_back(low: int, high: int, load_id: str) -> None:
    low, high = sorted((low, high))

    if _on_current(low, load_id):
        assert _on_current(high, load_id)


@settings(max_examples=30, deadline=None)
@given(percents, st.text(max_size=12))
def test_the_share_on_the_current_version_is_close_to_the_percent(
    percent: int, prefix: str
) -> None:
    current, previous = _pair(percent)

    on_current = sum(
        choose_version(current, previous, f"{prefix}{i}") is current for i in range(SAMPLE)
    )

    assert abs(on_current / SAMPLE - percent / 100) <= 0.04


# --- catalogs ------------------------------------------------------------------------------

_TAKEN = frozenset(i.intent_id for i in standard_broker().intents)
_FOLLOW_UP = "_follow_up_due"
_TARGETS: tuple[EscalationTarget, ...] = ("dispatcher_email", "hero_task", "broker_slack")
snake_ids = st.from_regex(r"[a-z][a-z0-9]{0,8}(_[a-z0-9]{1,5}){0,2}", fullmatch=True)
texts = st.text(min_size=1, max_size=30).filter(lambda s: bool(s.strip()))


@st.composite
def brokers(draw: st.DrawFn) -> Broker:
    """The standard catalog plus a broker's own intents, obligations and requirements."""
    standard = standard_broker()
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
    all_obligations = (*standard.obligations, *obligations)
    return new_broker("acme", "Acme").revised(
        intents=with_follow_up_intents((*standard.intents, *intents), all_obligations),
        obligations=all_obligations,
        documents=requirements,
        triage=draw(st.booleans()),
        config_version=draw(st.integers(1, 1000)),
    )


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
