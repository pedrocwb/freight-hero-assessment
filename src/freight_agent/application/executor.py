"""The executor turns approved actions into ledger changes, timers and outbox items.

It runs inside the caller's unit of work and never commits: the run's state
changes and its side effects are stored in one transaction. Delivery happens
later, from the outbox. Semantics are listed in docs/decisions.md.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from freight_agent.application.context import RunContext
from freight_agent.domain.actions import Action
from freight_agent.domain.load import Load
from freight_agent.domain.obligations import DeferralPolicy, Obligation
from freight_agent.domain.outbox import OutboxItem
from freight_agent.domain.timers import Timer
from freight_agent.ports.clock import Clock, IdGenerator
from freight_agent.ports.repositories import UnitOfWork


@dataclass(frozen=True)
class ExecutionResult:
    load: Load  # after any stage update
    outbox: tuple[OutboxItem, ...] = ()  # items created by this run
    obligations: tuple[Obligation, ...] = ()  # obligations created or changed
    timers: tuple[Timer, ...] = ()  # timers created or changed (incl. superseded, cancelled)


class Executor:
    def __init__(self, clock: Clock, ids: IdGenerator, deferral: DeferralPolicy) -> None:
        self._clock = clock
        self._ids = ids
        self._deferral = deferral

    async def apply(
        self, uow: UnitOfWork, *, run_id: str, ctx: RunContext, actions: Sequence[Action]
    ) -> ExecutionResult:
        raise NotImplementedError  # card APP-1
