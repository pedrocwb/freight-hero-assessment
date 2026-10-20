"""Contract suite for `BrokerRepository` (`ports/brokers.py`), on every unit of work.

Owner: card W3-BRK. It runs on the memory store, each test on a fresh, empty store.

It pins down what the port's docstrings leave implicit: a broker comes back equal (every
part of the catalog survives the store), `save` makes the given version current and
keeps every other one, saving a version again replaces only that snapshot (a rollout
change), versions are listed by number, brokers by id in code point order, and nothing
is kept unless the unit of work commits.
"""

import pytest

from freight_agent.domain.broker import (
    Broker,
    BrokerLimits,
    DocumentRequirement,
    IntentDef,
    ObligationDef,
    Rollout,
    new_broker,
    with_follow_up_intents,
)
from freight_agent.domain.load import Stage
from freight_agent.domain.validation import ValidationConfig
from freight_agent.ports.repositories import UnitOfWorkFactory

pytestmark = pytest.mark.contract


def _acme(version: int = 1, **changes: object) -> Broker:
    """Acme at `version`; each version after the first rolls out over the one before."""
    rollout = Rollout(previous_version=version - 1) if version > 1 else Rollout()
    return new_broker("acme", "Acme Freight").revised(
        config_version=version, rollout=rollout, **changes
    )


def _rich() -> Broker:
    """Acme with every part of a catalog changed, so a lossy store shows."""
    base = _acme(3, sop_version="3f2a9c1b7d4e")
    pod = ObligationDef(
        kind="pod",
        title="Proof of delivery",
        description="The signed POD, due after the truck arrives at the receiver.",
        follow_up_minutes=30,
        max_attempts=1,
        on_exhausted=("hero_task",),
    )
    detention = IntentDef(
        intent_id="detention_request",
        title="Detention request",
        description="The truck waited more than 2 hours at a stop: «on attend», 3h, 🚚.",
        examples=("Been at the dock since 7, still not unloaded",),
        priority=55,
    )
    obligations = (*base.obligations, pod)
    return base.revised(
        triage=False,
        intents=with_follow_up_intents((*base.intents, detention), obligations),
        obligations=obligations,
        documents=(
            DocumentRequirement(
                type_id="pod",
                at_stage=Stage.AT_DELIVERY,
                within_minutes=120,
                opens_obligation="pod",
            ),
        ),
        limits=BrokerLimits(
            validation=ValidationConfig(sms_max_per_window=2, sms_window_minutes=45),
            max_steps=0,
            max_cost_usd=0.125,
            max_rejections=1,
        ),
        rollout=Rollout(percent=25, previous_version=2),
    )


async def _save(uow_factory: UnitOfWorkFactory, *brokers: Broker) -> None:
    async with uow_factory() as uow:
        for broker in brokers:
            await uow.brokers.save(broker)
        await uow.commit()


async def test_a_broker_never_saved_is_not_found(uow_factory: UnitOfWorkFactory) -> None:
    async with uow_factory() as uow:
        assert await uow.brokers.get("acme") is None
        assert await uow.brokers.get_version("acme", 1) is None
        assert await uow.brokers.versions("acme") == []
        assert await uow.brokers.list() == []


async def test_a_saved_broker_comes_back_equal_with_every_part(
    uow_factory: UnitOfWorkFactory,
) -> None:
    rich = _rich()

    await _save(uow_factory, rich)

    async with uow_factory() as uow:
        assert await uow.brokers.get("acme") == rich
        assert await uow.brokers.get_version("acme", 3) == rich
        assert await uow.brokers.versions("acme") == [rich]
        assert await uow.brokers.list() == [rich]


async def test_a_new_version_becomes_current_and_the_previous_one_is_kept(
    uow_factory: UnitOfWorkFactory,
) -> None:
    first, second = _acme(1), _acme(2, name="Acme Freight, Inc.")

    await _save(uow_factory, first)
    await _save(uow_factory, second)

    async with uow_factory() as uow:
        assert await uow.brokers.get("acme") == second
        assert await uow.brokers.get_version("acme", 1) == first
        assert await uow.brokers.get_version("acme", 2) == second
        assert await uow.brokers.get_version("acme", 3) is None
        assert await uow.brokers.versions("acme") == [first, second]
        assert await uow.brokers.list() == [second]


async def test_saving_a_version_again_replaces_only_that_snapshot(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """A rollout change (a canary, a rollback) re-saves the current version."""
    first, second = _acme(1), _acme(2)
    rolled_back = second.revised(rollout=Rollout(percent=0, previous_version=1))
    await _save(uow_factory, first, second)

    await _save(uow_factory, rolled_back)

    async with uow_factory() as uow:
        assert await uow.brokers.get("acme") == rolled_back
        assert await uow.brokers.versions("acme") == [first, rolled_back]


async def test_save_makes_the_given_version_current_whatever_its_number(
    uow_factory: UnitOfWorkFactory,
) -> None:
    first, second = _acme(1), _acme(2)
    await _save(uow_factory, first, second)

    await _save(uow_factory, first)

    async with uow_factory() as uow:
        assert await uow.brokers.get("acme") == first
        assert await uow.brokers.versions("acme") == [first, second]


async def test_versions_are_listed_by_number(uow_factory: UnitOfWorkFactory) -> None:
    """Numbers, not text: version 10 comes after version 9."""
    ninth, tenth = _acme(9), _acme(10)

    await _save(uow_factory, ninth, tenth)

    async with uow_factory() as uow:
        assert [b.config_version for b in await uow.brokers.versions("acme")] == [9, 10]


async def test_brokers_are_listed_by_id_in_code_point_order(
    uow_factory: UnitOfWorkFactory,
) -> None:
    ids = ["beta", "acme_east", "acme", "acme-west", "7eleven"]

    await _save(uow_factory, *(new_broker(broker_id, broker_id.title()) for broker_id in ids))

    async with uow_factory() as uow:
        assert [b.broker_id for b in await uow.brokers.list()] == sorted(ids)
    assert sorted(ids) == ["7eleven", "acme", "acme-west", "acme_east", "beta"]


async def test_brokers_keep_their_versions_apart(uow_factory: UnitOfWorkFactory) -> None:
    acme, beta = _acme(1), new_broker("beta", "Beta")
    beta_two = beta.revised(config_version=2, rollout=Rollout(previous_version=1))

    await _save(uow_factory, acme, beta, beta_two)

    async with uow_factory() as uow:
        assert await uow.brokers.versions("acme") == [acme]
        assert await uow.brokers.get_version("acme", 2) is None
        assert await uow.brokers.versions("beta") == [beta, beta_two]
        assert await uow.brokers.list() == [acme, beta_two]


async def test_a_unit_of_work_reads_its_own_writes(uow_factory: UnitOfWorkFactory) -> None:
    async with uow_factory() as uow:
        await uow.brokers.save(_acme(1))
        await uow.brokers.save(_acme(2))

        assert await uow.brokers.get("acme") == _acme(2)
        assert [b.config_version for b in await uow.brokers.versions("acme")] == [1, 2]
        await uow.commit()


async def test_nothing_is_kept_unless_the_unit_of_work_commits(
    uow_factory: UnitOfWorkFactory,
) -> None:
    await _save(uow_factory, _acme(1))

    async with uow_factory() as uow:
        await uow.brokers.save(_acme(2))
        await uow.brokers.save(new_broker("beta", "Beta"))

    with pytest.raises(RuntimeError, match="boom"):
        async with uow_factory() as uow:
            await uow.brokers.save(_acme(2))
            await uow.commit()
            raise RuntimeError("boom")

    async with uow_factory() as uow:
        assert await uow.brokers.get("acme") == _acme(1)
        assert await uow.brokers.versions("acme") == [_acme(1)]
        assert await uow.brokers.get("beta") is None
