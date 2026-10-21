"""The Temporal bridge against both stores, real graph with a scripted LLM."""

from dataclasses import replace
from datetime import timedelta

import pytest

from freight_agent.application.runtime import AgentRuntime, RuntimeConfig
from freight_agent.domain.actions import CloseObligation, CloseStatus
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.load import Channel, DelayRisk, Load
from freight_agent.domain.runs import RunStatus
from freight_agent.interfaces.repositories import UnitOfWorkFactory
from freight_agent.orchestration.activities import LoadActivities
from freight_agent.orchestration.contracts import LoadKey, WorkItem
from tests.unit.application.test_runtime_harness import (
    ASK_ETA,
    DECIDE,
    ETA,
    FOLLOW_UP_IN_30,
    World,
    at,
    plan,
)

pytestmark = pytest.mark.contract


async def world_on(load: Load, factory: UnitOfWorkFactory) -> World:
    world = World(load, config=RuntimeConfig(delivery_followups=True))
    world.deps = replace(world.deps, uow_factory=factory)
    world.runtime = AgentRuntime(world.deps)
    return await world.start()


async def test_exact_event_fenced_claim_and_fifo(uow_factory: UnitOfWorkFactory, load: Load):
    second = load.model_copy(update={"load_id": "other"})
    async with uow_factory() as uow:
        assert await uow.loads.add(load)
        assert not await uow.loads.add(load)
        assert await uow.loads.add(second)
        for event_id, load_id in [("other-1", "other"), ("a", load.load_id), ("b", load.load_id)]:
            await uow.queue.enqueue(
                Event(
                    event_id=event_id,
                    load_id=load_id,
                    type=EventType.INBOUND_MESSAGE,
                    channel=Channel.SMS,
                    occurred_at=at(7, 0),
                )
            )
        assert await uow.queue.claim_next(at(7, 0), load_id=load.load_id, event_id="b") is None
        event = await uow.queue.claim_next(
            at(7, 0), load_id=load.load_id, event_id="a", token="old"
        )
        assert event and event.event_id == "a"
        assert await uow.queue.owns_claim("a", "old")
        await uow.commit()
    async with uow_factory() as uow:
        assert await uow.queue.reclaim_expired(at(7, 6), 300) == 1
        assert not await uow.queue.owns_claim("a", "old")
        assert await uow.queue.claim_next(at(7, 6), load_id=load.load_id, event_id="a", token="new")
        assert await uow.queue.owns_claim("a", "new")
        head = await uow.queue.head(load.load_id)
        assert head and head.event_id == "a"
        assert await uow.queue.wake_loads() == [load.load_id, "other"]
        await uow.commit()


@pytest.mark.parametrize("model_schedules", [False, True])
async def test_confirmed_delivery_arms_timer_and_retry_does_not_reset_it(
    uow_factory: UnitOfWorkFactory,
    load: Load,
    model_schedules: bool,
):
    world = await world_on(load, uow_factory)
    activities = LoadActivities(world.deps)
    key = LoadKey(load.broker_id, load.load_id)
    result = await world.ping(at(7, 0), DelayRisk.HIGH)
    assert result.event_id
    world.model.push(DECIDE, plan(ASK_ETA, *([FOLLOW_UP_IN_30] if model_schedules else [])))
    await activities.run_graph(WorkItem(key, result.event_id))
    state = await activities.snapshot(key)
    assert state.timers == []  # no timer before the send is confirmed
    assert state.outbox_id
    # Crash after graph commit: retry must use the persisted run, with no new model calls.
    calls = len(world.model.calls)
    await LoadActivities(world.deps).run_graph(WorkItem(key, result.event_id))
    assert len(world.model.calls) == calls
    world.clock.set(at(7, 5))
    await activities.deliver(WorkItem(key, state.outbox_id))
    due = (await activities.snapshot(key)).timers[0]
    assert due.due_at == at(7, 35).timestamp()
    world.clock.set(at(7, 10))
    await LoadActivities(world.deps).deliver(WorkItem(key, state.outbox_id))
    assert (await activities.snapshot(key)).timers == [due]
    assert len(world.sent("sms")) == 1
    world.clock.set(at(7, 35))
    await activities.fire_timer(WorkItem(key, due.timer_id))
    await activities.fire_timer(WorkItem(key, due.timer_id))
    snapshot = await world.runtime.snapshot(load.load_id)
    assert len([e for e in snapshot.events if e.type is EventType.TIMER_DUE]) == 1


async def test_late_attempt_cannot_commit_or_release_replacement(
    uow_factory: UnitOfWorkFactory,
    load: Load,
):
    world = await world_on(load, uow_factory)
    await world.ping(at(7, 0), DelayRisk.HIGH)
    old = await world.runtime._claim(load_id=load.load_id)
    assert old
    world.model.push(DECIDE, plan(ASK_ETA))
    verdict = await world.runtime._think(old)
    world.clock.advance(timedelta(minutes=6))
    await world.runtime._reclaim_expired()
    replacement = await world.runtime._claim(load_id=load.load_id)
    assert replacement
    with pytest.raises(RuntimeError, match="claim was lost"):
        await world.runtime._commit(old, verdict)
    await world.runtime._fail(old, RuntimeError("old worker failed late"))
    async with uow_factory() as uow:
        assert await uow.queue.owns_claim(replacement.event.event_id, replacement.claim_token)
        assert await uow.outbox.list_for_load(load.load_id) == []


async def test_cancelled_timer_is_harmless(uow_factory: UnitOfWorkFactory, load: Load):
    world = await world_on(load, uow_factory)
    await world.high_risk_at_seven()
    (timer,) = await world.pending_timers()
    # Closing happens through the same runtime executor used by the graph.
    await world.ping(at(7, 10), DelayRisk.LOW, ping_id="low1")
    await world.ping(at(7, 11), DelayRisk.LOW, ping_id="low2")
    world.model.push(DECIDE, plan(CloseObligation(kind=ETA, status=CloseStatus.CANCELLED)))
    runs = await world.runtime.process_all()
    assert runs[-1].status is RunStatus.ACTED
    world.clock.set(at(8, 0))
    await world.runtime.fire_timer(load.load_id, timer.timer_id)
    assert not any(e.type is EventType.TIMER_DUE for e in (await world.snapshot()).events)


async def test_provider_receipt_keeps_original_deadline_after_lost_ack(
    uow_factory: UnitOfWorkFactory,
    load: Load,
):
    from freight_agent.interfaces.gateways import DeliveryReceipt

    world = await world_on(load, uow_factory)
    await world.ping(at(7, 0), DelayRisk.HIGH)
    world.model.push(DECIDE, plan(ASK_ETA))
    await world.runtime.process_all()

    # A real provider returns the original acceptance time for the same key, even if
    # the first response was lost and this worker only retries ten minutes later.
    class ReceiptGateway:
        async def send(self, **kwargs):
            return DeliveryReceipt(accepted_at=at(7, 1))

    deps = replace(world.deps, gateways=replace(world.deps.gateways, sms=ReceiptGateway()))
    runtime = AgentRuntime(deps)
    world.clock.set(at(7, 11))
    item = (await world.snapshot()).outbox[0]
    sent = await runtime.deliver_item(load.load_id, item.outbox_id)
    assert sent.accepted_at == at(7, 1)
    timer = (await world.pending_timers())[0]
    assert timer.due_at == at(7, 31)


async def test_failed_delivery_never_arms_a_followup(uow_factory: UnitOfWorkFactory, load: Load):
    from freight_agent.interfaces.gateways import DeliveryError

    world = await world_on(load, uow_factory)
    await world.ping(at(7, 0), DelayRisk.HIGH)
    world.model.push(DECIDE, plan(ASK_ETA))
    await world.runtime.process_all()

    class BrokenGateway:
        async def send(self, **kwargs):
            raise DeliveryError("provider unavailable")

    runtime = AgentRuntime(
        replace(world.deps, gateways=replace(world.deps.gateways, sms=BrokenGateway()))
    )
    item = (await world.snapshot()).outbox[0]
    failed = await runtime.deliver_item(load.load_id, item.outbox_id)
    assert failed.accepted_at is None
    assert await world.pending_timers() == []


async def test_expired_claim_exhaustion_stops_activity_retries(
    uow_factory: UnitOfWorkFactory,
    load: Load,
):
    from temporalio.exceptions import ApplicationError

    world = await world_on(load, uow_factory)
    result = await world.ping(at(7, 0), DelayRisk.HIGH)
    assert result.event_id
    for attempt in range(3):
        async with uow_factory() as uow:
            assert await uow.queue.claim_next(
                world.clock.now(), load_id=load.load_id, token=f"crash-{attempt}"
            )
            await uow.commit()
        world.clock.advance(timedelta(minutes=6))
        await world.runtime._reclaim_expired()
    async with uow_factory() as uow:
        assert await uow.queue.is_dead(result.event_id)
        assert await uow.queue.wake_loads(include_timers=True) == []
    activities = LoadActivities(world.deps)
    with pytest.raises(ApplicationError, match="dead-letter") as raised:
        await activities.run_graph(WorkItem(LoadKey(load.broker_id, load.load_id), result.event_id))
    assert raised.value.non_retryable
    assert world.model.calls == []
