"""Ingress commits first. A relay repairs the DB→Temporal gap after any outage."""

import asyncio
import logging
from datetime import timedelta
from urllib.parse import quote

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from freight_agent.application.contracts import InboundMessage, IngestResult, LocationPing
from freight_agent.application.runtime import AgentRuntime, RuntimeDeps
from freight_agent.domain.errors import DomainError
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.load import Channel, Load
from freight_agent.domain.ping_filter import RiskTracker
from freight_agent.orchestration.contracts import LoadKey
from freight_agent.orchestration.workflow import LoadWorkflow

logger = logging.getLogger(__name__)


class TemporalIngress:
    def __init__(self, deps: RuntimeDeps, client: Client, task_queue: str) -> None:
        self.deps, self.client, self.task_queue = deps, client, task_queue
        self.runtime = AgentRuntime(deps)

    async def notify(self, key: LoadKey) -> None:
        async with self.deps.uow_factory() as uow:
            load = await uow.loads.get(key.load_id)
            if load and load.automation_stopped_at:
                return
        await self.client.start_workflow(
            LoadWorkflow.run,
            key,
            id=key.workflow_id,
            task_queue=self.task_queue,
            start_signal="wake",
            start_signal_args=[],
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            rpc_timeout=timedelta(seconds=10),
        )

    async def _best_effort_notify(self, key: LoadKey) -> bool:
        try:
            await self.notify(key)
            return True
        except Exception:
            # The HTTP success is for the DB commit. The persistent queue remains visible
            # to the relay; the provider need not send the message again.
            logger.exception("Temporal notification deferred for %s", key.workflow_id)
            return False

    async def register(self, load: Load) -> LoadKey:
        key = LoadKey(load.broker_id, load.load_id)
        async with self.deps.uow_factory() as uow:
            if await uow.loads.add(load):
                await uow.loads.save_tracker(load.load_id, RiskTracker(band=load.delay_risk))
            else:
                existing = await uow.loads.get(load.load_id)
                if existing is None or existing.broker_id != load.broker_id:
                    raise DomainError("load_id is globally unique and belongs to another broker")
                if existing.sop_snapshot != load.sop_snapshot:
                    raise DomainError("Este ID já usa outro SOP. Inicie uma nova execução.")
            await uow.commit()
        await self._best_effort_notify(key)
        return key

    async def _check(self, key: LoadKey, *, running: bool = False) -> None:
        async with self.deps.uow_factory() as uow:
            load = await uow.loads.get(key.load_id)
            if load is None or load.broker_id != key.broker_id:
                raise DomainError("load/broker mismatch")
            if running and load.automation_stop_requested_at:
                raise DomainError("Execução parada. Inicie uma nova carga para continuar.")

    async def stop(self, key: LoadKey) -> dict:
        await self._check(key)
        async with self.deps.uow_factory() as uow:
            load = await uow.loads.get(key.load_id)
            assert load is not None
            if load.automation_stopped_at:
                return {"status": "stopped"}
            if not load.automation_stop_requested_at:
                now = self.deps.clock.now()
                await uow.loads.save(load.model_copy(update={"automation_stop_requested_at": now}))
                event = Event(
                    event_id=f"stop:{key.workflow_id}",
                    load_id=key.load_id,
                    type=EventType.EXECUTION_STOP_REQUESTED,
                    occurred_at=now,
                    channel=Channel.SYSTEM,
                    body="Parada solicitada pelo operador.",
                )
                if await uow.events.append(event):
                    await uow.queue.enqueue(event)
            await uow.commit()
        await self._best_effort_notify(key)
        return {"status": "stopping"}

    async def message(self, key: LoadKey, message: InboundMessage) -> IngestResult:
        await self._check(key, running=True)
        if message.load_id != key.load_id:
            raise DomainError("message/load mismatch")
        # Provider ids are scoped: two brokers may both receive message '1'.
        message = message.model_copy(
            update={
                "message_id": f"{key.workflow_id}/message/{message.channel.value}/"
                f"{quote(message.message_id, safe='')}"
            }
        )
        result = await self.runtime.ingest_message(message)
        await self._best_effort_notify(key)
        return result

    async def ping(self, key: LoadKey, ping: LocationPing) -> IngestResult:
        await self._check(key, running=True)
        if ping.load_id != key.load_id:
            raise DomainError("ping/load mismatch")
        ping = ping.model_copy(
            update={"ping_id": f"{key.workflow_id}/ping/{quote(ping.ping_id, safe='')}"}
        )
        result = await self.runtime.ingest_ping(ping)
        await self._best_effort_notify(key)
        return result

    async def reconcile_once(self, *, recover_timers: bool = False) -> int:
        # Startup also discovers timers written before the workflow was started.
        async with self.deps.uow_factory() as uow:
            ids = await uow.queue.wake_loads(include_timers=recover_timers)
            keys = []
            for load_id in ids:
                load = await uow.loads.get(load_id)
                if load is not None and not load.automation_stopped_at:
                    keys.append(LoadKey(load.broker_id, load_id))
        slots = asyncio.Semaphore(32)

        async def publish(key: LoadKey) -> bool:
            async with slots:
                return await self._best_effort_notify(key)

        results = await asyncio.gather(*(publish(key) for key in keys))
        if not all(results):
            raise RuntimeError("some Temporal notifications were deferred")
        return len(keys)

    async def relay(self, interval: float) -> None:
        startup = True
        while True:
            try:
                await self.reconcile_once(recover_timers=startup)
                startup = False
            except Exception:
                logger.exception("Temporal relay failed; will retry")
            await asyncio.sleep(interval)
