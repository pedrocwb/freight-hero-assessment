"""Stopping during a wait or a graph turn must prevent later deliveries and restarts."""

import asyncio
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.temporal.test_load_workflow import ClockedActivities, reached
from tests.temporal.test_load_workflow import pytestmark as temporal_marks
from tests.unit.application.test_runtime_harness import ASK_ETA, DECIDE, World, plan

from freight_agent.application.contracts import LocationPing
from freight_agent.application.runtime import RuntimeConfig
from freight_agent.domain.errors import DomainError
from freight_agent.domain.load import DelayRisk, Load
from freight_agent.orchestration.contracts import LoadKey, WorkItem
from freight_agent.orchestration.service import TemporalIngress
from freight_agent.orchestration.workflow import LoadWorkflow

pytestmark = temporal_marks


class PausableActivities(ClockedActivities):
    def __init__(self, world, env):
        super().__init__(world, env)
        self.thinking = asyncio.Event()
        self.resume = asyncio.Event()

    @activity.defn(name="run_load_graph")
    async def run_graph(self, work: WorkItem) -> None:
        self.thinking.set()
        await self.resume.wait()
        await super().run_graph(work)


@pytest.mark.parametrize("during_thinking", [False, True])
async def test_stop_settles_current_activity_then_cancels_all_future_work(
    load: Load, during_thinking
):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        world = await World(load, config=RuntimeConfig(delivery_followups=True)).start()
        world.clock.set(await env.get_current_time())
        world.model.push(DECIDE, plan(ASK_ETA))
        activities = PausableActivities(world, env)
        if not during_thinking:
            activities.resume.set()
        service = TemporalIngress(world.deps, env.client, f"stop-{uuid4().hex}")
        key = LoadKey(load.broker_id, load.load_id)
        async with Worker(
            env.client,
            task_queue=service.task_queue,
            workflows=[LoadWorkflow],
            activities=activities.registered(),
        ):
            ping = LocationPing(
                ping_id="risk",
                load_id=load.load_id,
                lat=37.95,
                lon=-121.29,
                occurred_at=world.clock.now(),
                delay_risk=DelayRisk.HIGH,
            )
            await service.ping(key, ping)
            await reached(activities.thinking if during_thinking else activities.waiting)
            await service.stop(key)
            assert (await world.snapshot()).load.automation_stop_requested_at
            activities.resume.set()
            handle = env.client.get_workflow_handle(key.workflow_id)
            await asyncio.wait_for(handle.result(), 15)
            snapshot = await world.snapshot()
            assert snapshot.load.automation_stopped_at
            assert not await world.pending_timers()
            assert all(not o.is_open for o in snapshot.obligations)
            assert all(o.status != "pending" for o in snapshot.outbox)
            expected = 0 if during_thinking else 1
            assert len(world.sent("sms")) == expected
            assert snapshot.outbox[0].status == ("cancelled" if during_thinking else "sent")
            assert await service.reconcile_once(recover_timers=True) == 0
            await service.notify(key)
            with pytest.raises(DomainError, match="parada"):
                await service.ping(key, ping)
            await env.sleep(3600)
            assert len(world.sent("sms")) == expected
            assert not world.sent("hero_task")
