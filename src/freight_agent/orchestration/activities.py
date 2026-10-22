"""Activities call the existing runtime: claim → LangGraph → atomic state/outbox commit."""

from dataclasses import replace

from temporalio import activity
from temporalio.exceptions import ApplicationError

from freight_agent.application.runtime import AgentRuntime, RuntimeDeps
from freight_agent.domain.documents import DocumentStatus
from freight_agent.domain.load import Stage
from freight_agent.domain.obligations import ObligationStatus
from freight_agent.domain.outbox import OutboxStatus
from freight_agent.domain.runs import RunStatus
from freight_agent.domain.timers import TimerStatus, is_stale
from freight_agent.orchestration.contracts import LoadKey, TimerRef, WakeState, WorkItem
from freight_agent.orchestration.policy import pinned_index


class LoadActivities:
    def __init__(self, deps: RuntimeDeps) -> None:
        if not deps.config.delivery_followups:
            raise ValueError("Temporal requires delivery_followups=True")
        self.deps = deps
        self.runtime = AgentRuntime(deps)

    async def _check(self, key: LoadKey) -> None:
        async with self.deps.uow_factory() as uow:
            load = await uow.loads.get(key.load_id)
            if load is None or load.broker_id != key.broker_id:
                raise ApplicationError("load/broker mismatch", non_retryable=True)

    @activity.defn(name="load_snapshot")
    async def snapshot(self, key: LoadKey) -> WakeState:
        async with self.deps.uow_factory() as uow:
            load = await uow.loads.get(key.load_id)
            if load is None or load.broker_id != key.broker_id:
                raise ApplicationError("load/broker mismatch", non_retryable=True)
            if load.automation_stopped_at:
                return WakeState(finished=True)
            if load.automation_stop_requested_at:
                return WakeState(stop_requested=True)
            head = await uow.queue.head(key.load_id)
            if head and await uow.queue.is_dead(head.event_id):
                raise ApplicationError(f"dead-letter event: {head.event_id}", non_retryable=True)
            items = await uow.outbox.list_for_load(key.load_id)
            if any(i.status is OutboxStatus.FAILED for i in items):
                raise ApplicationError(
                    "delivery exhausted; operator intervention needed", non_retryable=True
                )
            pending = next((i for i in items if i.status is OutboxStatus.PENDING), None)
            obligations = await uow.obligations.list_for_load(key.load_id)
            by_id = {o.obligation_id: o for o in obligations}
            timers = [
                TimerRef(t.timer_id, t.due_at.timestamp())
                for t in await uow.timers.list_for_load(key.load_id)
                if t.status is TimerStatus.PENDING and not is_stale(t, by_id.get(t.obligation_id))
            ]
            documents_pending = any(
                d.status is DocumentStatus.PENDING
                for d in await uow.documents.list_for_load(key.load_id)
            )
            return WakeState(
                event_id=head.event_id if head else None,
                outbox_id=pending.outbox_id if pending else None,
                timers=timers,
                documents_pending=documents_pending,
                finished=(
                    load.stage is Stage.DELIVERED
                    and not head
                    and not pending
                    and not any(o.is_open for o in obligations)
                ),
            )

    @activity.defn(name="run_load_graph")
    async def run_graph(self, work: WorkItem) -> None:
        await self._check(work.load)
        async with self.deps.uow_factory() as uow:
            if await uow.queue.is_dead(work.item_id):
                raise ApplicationError(f"dead-letter event: {work.item_id}", non_retryable=True)
            load = await uow.loads.get(work.load.load_id)
            frozen = load.sop_snapshot if load else None
        runtime = (
            AgentRuntime(replace(self.deps, knowledge=pinned_index(frozen)))
            if frozen
            else self.runtime
        )
        run = await runtime.process_event(work.load.load_id, work.item_id)
        if run.status is RunStatus.FAILED:
            async with self.deps.uow_factory() as uow:
                dead = await uow.queue.is_dead(work.item_id)
            raise ApplicationError(run.error or "run failed", non_retryable=dead)

    @activity.defn(name="fire_load_timer")
    async def fire_timer(self, work: WorkItem) -> None:
        await self._check(work.load)
        await self.runtime.fire_timer(work.load.load_id, work.item_id)

    @activity.defn(name="deliver_load_item")
    async def deliver(self, work: WorkItem) -> None:
        await self._check(work.load)
        item = await self.runtime.deliver_item(work.load.load_id, work.item_id)
        if item.status is not OutboxStatus.SENT:
            raise ApplicationError(
                item.last_error or "delivery failed",
                non_retryable=item.status is OutboxStatus.FAILED,
            )

    @activity.defn(name="process_load_documents")
    async def documents(self, key: LoadKey) -> None:
        await self._check(key)
        events = await self.deps.documents.process_pending(limit=1, load_id=key.load_id)
        if events:
            return
        # A persistence failure leaves the document pending. Retry with backoff instead
        # of spinning through snapshots on a permanently broken document pipeline.
        async with self.deps.uow_factory() as uow:
            pending = [
                d
                for d in await uow.documents.list_for_load(key.load_id)
                if d.status is DocumentStatus.PENDING
            ]
        if pending:
            raise ApplicationError("pending documents remain; retry processing")

    @activity.defn(name="stop_load_execution")
    async def stop(self, key: LoadKey) -> None:
        """Serialized after the current Activity: retain history, cancel future work."""
        await self._check(key)
        async with self.deps.uow_factory() as uow:
            load = await uow.loads.get(key.load_id)
            if load is None or load.automation_stopped_at:
                return
            for timer in await uow.timers.list_for_load(key.load_id):
                if timer.status is TimerStatus.PENDING:
                    await uow.timers.save(timer.mark(TimerStatus.CANCELLED))
            for obligation in await uow.obligations.list_for_load(key.load_id):
                if obligation.is_open:
                    await uow.obligations.save(obligation.close(ObligationStatus.CANCELLED))
            for item in await uow.outbox.list_for_load(key.load_id):
                if item.status is OutboxStatus.PENDING:
                    await uow.outbox.save(
                        item.model_copy(update={"status": OutboxStatus.CANCELLED})
                    )
            await uow.loads.save(
                load.model_copy(update={"automation_stopped_at": self.deps.clock.now()})
            )
            await uow.commit()

    def registered(self) -> list:
        return [
            self.snapshot,
            self.run_graph,
            self.fire_timer,
            self.deliver,
            self.documents,
            self.stop,
        ]
