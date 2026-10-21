"""The executor turns approved actions into ledger changes, timers and outbox items.

It runs inside the caller's unit of work and never commits: the run's state
changes and its side effects are stored in one transaction. Delivery happens
later, from the outbox. Semantics are listed in docs/decisions.md.

An approved action that would still break a domain invariant (a timer for an
obligation that is not open, a second deferral) is skipped and reported in
`ExecutionResult.skipped`; the rest of the plan runs. The validator normally
stops these actions earlier, and one bad action must not cost the run its other
side effects. Ledger-only actions check every invariant before they write, and
outbound actions cannot break one, so a skipped action leaves no partial change.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from freight_agent.application.context import RunContext
from freight_agent.domain.actions import (
    Action,
    CloseObligation,
    CreateHeroTask,
    DeferObligation,
    OpenObligation,
    PostBrokerSlack,
    PostInternalSlack,
    ScheduleTimer,
    SendEmail,
    SendSms,
    UpdateStage,
    WriteTmsNote,
)
from freight_agent.domain.documents import DocumentStatus
from freight_agent.domain.errors import InvariantViolation
from freight_agent.domain.events import Direction, Event, EventType
from freight_agent.domain.load import Channel, Load, Stage
from freight_agent.domain.obligations import DeferralPolicy, Obligation, ObligationStatus
from freight_agent.domain.outbox import OutboxItem, OutboxKind
from freight_agent.domain.timers import Timer, TimerStatus, follow_up_due_at, later
from freight_agent.interfaces.clock import Clock, IdGenerator
from freight_agent.interfaces.repositories import UnitOfWork


@dataclass(frozen=True)
class SkippedAction:
    """An approved action the executor could not apply. The rest of the plan still ran."""

    index: int  # position in the approved list
    type: str  # the action's `type`
    reason: str


@dataclass(frozen=True)
class ExecutionResult:
    load: Load  # after any stage update
    outbox: tuple[OutboxItem, ...] = ()  # items created by this run
    obligations: tuple[Obligation, ...] = ()  # obligations created or changed
    timers: tuple[Timer, ...] = ()  # timers created or changed (incl. superseded, cancelled)
    skipped: tuple[SkippedAction, ...] = ()  # impossible or already applied actions


class Executor:
    """Applies a run's approved actions. The deferral policy and the document requirements
    are the run's broker's (`ctx.broker`)."""

    def __init__(
        self,
        clock: Clock,
        ids: IdGenerator,
        *,
        delivery_followups: bool = False,
    ) -> None:
        self._clock = clock
        self._ids = ids
        self._delivery_followups = delivery_followups

    async def apply(
        self, uow: UnitOfWork, *, run_id: str, ctx: RunContext, actions: Sequence[Action]
    ) -> ExecutionResult:
        policy = ctx.broker.limits.validation.deferral
        execution = _Execution(
            uow,
            run_id=run_id,
            ctx=ctx,
            now=self._clock.now(),
            ids=self._ids,
            policy=policy,
            delivery_followups=self._delivery_followups,
        )
        for index, action in enumerate(actions):
            await execution.apply(index, action)
        return execution.result()


# Which channel an agent_action event is logged on, by outbox kind.
_CHANNEL_BY_KIND: Mapping[OutboxKind, Channel] = {
    OutboxKind.SMS: Channel.SMS,
    OutboxKind.EMAIL: Channel.EMAIL,
    OutboxKind.BROKER_SLACK: Channel.SLACK,
    OutboxKind.HERO_TASK: Channel.HERO,
    OutboxKind.INTERNAL_SLACK: Channel.HERO,
    OutboxKind.TMS_NOTE: Channel.SYSTEM,
    OutboxKind.STAGE_UPDATE: Channel.SYSTEM,
}


@dataclass(frozen=True)
class _Message:
    """What an action puts in the outbox, and the text the event log keeps of it."""

    kind: OutboxKind
    payload: dict[str, Any]
    body: str


def _message_for(action: Action, load: Load) -> _Message | None:
    """The outbox entry of an action; None for ledger-only actions."""
    match action:
        case SendSms(contact_id=contact_id, text=text, purpose=purpose):
            payload = {"contact_id": contact_id, "text": text, "purpose": purpose.value}
            return _Message(OutboxKind.SMS, payload, text)
        case SendEmail(to_contact_ids=to, subject=subject, body=body, purpose=purpose):
            payload = {
                "to_contact_ids": list(to),
                "subject": subject,
                "body": body,
                "purpose": purpose.value,
            }
            return _Message(OutboxKind.EMAIL, payload, body)
        case PostBrokerSlack(text=text):
            return _Message(
                OutboxKind.BROKER_SLACK, {"broker_id": load.broker_id, "text": text}, text
            )
        case WriteTmsNote(text=text):
            return _Message(OutboxKind.TMS_NOTE, {"text": text}, text)
        case CreateHeroTask(title=title, description=description, urgent=urgent):
            payload = {"title": title, "description": description, "urgent": urgent}
            body = f"{title}\n{description}" if description else title
            return _Message(OutboxKind.HERO_TASK, payload, body)
        case PostInternalSlack(text=text):
            return _Message(OutboxKind.INTERNAL_SLACK, {"text": text}, text)
        case UpdateStage(stage=stage):
            body = f"Stage updated to {stage.value}"
            return _Message(OutboxKind.STAGE_UPDATE, {"stage": stage.value}, body)
        case _:  # ScheduleTimer, DeferObligation, CloseObligation, OpenObligation
            return None


class _Execution:
    """The working state of one `Executor.apply` call.

    Rows are read from the unit of work, not from the run's snapshot, because
    every write must start from the transaction's current row: a timer may have
    fired since the context was loaded, and an earlier action of the same plan
    may have opened or changed the obligation.
    """

    def __init__(
        self,
        uow: UnitOfWork,
        *,
        run_id: str,
        ctx: RunContext,
        now: datetime,
        ids: IdGenerator,
        policy: DeferralPolicy,
        delivery_followups: bool = False,
    ) -> None:
        self._uow = uow
        self._run_id = run_id
        self._event = ctx.event
        self._load = ctx.load
        self._broker = ctx.broker
        self._now = now
        self._ids = ids
        self._policy = policy
        self._delivery_followups = delivery_followups
        self._grace = timedelta(minutes=policy.grace_minutes)
        self._outbox: list[OutboxItem] = []
        self._obligations: dict[str, Obligation] = {}  # latest version, in first-change order
        self._timers: dict[str, Timer] = {}
        self._skipped: list[SkippedAction] = []

    def result(self) -> ExecutionResult:
        return ExecutionResult(
            load=self._load,
            outbox=tuple(self._outbox),
            obligations=tuple(self._obligations.values()),
            timers=tuple(self._timers.values()),
            skipped=tuple(self._skipped),
        )

    async def apply(self, index: int, action: Action) -> None:
        try:
            message = _message_for(action, self._load)
            if message is not None and not await self._record(index, message):
                key = self._idempotency_key(index)
                self._skip(index, action, f"already applied: {key} is in the outbox")
                return
            await self._change_state(action)
            if self._delivery_followups:
                match action:
                    case SendSms(requests=str() as kind) | SendEmail(requests=str() as kind):
                        await self._bind_followup(kind)
                    case _:
                        pass
        except InvariantViolation as exc:
            self._skip(index, action, str(exc))

    def _skip(self, index: int, action: Action, reason: str) -> None:
        self._skipped.append(SkippedAction(index=index, type=action.type, reason=reason))

    # --- outbox and memory ---------------------------------------------------------------------

    def _idempotency_key(self, index: int) -> str:
        return f"{self._event.event_id}:{index}"

    async def _record(self, index: int, message: _Message) -> bool:
        """Add the outbox item and its agent_action event. False if the key is already there."""
        item = OutboxItem(
            outbox_id=self._ids.new("out"),
            idempotency_key=self._idempotency_key(index),
            run_id=self._run_id,
            load_id=self._load.load_id,
            kind=message.kind,
            payload=message.payload,
            created_at=self._now,
        )
        if not await self._uow.outbox.add(item):
            return False
        self._outbox.append(item)
        await self._uow.events.append(self._agent_action(item, message.body))
        return True

    def _agent_action(self, item: OutboxItem, body: str) -> Event:
        """The agent's own action as an event, so later runs remember what it did."""
        return Event(
            event_id=f"act:{item.idempotency_key}",
            load_id=item.load_id,
            type=EventType.AGENT_ACTION,
            occurred_at=self._now,
            channel=_CHANNEL_BY_KIND[item.kind],
            direction=Direction.OUTBOUND,
            body=body,
            data={
                "kind": item.kind.value,
                "payload": dict(item.payload),
                "run_id": item.run_id,
                "outbox_id": item.outbox_id,
            },
        )

    # --- ledger, timers and stage --------------------------------------------------------------

    async def _change_state(self, action: Action) -> None:
        match action:
            case SendSms(requests=str() as kind) | SendEmail(requests=str() as kind):
                await self._count_request(kind)
            case OpenObligation(kind=kind):
                await self._open(kind)
            case UpdateStage(stage=stage):
                await self._update_stage(stage)
                await self._open_requirements(stage)
            case ScheduleTimer(kind=kind, due_in_minutes=minutes):
                await self._schedule_timer(kind, minutes)
            case DeferObligation(kind=kind, minutes=minutes):
                await self._defer(kind, minutes)
            case CloseObligation(kind=kind, status=status):
                await self._close(kind, ObligationStatus(status.value))
            case _:
                pass  # outbox only

    async def _count_request(self, kind: str) -> None:
        """An SMS or an email that asks for `kind` (`requests`) opens its obligation
        (attempts 1) or counts one more attempt. The validator refuses a kind outside the
        broker's catalog and a request over the kind's attempt cap."""
        current = await self._uow.obligations.get_open(self._load.load_id, kind)
        if current is None:
            updated = Obligation.open(
                obligation_id=self._ids.new("obl"),
                load_id=self._load.load_id,
                kind=kind,
                at=self._now,
                first_attempt=True,
            )
        else:
            updated = current.record_attempt(self._now)
        await self._save_obligation(updated)

    async def _bind_followup(self, kind: str) -> None:
        obligation = await self._open_obligation(kind, "bind delivery")
        definition = self._broker.obligation(kind)
        if definition is None:
            raise InvariantViolation(f"unknown obligation kind: {kind}")
        item = self._outbox[-1]
        item = item.model_copy(
            update={
                "payload": {
                    **item.payload,
                    "follow_up": {
                        "obligation_id": obligation.obligation_id,
                        "minutes": definition.follow_up_minutes,
                        "grace_minutes": self._policy.grace_minutes,
                    },
                }
            }
        )
        self._outbox[-1] = item
        await self._uow.outbox.save(item)

    async def _open(self, kind: str) -> None:
        """Open an obligation of `kind` with no attempt yet (a document the load owes)."""
        if await self._uow.obligations.get_open(self._load.load_id, kind) is not None:
            raise InvariantViolation(f"an open {kind} obligation already exists")
        opened = Obligation.open(
            obligation_id=self._ids.new("obl"),
            load_id=self._load.load_id,
            kind=kind,
            at=self._now,
            first_attempt=False,
        )
        await self._save_obligation(opened)

    async def _update_stage(self, stage: Stage) -> None:
        current = await self._uow.loads.get(self._load.load_id) or self._load
        self._load = current.model_copy(update={"stage": stage})
        await self._uow.loads.save(self._load)

    async def _open_requirements(self, stage: Stage) -> None:
        """The documents the broker requires once the load reaches `stage` (a POD at
        delivery): each requirement's `opens_obligation` opens with no attempt, with a
        follow-up timer after `within_minutes` when set. Nothing opens when one is already
        open, or when a clean processed document of that type is already in hand (the
        document pipeline closes the obligation only once it exists)."""
        for requirement in self._broker.requirements_at(stage):
            kind = requirement.opens_obligation
            if kind is None:
                continue
            if await self._uow.obligations.get_open(self._load.load_id, kind) is not None:
                continue
            if await self._has_clean_document(requirement.type_id):
                continue
            opened = Obligation.open(
                obligation_id=self._ids.new("obl"),
                load_id=self._load.load_id,
                kind=kind,
                at=self._now,
                first_attempt=False,
            )
            await self._save_obligation(opened)
            if requirement.within_minutes is not None:
                due_at = follow_up_due_at(
                    requested=later(self._now, timedelta(minutes=requirement.within_minutes)),
                    deferred_until=None,
                    grace=self._grace,
                )
                reason = f"{requirement.type_id} due within {requirement.within_minutes} min"
                await self._replace_timer(opened, due_at, reason=reason)

    async def _has_clean_document(self, type_id: str) -> bool:
        documents = await self._uow.documents.list_for_load(self._load.load_id)
        return any(d.type_id == type_id and d.status == DocumentStatus.PROCESSED for d in documents)

    async def _schedule_timer(self, kind: str, minutes: int) -> None:
        obligation = await self._open_obligation(kind, "schedule a timer")
        if self._delivery_followups and any(
            item.payload.get("follow_up", {}).get("obligation_id") == obligation.obligation_id
            for item in self._outbox
        ):
            return  # the catalog's cadence starts when this request is confirmed
        due_at = follow_up_due_at(
            requested=later(self._now, timedelta(minutes=minutes)),
            deferred_until=obligation.deferred_until,
            grace=self._grace,
        )
        await self._replace_timer(obligation, due_at, reason="schedule_timer")

    async def _defer(self, kind: str, minutes: int) -> None:
        """Count the promise from the message time; push any active timer past it."""
        obligation = await self._open_obligation(kind, "defer")
        said_at = self._event.occurred_at
        until = later(said_at, timedelta(minutes=minutes))
        deferred = obligation.defer(until=until, now=said_at, policy=self._policy)
        active = await self._active_timer(deferred)
        if active is None:
            await self._save_obligation(deferred)
            return
        due_at = follow_up_due_at(requested=active.due_at, deferred_until=until, grace=self._grace)
        await self._replace_timer(deferred, due_at, reason="defer_obligation")

    async def _close(self, kind: str, status: ObligationStatus) -> None:
        obligation = await self._open_obligation(kind, "close")
        closed = obligation.close(status)
        await self._mark_pending_timers(obligation, TimerStatus.CANCELLED)
        await self._save_obligation(closed)

    async def _open_obligation(self, kind: str, operation: str) -> Obligation:
        obligation = await self._uow.obligations.get_open(self._load.load_id, kind)
        if obligation is None:
            raise InvariantViolation(f"no open {kind} obligation to {operation}")
        return obligation

    async def _active_timer(self, obligation: Obligation) -> Timer | None:
        if obligation.active_timer_id is None:
            return None
        return await self._uow.timers.get(obligation.active_timer_id)

    async def _replace_timer(
        self, obligation: Obligation, due_at: datetime, *, reason: str
    ) -> None:
        """Supersede the obligation's pending timers and make a new one active.

        A fired timer is left fired: moving `active_timer_id` is enough to make
        its queued `timer_due` event stale for the prefilter.
        """
        await self._mark_pending_timers(obligation, TimerStatus.SUPERSEDED)
        timer = Timer(
            timer_id=self._ids.new("timer"),
            load_id=obligation.load_id,
            obligation_id=obligation.obligation_id,
            due_at=due_at,
            created_at=self._now,
            reason=reason,
        )
        await self._save_timer(timer)
        await self._save_obligation(obligation.attach_timer(timer.timer_id))

    async def _mark_pending_timers(self, obligation: Obligation, status: TimerStatus) -> None:
        for timer in await self._uow.timers.pending_for_obligation(obligation.obligation_id):
            await self._save_timer(timer.mark(status))

    async def _save_obligation(self, obligation: Obligation) -> None:
        await self._uow.obligations.save(obligation)
        self._obligations[obligation.obligation_id] = obligation

    async def _save_timer(self, timer: Timer) -> None:
        await self._uow.timers.save(timer)
        self._timers[timer.timer_id] = timer
