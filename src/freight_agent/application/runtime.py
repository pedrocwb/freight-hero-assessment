"""AgentRuntime: the one facade the API, the CLI and the eval harness use.

The same object runs in production, in the simulation and in evals; only the
adapters passed in differ (composition root: `freight_agent.bootstrap`).

A run (`process_next`) has three phases, so that no unit of work is open while
a model thinks (the memory store serializes units of work behind one lock, and
a database would hold the claimed rows):

1. claim: one unit of work claims the next event, reads its context
   (`load_run_context`) and commits the claim;
2. think: the run graph (prefilter, gate, SOP resolution, decide, validate) runs
   outside any transaction, under the run deadline;
3. commit: one unit of work checks again that no other run finished the event,
   executes the verdict's actions, saves the run record and completes the event.

The tracer exports the run after the commit, and can never break it.
"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from freight_agent.application import escalation
from freight_agent.application.context import load_run_context
from freight_agent.application.contracts import (
    InboundMessage,
    IngestResult,
    LoadSnapshot,
    LocationPing,
)
from freight_agent.application.decide import Decider
from freight_agent.application.executor import Executor
from freight_agent.application.gate import Gate
from freight_agent.application.graph import GraphDeps, RunState, Verdict, build_graph, need
from freight_agent.application.prefilter import SkipReason
from freight_agent.domain.errors import DomainError
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.load import Channel, DelayRisk, Load, Stage
from freight_agent.domain.outbox import OutboxItem, OutboxKind, OutboxStatus
from freight_agent.domain.ping_filter import (
    PingFilterConfig,
    RiskTracker,
    RiskTransition,
    observe,
)
from freight_agent.domain.runs import RunRecord, RunStatus
from freight_agent.domain.timers import Timer, TimerStatus
from freight_agent.domain.validation import ValidationConfig
from freight_agent.ports.clock import Clock, IdGenerator
from freight_agent.ports.gateways import DeliveryError, Gateways
from freight_agent.ports.llm import LanguageModel, LlmError
from freight_agent.ports.repositories import EventLog, UnitOfWork, UnitOfWorkFactory
from freight_agent.ports.sop import SopRepository
from freight_agent.ports.tracing import Tracer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeConfig:
    ping_filter: PingFilterConfig = field(default_factory=PingFilterConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    recent_events_limit: int = 30
    run_deadline_s: float = 240.0
    max_delivery_attempts: int = 5
    llm_timeout_s: float = 60.0  # per model call; the adapter enforces it
    decide_max_tool_rounds: int = 2
    outbox_batch_size: int = 100  # items one `dispatch_outbox` call sends at most
    recent_pings_per_load: int = 64  # ping ids remembered to drop redeliveries
    # Production keeps the worker going: an unexpected error becomes a failed attempt
    # and the event is released for a retry. The eval harness sets this so that a
    # harness error (a missing cassette) ends the trial with its own message; the
    # event is still released first. Domain errors are failed attempts either way.
    reraise_unexpected: bool = False


@dataclass(frozen=True)
class RuntimeDeps:
    uow_factory: UnitOfWorkFactory
    clock: Clock
    ids: IdGenerator
    llm: LanguageModel
    sop: SopRepository
    gateways: Gateways
    tracer: Tracer
    config: RuntimeConfig = field(default_factory=RuntimeConfig)


@dataclass
class _Attempt:
    """One attempt at one claimed event.

    `state` follows the graph as it streams, so a failure still reports the gate
    decision, the plan and the model calls reached so far. It stays None when
    the context could not be read; `error` then holds why.
    """

    event: Event
    run_id: str
    started_at: datetime
    state: RunState | None = None
    error: Exception | None = None


@dataclass(frozen=True)
class _Finished:
    record: RunRecord
    stored: bool  # a failed attempt is stored only when it dead-letters the event


class _RecentPings:
    """The last ping ids of each load, in process memory.

    The integration layer delivers at least once, so the same ping can arrive
    twice; counted twice it would confirm a downgrade early. The ports have no
    place to keep ping ids durably, so this window lives in the process: it
    catches the usual quick redelivery, but a restart or a second ingest process
    forgets it. A durable version stores the last ping id with the risk tracker.
    """

    def __init__(self, size: int) -> None:
        self._size = size
        self._ids: dict[str, deque[str]] = {}

    def __contains__(self, key: tuple[str, str]) -> bool:
        load_id, ping_id = key
        return ping_id in self._ids.get(load_id, ())

    def add(self, load_id: str, ping_id: str) -> None:
        self._ids.setdefault(load_id, deque(maxlen=self._size)).append(ping_id)


class AgentRuntime:
    def __init__(self, deps: RuntimeDeps) -> None:
        self._deps = deps
        config = deps.config
        self._graph = build_graph(
            GraphDeps(
                gate=Gate(deps.llm, timeout_s=config.llm_timeout_s),
                decider=Decider(
                    deps.llm,
                    max_tool_rounds=config.decide_max_tool_rounds,
                    timeout_s=config.llm_timeout_s,
                ),
                sop=deps.sop,
                validation=config.validation,
            )
        )
        self._executor = Executor(deps.clock, deps.ids, config.validation.deferral)
        self._pings = _RecentPings(config.recent_pings_per_load)

    def _uow(self) -> UnitOfWork:
        return self._deps.uow_factory()

    # --- ingest -----------------------------------------------------------------------------

    async def register_load(self, load: Load) -> None:
        """Store the load metadata and start its risk tracker at the load's current risk."""
        async with self._uow() as uow:
            await uow.loads.save(load)
            await uow.loads.save_tracker(load.load_id, RiskTracker(band=load.delay_risk))
            await uow.commit()

    async def ingest_message(self, message: InboundMessage) -> IngestResult:
        """Dedupe by message_id, append to the event log and enqueue, in one transaction."""
        async with self._uow() as uow:
            if await uow.loads.get(message.load_id) is None:
                return _unknown_load(message.load_id)
            event = Event(
                event_id=message.message_id,
                load_id=message.load_id,
                type=EventType.INBOUND_MESSAGE,
                occurred_at=message.occurred_at,
                channel=message.channel,
                sender_contact_id=message.sender_contact_id,
                body=message.body,
                attachments=message.attachments,
            )
            if not await uow.events.append(event):
                return IngestResult(accepted=True, duplicate=True, event_id=event.event_id)
            await uow.queue.enqueue(event)
            await uow.commit()
        return IngestResult(accepted=True, event_id=event.event_id)

    async def ingest_ping(self, ping: LocationPing) -> IngestResult:
        """Update the risk tracker in code (no LLM).

        Only a confirmed transition into or out of HIGH becomes an event. Every
        confirmed transition saves the load with its new `delay_risk`, in the same
        unit of work as the tracker. A ping id seen recently is a redelivery and
        changes nothing (see `_RecentPings`).
        """
        if (ping.load_id, ping.ping_id) in self._pings:
            return IngestResult(accepted=True, duplicate=True, reason="ping already received")
        async with self._uow() as uow:
            load = await uow.loads.get(ping.load_id)
            if load is None:
                return _unknown_load(ping.load_id)
            tracker = await uow.loads.get_tracker(load.load_id) or RiskTracker(band=load.delay_risk)
            tracker, transition = observe(tracker, ping.delay_risk, self._deps.config.ping_filter)
            await uow.loads.save_tracker(load.load_id, tracker)
            result = IngestResult(accepted=True)
            if transition is not None:
                await uow.loads.save(load.model_copy(update={"delay_risk": transition.current}))
                if DelayRisk.HIGH in (transition.previous, transition.current):
                    result = await self._log_risk_change(uow, ping, transition)
            await uow.commit()
        self._pings.add(ping.load_id, ping.ping_id)
        return result

    async def _log_risk_change(
        self, uow: UnitOfWork, ping: LocationPing, transition: RiskTransition
    ) -> IngestResult:
        event = Event(
            event_id=f"risk:{ping.ping_id}",
            load_id=ping.load_id,
            type=EventType.RISK_CHANGED,
            occurred_at=ping.occurred_at,
            channel=Channel.SYSTEM,
            data={"previous": transition.previous.value, "current": transition.current.value},
        )
        if not await uow.events.append(event):  # an earlier process already logged it
            return IngestResult(accepted=True, duplicate=True, event_id=event.event_id)
        await uow.queue.enqueue(event)
        return IngestResult(accepted=True, event_id=event.event_id)

    # --- timers -----------------------------------------------------------------------------

    async def fire_due_timers(self) -> int:
        """Turn pending timers with due_at <= now into TIMER_DUE events. Returns how many fired.

        A fired timer is marked FIRED in the same unit of work, so it fires once.
        Whether it may still act is the prefilter's question (a stale timer).
        """
        async with self._uow() as uow:
            due = await uow.timers.due(self._deps.clock.now())
            for timer in due:
                await uow.timers.save(timer.mark(TimerStatus.FIRED))
                event = await self._timer_event(uow, timer)
                if await uow.events.append(event):
                    await uow.queue.enqueue(event)
            await uow.commit()
        return len(due)

    @staticmethod
    async def _timer_event(uow: UnitOfWork, timer: Timer) -> Event:
        obligation = await uow.obligations.get(timer.obligation_id)
        return Event(
            event_id=f"timer:{timer.timer_id}",
            load_id=timer.load_id,
            type=EventType.TIMER_DUE,
            occurred_at=timer.due_at,
            channel=Channel.SYSTEM,
            data={
                "timer_id": timer.timer_id,
                "obligation_id": timer.obligation_id,
                "kind": obligation.kind.value if obligation is not None else None,
            },
        )

    async def next_timer_due_at(self) -> datetime | None:
        async with self._uow() as uow:
            return await uow.timers.next_due_at()

    # --- runs -------------------------------------------------------------------------------

    async def process_next(self) -> RunRecord | None:
        """Claim the next queued event and run the agent graph on it.

        Outcomes besides acting:
        - `LlmError` or the run deadline: one urgent Hero task, `escalated`, and the
          event completes, so it is never retried into a duplicate.
        - Any other error: the attempt fails and the event goes back to the queue,
          which makes it a dead letter after the store's max attempts. A failed
          attempt is stored as a `failed` run only when it dead-letters the event:
          a stored run makes the prefilter skip the event as `already_processed`,
          so storing earlier failures would turn every retry into a skip. Earlier
          attempts are still returned and traced. With `reraise_unexpected`, an
          error that is not a `DomainError` is raised after the release.
        """
        attempt = await self._claim()
        if attempt is None:
            return None
        try:
            if attempt.error is not None:
                raise attempt.error
            finished = await self._commit(attempt, await self._think(attempt))
        except Exception as exc:
            finished = await self._fail(attempt, exc)
            if self._deps.config.reraise_unexpected and not isinstance(exc, DomainError):
                await self._export(finished)
                raise
        return await self._export(finished)

    async def process_all(self) -> list[RunRecord]:
        """Run events until nothing is claimable (each load runs one event at a time)."""
        runs: list[RunRecord] = []
        while (run := await self.process_next()) is not None:
            runs.append(run)
        return runs

    async def _claim(self) -> _Attempt | None:
        deps = self._deps
        async with self._uow() as uow:
            event = await uow.queue.claim_next()
            if event is None:
                return None
            attempt = _Attempt(event=event, run_id=deps.ids.new("run"), started_at=deps.clock.now())
            try:
                ctx = await load_run_context(
                    uow,
                    event,
                    now=attempt.started_at,
                    recent_limit=deps.config.recent_events_limit,
                    outbound_window_minutes=deps.config.validation.sms_window_minutes,
                )
                attempt.state = RunState(ctx=ctx)
            except Exception as exc:  # the claim still commits: the failure path releases it
                attempt.error = exc
            await uow.commit()
        return attempt

    async def _think(self, attempt: _Attempt) -> Verdict:
        """Run the graph outside any transaction, under the run deadline."""
        ctx = need(attempt.state, "a run context").ctx
        deadline_s = self._deps.config.run_deadline_s
        deadline = asyncio.timeout(deadline_s)
        try:
            async with deadline:
                async for state in self._graph.stream(ctx, run_id=attempt.run_id):
                    attempt.state = state
        except LlmError as exc:
            return Verdict.escalated(escalation.model_failure(ctx, exc))
        except TimeoutError:
            if not deadline.expired():  # a timeout of something else, not the run's
                raise
            return Verdict.escalated(escalation.deadline_passed(ctx, deadline_s))
        return need(need(attempt.state, "a run state").verdict, "a verdict")

    async def _commit(self, attempt: _Attempt, verdict: Verdict) -> _Finished:
        state = need(attempt.state, "a run state")
        event_id = attempt.event.event_id
        async with self._uow() as uow:
            done = await uow.runs.for_event(event_id) is not None
            if done and verdict.status is not RunStatus.SKIPPED:
                # Another worker finished the event while this run was thinking.
                verdict = Verdict(
                    status=RunStatus.SKIPPED, skip_reason=SkipReason.ALREADY_PROCESSED.value
                )
            await self._executor.apply(
                uow, run_id=attempt.run_id, ctx=state.ctx, actions=verdict.actions
            )
            record = self._record(attempt, verdict)
            await uow.runs.save(record)
            await uow.queue.complete(event_id)
            await uow.commit()
        return _Finished(record, stored=True)

    async def _fail(self, attempt: _Attempt, exc: Exception) -> _Finished:
        error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "run %s failed on event %s: %s",
            attempt.run_id,
            attempt.event.event_id,
            error,
            exc_info=exc,
        )
        record = self._record(attempt, Verdict(status=RunStatus.FAILED, error=error))
        event_id = attempt.event.event_id
        async with self._uow() as uow:
            await uow.queue.release(event_id, error)
            dead = any(e.event_id == event_id for e in await uow.queue.dead_letters())
            if dead:
                await uow.runs.save(record)
            await uow.commit()
        return _Finished(record, stored=dead)

    def _record(self, attempt: _Attempt, verdict: Verdict) -> RunRecord:
        state = attempt.state
        return RunRecord(
            run_id=attempt.run_id,
            event_id=attempt.event.event_id,
            load_id=attempt.event.load_id,
            started_at=attempt.started_at,
            finished_at=self._deps.clock.now(),
            status=verdict.status,
            gate=state.gate_decision if state else None,
            sop_section_ids=state.section_ids if state else (),
            plan=state.plan if state else None,
            violations=state.violations if state else (),
            usage=state.usage if state else (),
            skip_reason=verdict.skip_reason,
            error=verdict.error,
        )

    async def _export(self, finished: _Finished) -> RunRecord:
        """Trace the run, fail-open. A stored run keeps its trace URL."""
        record = finished.record
        try:
            url = await self._deps.tracer.record_run(record)
            if url is not None:
                record = record.model_copy(update={"trace_url": url})
                if finished.stored:
                    async with self._uow() as uow:
                        await uow.runs.save(record)
                        await uow.commit()
        except Exception:
            logger.warning("tracing run %s failed", record.run_id, exc_info=True)
        return record

    # --- delivery ---------------------------------------------------------------------------

    async def dispatch_outbox(self) -> int:
        """Deliver pending outbox items through the gateways. Returns how many were sent.

        Each item is sent outside any unit of work and its new status saved in its
        own. A `DeliveryError` keeps the item pending for the next dispatch, until
        `max_delivery_attempts` makes it failed. Gateways ignore a repeated
        idempotency key, so a resend after a crash is harmless.
        """
        async with self._uow() as uow:
            pending = await uow.outbox.pending(self._deps.config.outbox_batch_size)
        sent = 0
        for item in pending:
            updated = await self._deliver(item)
            async with self._uow() as uow:
                await uow.outbox.save(updated)
                await uow.commit()
            if updated.status is OutboxStatus.SENT:
                sent += 1
        return sent

    async def _deliver(self, item: OutboxItem) -> OutboxItem:
        attempts = item.attempts + 1
        try:
            await _send(self._deps.gateways, item)
        except DeliveryError as exc:
            final = attempts >= self._deps.config.max_delivery_attempts
            status = OutboxStatus.FAILED if final else OutboxStatus.PENDING
            update = {"attempts": attempts, "last_error": str(exc), "status": status}
            return item.model_copy(update=update)
        except (KeyError, TypeError, ValueError) as exc:  # retrying cannot fix the payload
            update = {
                "attempts": attempts,
                "last_error": f"malformed payload: {type(exc).__name__}: {exc}",
                "status": OutboxStatus.FAILED,
            }
            return item.model_copy(update=update)
        return item.model_copy(update={"attempts": attempts, "status": OutboxStatus.SENT})

    async def tick(self) -> list[RunRecord]:
        """fire_due_timers, then process_all, then dispatch_outbox."""
        await self.fire_due_timers()
        runs = await self.process_all()
        await self.dispatch_outbox()
        return runs

    # --- read model -------------------------------------------------------------------------

    async def snapshot(self, load_id: str) -> LoadSnapshot:
        """Everything stored for the load. Raises `DomainError` for an unknown load."""
        async with self._uow() as uow:
            load = await uow.loads.get(load_id)
            if load is None:
                raise DomainError(f"unknown load {load_id!r}")
            return LoadSnapshot(
                load=load,
                obligations=tuple(await uow.obligations.list_for_load(load_id)),
                timers=tuple(await uow.timers.list_for_load(load_id)),
                events=tuple(await _all_events(uow.events, load_id)),
                outbox=tuple(await uow.outbox.list_for_load(load_id)),
                runs=tuple(await uow.runs.list_for_load(load_id)),
            )


def _unknown_load(load_id: str) -> IngestResult:
    return IngestResult(accepted=False, reason=f"unknown load {load_id!r}")


async def _send(gateways: Gateways, item: OutboxItem) -> None:
    """One outbox item through its gateway (payload keys: docs/decisions.md)."""
    payload, key, load_id = item.payload, item.idempotency_key, item.load_id
    match item.kind:
        case OutboxKind.SMS:
            await gateways.sms.send(
                load_id=load_id,
                contact_id=_text(payload["contact_id"]),
                text=_text(payload["text"]),
                idempotency_key=key,
            )
        case OutboxKind.EMAIL:
            await gateways.email.send(
                load_id=load_id,
                to_contact_ids=tuple(_text(c) for c in payload["to_contact_ids"]),
                subject=payload.get("subject"),
                body=_text(payload["body"]),
                idempotency_key=key,
            )
        case OutboxKind.BROKER_SLACK:
            await gateways.broker_slack.post(
                broker_id=_text(payload["broker_id"]),
                load_id=load_id,
                text=_text(payload["text"]),
                idempotency_key=key,
            )
        case OutboxKind.TMS_NOTE:
            await gateways.tms.write_note(
                load_id=load_id, text=_text(payload["text"]), idempotency_key=key
            )
        case OutboxKind.HERO_TASK:
            await gateways.internal.create_hero_task(
                load_id=load_id,
                title=_text(payload["title"]),
                description=_text(payload["description"]),
                urgent=bool(payload["urgent"]),
                idempotency_key=key,
            )
        case OutboxKind.INTERNAL_SLACK:
            await gateways.internal.post_internal_slack(
                load_id=load_id, text=_text(payload["text"]), idempotency_key=key
            )
        case OutboxKind.STAGE_UPDATE:
            await gateways.internal.update_stage(
                load_id=load_id, stage=Stage(payload["stage"]), idempotency_key=key
            )


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected text, got {type(value).__name__}")
    return value


async def _all_events(events: EventLog, load_id: str) -> list[Event]:
    """Every event of the load, oldest first, through the paged `recent` port."""
    size = 256
    while len(page := await events.recent(load_id, size)) >= size:
        size *= 2
    return page
