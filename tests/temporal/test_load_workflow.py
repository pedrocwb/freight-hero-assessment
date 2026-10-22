"""Real Temporal server, real LangGraph/runtime, scripted model; no paid APIs.

RUN_TEMPORAL_TESTS=1 uv run pytest tests/temporal -v
The SDK downloads its time-skipping test server on the first run.
"""

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.unit.application.test_runtime_harness import (
    ASK_ETA,
    DECIDE,
    DRIVER,
    ETA,
    GATE,
    World,
    decision,
    plan,
)

from freight_agent.application.contracts import InboundMessage, LocationPing
from freight_agent.application.runtime import RuntimeConfig
from freight_agent.domain.actions import CloseObligation, CloseStatus, Purpose, SendSms
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Channel, DelayRisk, Load
from freight_agent.orchestration.activities import LoadActivities
from freight_agent.orchestration.contracts import LoadKey, WakeState, WorkItem
from freight_agent.orchestration.service import TemporalIngress
from freight_agent.orchestration.workflow import LoadWorkflow

pytestmark = [
    pytest.mark.temporal,
    pytest.mark.skipif(
        os.getenv("RUN_TEMPORAL_TESTS") != "1",
        reason="set RUN_TEMPORAL_TESTS=1 (Temporal test server)",
    ),
]


class ClockedActivities(LoadActivities):
    """Only the clock differs: match the SDK's time-skipping server before each Activity."""

    def __init__(self, world: World, env: WorkflowEnvironment):
        super().__init__(world.deps)
        self.world, self.env = world, env
        self.waiting = asyncio.Event()
        self.processed = asyncio.Event()
        self.fired = asyncio.Event()
        self.fired_at: float | None = None

    async def sync(self):
        self.world.clock.set(await self.env.get_current_time())

    @activity.defn(name="load_snapshot")
    async def snapshot(self, key: LoadKey) -> WakeState:
        await self.sync()
        state = await super().snapshot(key)
        if not state.event_id and not state.outbox_id:
            self.waiting.set()
        return state

    @activity.defn(name="run_load_graph")
    async def run_graph(self, work: WorkItem) -> None:
        await self.sync()
        await super().run_graph(work)
        self.processed.set()

    @activity.defn(name="deliver_load_item")
    async def deliver(self, work: WorkItem) -> None:
        await self.sync()
        await super().deliver(work)

    @activity.defn(name="fire_load_timer")
    async def fire_timer(self, work: WorkItem) -> None:
        await self.sync()
        await super().fire_timer(work)
        self.fired_at = self.world.clock.now().timestamp()
        self.fired.set()


async def reached(event: asyncio.Event, timeout: float = 15):
    await asyncio.wait_for(event.wait(), timeout)


@pytest.mark.parametrize("mode", ["timer", "po_duplicate", "eta", "restart"])
async def test_real_workflow_durable_wait(load: Load, mode: str):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        world = await World(load, config=RuntimeConfig(delivery_followups=True)).start()
        world.clock.set(await env.get_current_time())
        service = TemporalIngress(world.deps, env.client, f"test-{uuid4().hex}")
        key = LoadKey(load.broker_id, load.load_id)
        activities = ClockedActivities(world, env)
        worker_args = {
            "client": env.client,
            "task_queue": service.task_queue,
            "workflows": [LoadWorkflow],
            "activities": activities.registered(),
            "max_cached_workflows": 0,
        }
        world.model.push(DECIDE, plan(ASK_ETA), plan())
        async with Worker(**worker_args):
            await service.ping(
                key,
                LocationPing(
                    ping_id="risk",
                    load_id=load.load_id,
                    lat=37.95,
                    lon=-121.29,
                    occurred_at=world.clock.now(),
                    delay_risk=DelayRisk.HIGH,
                ),
            )
            await reached(activities.waiting)
            initial = await activities.snapshot(key)
            assert len(initial.timers) == 1
            due = initial.timers[0].due_at
            timer = (await world.runtime.snapshot(load.load_id)).timers[0]
            assert timer.due_at - timer.created_at == timedelta(minutes=30)
            await env.sleep(600)
            if mode in {"po_duplicate", "eta"}:
                activities.waiting.clear()
                # Replace the queued timer's noop with the response to the new message.
                world.model._queues[DECIDE].clear()
                world.model.push(
                    GATE,
                    decision(Intent.QUESTION if mode == "po_duplicate" else Intent.ETA_PROVIDED),
                )
                action = (
                    SendSms(contact_id=DRIVER, text="PO 55821", purpose=Purpose.REPLY)
                    if mode == "po_duplicate"
                    else CloseObligation(kind=ETA, status=CloseStatus.SATISFIED)
                )
                world.model.push(DECIDE, plan(action), plan())
                message = InboundMessage(
                    message_id="reply",
                    load_id=load.load_id,
                    channel=Channel.SMS,
                    sender_contact_id=DRIVER,
                    body="What's the PO?" if mode == "po_duplicate" else "ETA 10:30",
                    occurred_at=await env.get_current_time(),
                )
                await service.message(key, message)
                duplicate = await service.message(key, message)
                assert duplicate.duplicate
                await reached(activities.waiting)
                current = await activities.snapshot(key)
                assert current.timers == ([] if mode == "eta" else initial.timers)
            if mode != "restart":
                await env.sleep(max(0, due - (await env.get_current_time()).timestamp()) + 1)
                if mode == "eta":
                    assert not activities.fired.is_set()
                else:
                    await reached(activities.fired)
                    assert activities.fired_at is not None and activities.fired_at >= due
                assert len(world.sent("sms")) == (2 if mode == "po_duplicate" else 1)
        if mode == "restart":
            # No worker runs while the remaining 20 minutes elapse.
            await env.sleep(max(0, due - (await env.get_current_time()).timestamp()) + 1)
            replacement = ClockedActivities(world, env)
            worker_args["activities"] = replacement.registered()
            async with Worker(**worker_args):
                await reached(replacement.fired)
                assert replacement.fired_at is not None and replacement.fired_at >= due
                assert (await world.runtime.snapshot(load.load_id)).timers[
                    0
                ].due_at.timestamp() == due
        await env.client.get_workflow_handle(key.workflow_id).terminate()


async def test_continue_as_new_keeps_unprocessed_events(load: Load):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        world = await World(load, config=RuntimeConfig(delivery_followups=True)).start()
        world.clock.set(await env.get_current_time())
        for i in range(105):
            await world.runtime.ingest_message(
                InboundMessage(
                    message_id=f"empty-{i}",
                    load_id=load.load_id,
                    channel=Channel.SMS,
                    body="",
                    occurred_at=world.clock.now(),
                )
            )
        key = LoadKey(load.broker_id, load.load_id)
        activities = ClockedActivities(world, env)
        queue = f"continue-{uuid4().hex}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[LoadWorkflow],
            activities=activities.registered(),
            max_cached_workflows=100,
        ):
            handle = await env.client.start_workflow(
                LoadWorkflow.run, key, id=key.workflow_id, task_queue=queue
            )
            first = handle.first_execution_run_id
            await reached(activities.waiting, timeout=45)
            latest = await handle.describe()
            assert latest.run_id != first
            snapshot = await world.snapshot()
            assert len(snapshot.runs) == 105
            assert len({r.event_id for r in snapshot.runs}) == 105
            assert world.model.calls == []
            await handle.terminate()
