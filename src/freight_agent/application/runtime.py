"""AgentRuntime: the one facade the API, the CLI and the eval harness use.

The same object runs in production, in the simulation and in evals; only the
adapters passed in differ (composition root: `freight_agent.bootstrap`).

A run (`process_next`) has three phases, so that no unit of work is open while
a model thinks (the memory store serializes units of work behind one lock, and
a database would hold the claimed rows):

1. claim: one unit of work claims the next event, reads its context
   (`load_run_context`) and commits the claim;
2. think: the run graph (prefilter, triage, knowledge, agent, validate) runs
   outside any transaction, under the run deadline; it reads the SOP through the
   knowledge index (`RuntimeDeps.knowledge`), which is not on the unit of work, and
   the agent's read tools answer from the context read in phase 1;
3. commit: one unit of work checks again that no other run finished the event,
   executes the verdict's actions, saves the run record and completes the event.

A worker that dies between claim and commit (a crash, a cancelled task, a failure
while releasing) leaves its event in flight. The claim lease takes it back: every
`process_all` first releases the claims older than `claim_lease_s`, each counted as a
failed attempt (the watchdog of the plan).

The tracer exports the run after the commit, and can never break it.

A tick also runs the document pipeline (`RuntimeDeps.documents`) between firing the
timers and processing the queue, so a message's attachment is read before the queue
runs and its `document_processed` event is queued behind the message.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import cast

from freight_agent.application import escalation
from freight_agent.application.agent import Agent
from freight_agent.application.context import load_run_context
from freight_agent.application.contracts import (
    InboundMessage,
    IngestResult,
    LoadSnapshot,
    LocationPing,
)
from freight_agent.application.documents import NullDocumentProcessor
from freight_agent.application.executor import ExecutionResult, Executor
from freight_agent.application.gate import Gate
from freight_agent.application.graph import GraphDeps, RunState, Verdict, build_graph, need
from freight_agent.application.prefilter import SkipReason
from freight_agent.domain.documents import Document
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
from freight_agent.ports.documents import BlobStore, DocumentProcessor
from freight_agent.ports.gateways import DeliveryError, Gateways
from freight_agent.ports.knowledge import SopIndex
from freight_agent.ports.llm import LanguageModel, LlmError
from freight_agent.ports.repositories import EventLog, UnitOfWork, UnitOfWorkFactory
from freight_agent.ports.tracing import Tracer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeConfig:
    """The process's settings. What a run may do (validation limits, the agent's tool
    rounds, cost and rejection budgets) is its broker's (`Broker.limits`), not here."""

    ping_filter: PingFilterConfig = field(default_factory=PingFilterConfig)
    # Not read by the runs since wave 3: each run validates with its broker's
    # `BrokerLimits.validation`. Kept because the eval metadata still hashes it.
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    recent_events_limit: int = 30  # the transcript in the agent's prompt
    history_events_limit: int = 200  # what the agent's get_history tool can read
    run_deadline_s: float = 240.0
    # How long a claimed event may stay in flight before `process_all` takes it back;
    # None: `run_deadline_s` plus a minute, so a run that is still thinking keeps it.
    claim_lease_s: float | None = None
    max_delivery_attempts: int = 5
    llm_timeout_s: float = 60.0  # per model call (the whole tool loop); the adapter enforces it
    outbox_batch_size: int = 100  # items one `dispatch_outbox` call sends at most
    recent_pings_per_load: int = 64  # ping ids the risk tracker keeps to drop redeliveries
    # Production keeps the worker going: an unexpected error becomes a failed attempt
    # and the event is released for a retry. The eval harness sets this so that a
    # harness error (a missing cassette) ends the trial with its own message; the
    # event is still released first. Domain errors are failed attempts either way.
    reraise_unexpected: bool = False

    @property
    def effective_claim_lease_s(self) -> float:
        if self.claim_lease_s is not None:
            return self.claim_lease_s
        return self.run_deadline_s + _LEASE_MARGIN_S


# Past the run deadline, the commit phase still has to finish before a claim may expire.
_LEASE_MARGIN_S = 60.0


@dataclass(frozen=True)
class RuntimeDeps:
    uow_factory: UnitOfWorkFactory
    clock: Clock
    ids: IdGenerator
    llm: LanguageModel
    knowledge: SopIndex  # the SOP the think phase reads, outside any unit of work
    gateways: Gateways
    tracer: Tracer
    blobs: BlobStore  # attachment bytes, for the document pipeline
    documents: DocumentProcessor = field(default_factory=NullDocumentProcessor)
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


class AgentRuntime:
    def __init__(self, deps: RuntimeDeps) -> None:
        self._deps = deps
        config = deps.config
        self._graph = build_graph(
            GraphDeps(
                gate=Gate(deps.llm, timeout_s=config.llm_timeout_s),
                agent=Agent(deps.llm, deps.knowledge, timeout_s=config.llm_timeout_s),
                knowledge=deps.knowledge,
            )
        )
        self._executor = Executor(deps.clock, deps.ids)

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
        """Dedupe by message_id, append to the event log and enqueue, in one transaction.

        Each attachment of a new message is saved as a pending document in the same
        transaction, for the document pipeline of the next tick.
        """
        async with self._uow() as uow:
            load = await uow.loads.get(message.load_id)
            if load is None:
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
            for attachment in message.attachments:
                await uow.documents.save(
                    Document.pending(
                        document_id=self._deps.ids.new("doc"),
                        load_id=load.load_id,
                        attachment_id=attachment.attachment_id,
                        broker_id=load.broker_id,
                        blob_ref=attachment.ref,
                        filename=attachment.filename,
                        content_type=attachment.content_type,
                        received_at=message.occurred_at,
                    )
                )
            await uow.commit()
        return IngestResult(accepted=True, event_id=event.event_id)

    async def ingest_ping(self, ping: LocationPing) -> IngestResult:
        """Update the risk tracker in code (no LLM).

        Only a confirmed transition into or out of HIGH becomes an event. Every
        confirmed transition saves the load with its new `delay_risk`, in the same
        unit of work as the tracker. A ping id the tracker remembers is a redelivery
        and changes nothing; the tracker keeps the ids, so this holds across restarts
        and processes (the load row lock serializes two ingests of one load).
        """
        config = self._deps.config
        async with self._uow() as uow:
            load = await uow.loads.get(ping.load_id)
            if load is None:
                return _unknown_load(ping.load_id)
            tracker = await uow.loads.get_tracker(load.load_id) or RiskTracker(band=load.delay_risk)
            if tracker.has_seen(ping.ping_id):
                return IngestResult(accepted=True, duplicate=True, reason="ping already received")
            tracker, transition = observe(tracker, ping.delay_risk, config.ping_filter)
            tracker = tracker.remember(ping.ping_id, keep=config.recent_pings_per_load)
            await uow.loads.save_tracker(load.load_id, tracker)
            result = IngestResult(accepted=True)
            if transition is not None:
                await uow.loads.save(load.model_copy(update={"delay_risk": transition.current}))
                if DelayRisk.HIGH in (transition.previous, transition.current):
                    result = await self._log_risk_change(uow, ping, transition)
            await uow.commit()
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
        now = self._deps.clock.now()
        async with self._uow() as uow:
            due = await uow.timers.due(now)
            for timer in due:
                await uow.timers.save(timer.mark(TimerStatus.FIRED))
                event = await self._timer_event(uow, timer, fired_at=now)
                if await uow.events.append(event):
                    await uow.queue.enqueue(event)
            await uow.commit()
        return len(due)

    @staticmethod
    async def _timer_event(uow: UnitOfWork, timer: Timer, *, fired_at: datetime) -> Event:
        """Stamped with the firing time; `data.due_at` keeps the due time, so timer lag
        (fired minus due) is measurable."""
        obligation = await uow.obligations.get(timer.obligation_id)
        return Event(
            event_id=f"timer:{timer.timer_id}",
            load_id=timer.load_id,
            type=EventType.TIMER_DUE,
            occurred_at=fired_at,
            channel=Channel.SYSTEM,
            data={
                "timer_id": timer.timer_id,
                "obligation_id": timer.obligation_id,
                "due_at": timer.due_at.isoformat(),
                "kind": obligation.kind if obligation is not None else None,
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
        """Take back the expired claims, then run events until nothing is claimable
        (each load runs one event at a time)."""
        await self._reclaim_expired()
        runs: list[RunRecord] = []
        while (run := await self.process_next()) is not None:
            runs.append(run)
        return runs

    async def _reclaim_expired(self) -> None:
        """Release the events whose claim outlived the lease: their worker died.

        A run that is only slow and commits after this finds its event back in the queue:
        its completion leaves the queued event alone, and the event's next run is skipped
        as already processed. The lease is longer than the run deadline, so that is rare.
        """
        lease_s = self._deps.config.effective_claim_lease_s
        async with self._uow() as uow:
            reclaimed = await uow.queue.reclaim_expired(self._deps.clock.now(), lease_s)
            await uow.commit()
        if reclaimed:
            logger.warning("reclaimed %d event(s) in flight for more than %g s", reclaimed, lease_s)

    async def _claim(self) -> _Attempt | None:
        deps = self._deps
        async with self._uow() as uow:
            now = deps.clock.now()
            event = await uow.queue.claim_next(now)
            if event is None:
                return None
            attempt = _Attempt(event=event, run_id=deps.ids.new("run"), started_at=now)
            try:
                ctx = await load_run_context(
                    uow,
                    event,
                    knowledge=deps.knowledge,
                    now=attempt.started_at,
                    recent_limit=deps.config.recent_events_limit,
                    history_limit=deps.config.history_events_limit,
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
            result = await self._executor.apply(
                uow, run_id=attempt.run_id, ctx=state.ctx, actions=verdict.actions
            )
            record = self._record(attempt, _with_skips(verdict, result))
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
        broker = state.ctx.broker if state else None
        return RunRecord(
            run_id=attempt.run_id,
            event_id=attempt.event.event_id,
            load_id=attempt.event.load_id,
            started_at=attempt.started_at,
            finished_at=self._deps.clock.now(),
            status=verdict.status,
            gate=state.gate_decision if state else None,
            sop_section_ids=state.section_ids if state else (),
            sop_version=state.sop_version if state else None,
            plan=state.plan if state else None,
            violations=state.violations if state else (),
            usage=state.usage if state else (),
            skip_reason=verdict.skip_reason,
            error=verdict.error,
            intent=state.intent if state else None,
            broker_id=broker.broker_id if broker else None,
            trajectory=state.trajectory if state else (),
            sections_searched=state.sections_searched if state else (),
            rejections=state.rejections if state else 0,
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

        Each item is sent outside any unit of work and its new status saved in its own.
        The payload is checked before the call: a malformed one fails the item at once,
        since no retry can fix it. Anything the gateway call raises keeps the item pending
        for the next dispatch, until `max_delivery_attempts` makes it failed, and the
        other items of the batch are still sent. Gateways ignore a repeated idempotency
        key, so a resend after a crash is harmless.
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
            send = _delivery(item)
        except (KeyError, TypeError, ValueError) as exc:  # retrying cannot fix the payload
            update = {
                "attempts": attempts,
                "last_error": f"malformed payload: {type(exc).__name__}: {exc}",
                "status": OutboxStatus.FAILED,
            }
            return item.model_copy(update=update)
        try:
            await send(self._deps.gateways)
        except Exception as exc:  # the payload is sound, so the failure may pass: retry
            error = str(exc) if isinstance(exc, DeliveryError) else f"{type(exc).__name__}: {exc}"
            final = attempts >= self._deps.config.max_delivery_attempts
            logger.warning(
                "delivering outbox item %s failed (attempt %d%s): %s",
                item.outbox_id,
                attempts,
                ", final" if final else "",
                error,
                exc_info=not isinstance(exc, DeliveryError),
            )
            status = OutboxStatus.FAILED if final else OutboxStatus.PENDING
            update = {"attempts": attempts, "last_error": error, "status": status}
            return item.model_copy(update=update)
        return item.model_copy(update={"attempts": attempts, "status": OutboxStatus.SENT})

    async def tick(self) -> list[RunRecord]:
        """fire_due_timers, then the document pipeline, then process_all, then dispatch_outbox."""
        await self.fire_due_timers()
        await self._deps.documents.process_pending()
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


def _with_skips(verdict: Verdict, result: ExecutionResult) -> Verdict:
    """The verdict as executed: the executor's skips go in `skip_reason`, and a run that
    acted on nothing (every approved action skipped) is a noop. A run with some actions
    applied still acted."""
    if not result.skipped:
        return verdict
    skips = "; ".join(f"action {k.index} ({k.type}): {k.reason}" for k in result.skipped)
    nothing_done = len(result.skipped) == len(verdict.actions)
    status = (
        RunStatus.NOOP if verdict.status is RunStatus.ACTED and nothing_done else verdict.status
    )
    return replace(verdict, status=status, skip_reason=f"executor skipped {skips}")


def _unknown_load(load_id: str) -> IngestResult:
    return IngestResult(accepted=False, reason=f"unknown load {load_id!r}")


# One outbox item's gateway call, its payload already read.
_Send = Callable[[Gateways], Awaitable[None]]


def _delivery(item: OutboxItem) -> _Send:
    """The gateway call of one outbox item (payload keys: docs/decisions.md).

    The payload is read here, before any call, so that a malformed one (`KeyError`,
    `TypeError`, `ValueError`) is never confused with a gateway that failed.
    """
    payload, key, load_id = item.payload, item.idempotency_key, item.load_id
    match item.kind:
        case OutboxKind.SMS:
            contact_id, text = _text(payload["contact_id"]), _text(payload["text"])
            return lambda g: g.sms.send(
                load_id=load_id, contact_id=contact_id, text=text, idempotency_key=key
            )
        case OutboxKind.EMAIL:
            to_contact_ids = _texts(payload["to_contact_ids"])
            subject, body = _optional_text(payload.get("subject")), _text(payload["body"])
            return lambda g: g.email.send(
                load_id=load_id,
                to_contact_ids=to_contact_ids,
                subject=subject,
                body=body,
                idempotency_key=key,
            )
        case OutboxKind.BROKER_SLACK:
            broker_id, text = _text(payload["broker_id"]), _text(payload["text"])
            return lambda g: g.broker_slack.post(
                broker_id=broker_id, load_id=load_id, text=text, idempotency_key=key
            )
        case OutboxKind.TMS_NOTE:
            text = _text(payload["text"])
            return lambda g: g.tms.write_note(load_id=load_id, text=text, idempotency_key=key)
        case OutboxKind.HERO_TASK:
            title, description = _text(payload["title"]), _text(payload["description"])
            urgent = _flag(payload["urgent"])
            return lambda g: g.internal.create_hero_task(
                load_id=load_id,
                title=title,
                description=description,
                urgent=urgent,
                idempotency_key=key,
            )
        case OutboxKind.INTERNAL_SLACK:
            text = _text(payload["text"])
            return lambda g: g.internal.post_internal_slack(
                load_id=load_id, text=text, idempotency_key=key
            )
        case OutboxKind.STAGE_UPDATE:
            stage = Stage(payload["stage"])
            return lambda g: g.internal.update_stage(
                load_id=load_id, stage=stage, idempotency_key=key
            )


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected text, got {type(value).__name__}")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _texts(value: object) -> tuple[str, ...]:
    """A list of strings; a bare string is refused, not read as its characters."""
    if not isinstance(value, list | tuple):
        raise TypeError(f"expected a list of text, got {type(value).__name__}")
    return tuple(_text(item) for item in cast(Sequence[object], value))


def _flag(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"expected true or false, got {type(value).__name__}")
    return value


async def _all_events(events: EventLog, load_id: str) -> list[Event]:
    """Every event of the load, oldest first, through the paged `recent` port."""
    size = 256
    while len(page := await events.recent(load_id, size)) >= size:
        size *= 2
    return page
