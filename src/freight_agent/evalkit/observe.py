"""Hooks around each trial and each step of an eval run, for an experiment tracker.

The suite runner opens a trial span around every trial and hands it the graded result;
the scenario runner opens a step span around every step and hands it what the step
changed. Work done inside a span (the runtime's model calls) happens in its context, so a
tracker built on context variables, like LangSmith's, nests those calls under the span.

`NullObserver` is the default: an eval run that tracks nothing behaves exactly as before.
A tracker must fail open: a span that cannot be recorded never changes a trial or its
verdict.
"""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from typing import Protocol

from freight_agent.evalkit.results import ScenarioResult, StepObservation
from freight_agent.evalkit.spec import Scenario, Step


class TrialSpan(Protocol):
    def graded(self, result: ScenarioResult) -> None:
        """The trial's verdict, once the graders have judged it."""
        ...


class StepSpan(Protocol):
    def observed(self, observation: StepObservation) -> None:
        """What the step changed: the outbox items, runs and state at its end."""
        ...


class Observer(Protocol):
    def trial(self, scenario: Scenario, trial: int) -> AbstractAsyncContextManager[TrialSpan]: ...

    def step(
        self, scenario: Scenario, index: int, step: Step, at: datetime
    ) -> AbstractAsyncContextManager[StepSpan]: ...


class _NullSpan:
    def graded(self, result: ScenarioResult) -> None:
        pass

    def observed(self, observation: StepObservation) -> None:
        pass


class NullObserver:
    """Observes nothing."""

    @asynccontextmanager
    async def trial(self, scenario: Scenario, trial: int) -> AsyncIterator[TrialSpan]:
        yield _NullSpan()

    @asynccontextmanager
    async def step(
        self, scenario: Scenario, index: int, step: Step, at: datetime
    ) -> AsyncIterator[StepSpan]:
        yield _NullSpan()
