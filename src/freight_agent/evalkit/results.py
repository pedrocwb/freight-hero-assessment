"""What the player observes per step, and what graders return.

Shared by the player (which produces observations) and the graders (which judge them),
so both can be built and tested independently. An observation travels as JSON in an
experiment's outputs (`compact`), and the graders read it back from there.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from freight_agent.application.contracts import LoadSnapshot
from freight_agent.domain.memory import FacilityFact
from freight_agent.domain.outbox import OutboxItem
from freight_agent.domain.runs import RunRecord
from freight_agent.evalkit.judge import JudgeResult


class _Result(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StepObservation(_Result):
    step_index: int
    at: datetime  # the step's scheduled time, timezone-aware
    outbox: tuple[OutboxItem, ...]  # items created during the step, in creation order
    runs: tuple[RunRecord, ...]  # runs that started during the step, in order
    llm_calls: int  # model calls made during the step (all roles, an agent loop as one)
    snapshot: LoadSnapshot  # state at the end of the step
    judge: JudgeResult | None = None  # the judge's verdict, for a step with a rubric
    # The facility notes of the load's stops at the end of the step (its client's).
    memories: tuple[FacilityFact, ...] = ()

    def compact(self) -> dict[str, Any]:
        """The observation as JSON, without what the graders never read: the snapshot's
        event log, outbox and runs (the step's own outbox items and runs stay)."""
        data = self.model_dump(mode="json")
        snapshot = data["snapshot"]
        for key in ("events", "outbox", "runs"):
            snapshot[key] = []
        return data


class Failure(_Result):
    step_index: int | None  # None for scenario-wide checks (invariants)
    check: str  # e.g. "expect.actions[0]", "forbid[1]", "invariant.recipients"
    message: str

    def describe(self) -> str:
        where = f"step {self.step_index}: " if self.step_index is not None else ""
        return f"{where}{self.check}: {self.message}"
