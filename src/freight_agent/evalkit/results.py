"""What the runner observes per step, and what graders return.

Shared by the runner (which produces observations) and the graders (which
judge them), so both can be built and tested independently.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from freight_agent.application.contracts import LoadSnapshot
from freight_agent.domain.outbox import OutboxItem
from freight_agent.domain.runs import RunRecord, Usage


class _Result(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StepObservation(_Result):
    step_index: int
    at: datetime  # the step's scheduled time, timezone-aware
    outbox: tuple[OutboxItem, ...]  # items created during the step, in creation order
    runs: tuple[RunRecord, ...]  # runs that started during the step, in order
    llm_calls: int  # model calls made during the step (all roles)
    snapshot: LoadSnapshot  # state at the end of the step


class Failure(_Result):
    step_index: int | None  # None for scenario-wide checks (invariants)
    check: str  # e.g. "expect.actions[0]", "forbid[1]", "invariant.recipients"
    message: str


class ScenarioResult(_Result):
    scenario_id: str
    trial: int = 1
    passed: bool
    failures: tuple[Failure, ...] = ()
    observations: tuple[StepObservation, ...] = ()
    usage: tuple[Usage, ...] = ()
    error: str | None = None  # the scenario could not run (bad fixture, missing cassette)

    @property
    def cost_usd(self) -> float:
        return sum(u.cost_usd for u in self.usage)
