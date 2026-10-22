"""Aurora's real file catalog through Temporal, LangGraph, tools and delivery confirmation."""

import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.temporal.test_load_workflow import ClockedActivities, reached
from tests.temporal.test_load_workflow import pytestmark as temporal_marks
from tests.unit.application.test_runtime_harness import DECIDE, World, plan

from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.application.contracts import LocationPing
from freight_agent.application.runtime import RuntimeConfig
from freight_agent.domain.actions import CloseObligation, CloseStatus, CreateHeroTask, SendSms
from freight_agent.domain.events import EventType
from freight_agent.domain.load import DelayRisk, Load
from freight_agent.domain.obligations import ObligationStatus
from freight_agent.domain.outbox import OutboxStatus
from freight_agent.domain.timers import TimerStatus
from freight_agent.orchestration.contracts import LoadKey
from freight_agent.orchestration.service import TemporalIngress
from freight_agent.orchestration.workflow import LoadWorkflow

ROOT = Path(__file__).resolve().parents[2]
pytestmark = temporal_marks


async def test_ten_sms_one_minute_apart_then_hero_only_after_final_wait():
    load = Load.from_metadata(
        json.loads((ROOT / "evals/fixtures/load_aurora_extreme.json").read_text())
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        world = await World(
            load,
            knowledge=MemorySopIndex.from_files(ROOT / "sops"),
            config=RuntimeConfig(delivery_followups=True),
        ).start()
        world.clock.set(await env.get_current_time())
        for attempt in range(1, 11):
            world.model.push(
                DECIDE,
                plan(
                    SendSms(
                        contact_id="drv_aurora",
                        text=f"Tentativa {attempt}: qual a previsão de chegada?",
                        requests="eta_request",
                    )
                ),
            )
        world.model.push(
            DECIDE,
            plan(
                CreateHeroTask(
                    title="Ligar para o motorista",
                    urgent=True,
                    description="Dez SMS sem resposta. Confirmar ETA no CD Aurora.",
                ),
                CloseObligation(kind="eta_request", status=CloseStatus.EXHAUSTED),
            ),
        )
        activities = ClockedActivities(world, env)
        service = TemporalIngress(world.deps, env.client, f"extreme-{uuid4().hex}")
        key = LoadKey(load.broker_id, load.load_id)
        async with Worker(
            env.client,
            task_queue=service.task_queue,
            workflows=[LoadWorkflow],
            activities=activities.registered(),
        ):
            ping = LocationPing(
                ping_id="extreme-risk",
                load_id=load.load_id,
                lat=-23.68,
                lon=-46.88,
                delay_risk=DelayRisk.HIGH,
                occurred_at=world.clock.now(),
            )
            await service.ping(key, ping)
            assert (await service.ping(key, ping)).duplicate
            for attempt in range(1, 11):
                await reached(activities.waiting)
                snapshot = await world.snapshot()
                assert len(world.sent("sms")) == attempt
                assert not world.sent("hero_task")
                assert not world.sent("email")
                obligation = snapshot.obligations[0]
                assert obligation.status is ObligationStatus.OPEN
                assert obligation.attempts == attempt
                timer = next(t for t in snapshot.timers if t.status is TimerStatus.PENDING)
                sent = [o for o in snapshot.outbox if o.kind == "sms"]
                assert all(o.status is OutboxStatus.SENT for o in sent)
                assert sent[-1].accepted_at is not None
                assert timer.due_at - sent[-1].accepted_at == timedelta(minutes=1)
                # Nothing may advance before the full interval, including after SMS 10.
                await env.sleep(
                    max(0, (timer.due_at - await env.get_current_time()).total_seconds() - 1)
                )
                assert len(world.sent("sms")) == attempt
                assert not world.sent("hero_task")
                activities.waiting.clear()
                await env.sleep(2)
            await reached(activities.waiting)
            final = await world.snapshot()
            assert len(world.sent("sms")) == 10
            assert len(world.sent("hero_task")) == 1
            assert final.obligations[0].status is ObligationStatus.EXHAUSTED
            assert not await world.pending_timers()
            assert len([e for e in final.events if e.type is EventType.TIMER_DUE]) == 10
            assert len(final.runs) == 11
            assert all(not r.violations and not r.error for r in final.runs)
            assert all("tools" in r.nodes and "agent" in r.nodes for r in final.runs)
            assert world.model.remaining() == 0
            await env.sleep(180)
            assert len(world.sent("sms")) == 10
            assert len(world.sent("hero_task")) == 1
            await env.client.get_workflow_handle(key.workflow_id).terminate()
