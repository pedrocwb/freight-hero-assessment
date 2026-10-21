"""One active Workflow per load. Only deterministic orchestration lives here."""

from contextlib import suppress
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from freight_agent.orchestration.contracts import LoadKey, WakeState, WorkItem

RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_interval=timedelta(seconds=30))


@workflow.defn
class LoadWorkflow:
    def __init__(self) -> None:
        self._dirty = False

    @workflow.signal
    def wake(self) -> None:
        # Coalesce notifications: durable events and their FIFO order are in Postgres.
        self._dirty = True

    @workflow.run
    async def run(self, key: LoadKey) -> None:
        turns = 0
        while True:
            if turns >= 100 or workflow.info().is_continue_as_new_suggested():
                # The next run reads pending events and absolute deadlines from Postgres.
                workflow.continue_as_new(key)
            turns += 1
            self._dirty = False  # signals received during an Activity remain visible
            state = await workflow.execute_activity(
                "load_snapshot",
                key,
                result_type=WakeState,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RETRY,
            )
            timer = min(state.timers, key=lambda t: (t.due_at, t.timer_id), default=None)
            if state.outbox_id:
                await workflow.execute_activity(
                    "deliver_load_item",
                    WorkItem(key, state.outbox_id),
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=RETRY,
                )
            elif timer and timer.due_at <= workflow.now().timestamp():
                # Enqueue before draining more messages: a busy load cannot starve timers.
                # Existing inbound events remain ahead of it in the per-load FIFO.
                await workflow.execute_activity(
                    "fire_load_timer",
                    WorkItem(key, timer.timer_id),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RETRY,
                )
            elif state.documents_pending:
                await workflow.execute_activity(
                    "process_load_documents",
                    key,
                    start_to_close_timeout=timedelta(seconds=280),
                    retry_policy=RETRY,
                )
            elif state.event_id:
                await workflow.execute_activity(
                    "run_load_graph",
                    WorkItem(key, state.event_id),
                    # Runtime has a 240s thinking deadline and a 300s claim lease.
                    start_to_close_timeout=timedelta(seconds=280),
                    retry_policy=RETRY,
                )
            elif state.finished and not self._dirty:
                return
            elif self._dirty:
                continue
            elif timer:
                remaining = timer.due_at - workflow.now().timestamp()
                # Reread DB after timeout: an ETA may have cancelled the timer.
                with suppress(TimeoutError):
                    await workflow.wait_condition(
                        lambda: self._dirty, timeout=timedelta(seconds=max(0, remaining))
                    )
            else:
                await workflow.wait_condition(lambda: self._dirty)
