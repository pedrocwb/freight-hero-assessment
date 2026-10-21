"""What a person did with a load on the platform, step by step, so it can become a scenario.

Every input that reaches a load through the API (a message, a file, a ping) and every
clock move that made the agent run on it ("skip to the next timer") is one recorded step:
what was injected, when (load time), and what the step changed, measured the way the
scenario player measures a step (`evalkit.runner.ScenarioPlayer.observe`): the runs and
outbox items it created, the model calls it made (an agent loop counts once), and the
latest obligation of each kind and the pending timers at its end.

The web UI drafts a step's expectations from this record, and `api.scenario_files`
turns the record and the chosen expectations into a scenario file. The recording lives
in the simulation's session: a reset or a scenario drops it.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from freight_agent.application.contracts import LoadSnapshot
from freight_agent.domain.load import Channel, DelayRisk
from freight_agent.domain.obligations import ObligationStatus
from freight_agent.domain.runs import RunStatus
from freight_agent.domain.timers import TimerStatus

StepKind = Literal["message", "ping", "attachment", "wait"]


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RecordedAttachment(_Body):
    filename: str
    content_type: str


class RecordedMessage(_Body):
    channel: Channel
    sender: str | None
    body: str
    attachments: tuple[RecordedAttachment, ...] = ()


class RecordedPing(_Body):
    delay_risk: DelayRisk
    lat: float
    lon: float


class ObligationState(_Body):
    status: ObligationStatus
    attempts: int


class TimerState(_Body):
    pending: int
    due_at: str | None  # local HH:MM of the earliest pending timer


class RecordedStep(_Body):
    """One step of a load's recording (`GET /api/loads/{load_id}/recording`)."""

    index: int
    at: datetime
    kind: StepKind
    message: RecordedMessage | None = None
    ping: RecordedPing | None = None
    latency_seconds: float | None = None  # None for a clock move
    run_ids: tuple[str, ...] = ()
    run_statuses: tuple[RunStatus, ...] = ()
    outbox_ids: tuple[str, ...] = ()
    # None when the step's model calls cannot be told apart from another load's.
    llm_calls: int | None = 0
    obligations: dict[str, ObligationState] = {}
    timers: TimerState = TimerState(pending=0, due_at=None)


@dataclass
class Recording:
    """A load's recorded steps, and the blob reference of each file they carried (kept
    out of the API's view, as a document's is)."""

    steps: list[RecordedStep] = field(default_factory=list[RecordedStep])
    blob_refs: dict[tuple[int, int], str] = field(default_factory=dict[tuple[int, int], str])


def changes(
    before: LoadSnapshot | None, after: LoadSnapshot, timezone: ZoneInfo
) -> dict[str, object]:
    """What a step changed on one load, as `RecordedStep` fields: the runs and outbox
    items it created, in the snapshot's order, and the ledger at its end."""
    old_runs = {r.run_id for r in before.runs} if before else set[str]()
    old_items = {o.outbox_id for o in before.outbox} if before else set[str]()
    runs = [r for r in after.runs if r.run_id not in old_runs]
    return {
        "run_ids": tuple(r.run_id for r in runs),
        "run_statuses": tuple(r.status for r in runs),
        "outbox_ids": tuple(o.outbox_id for o in after.outbox if o.outbox_id not in old_items),
        **ledger(after, timezone),
    }


def ledger(snapshot: LoadSnapshot, timezone: ZoneInfo) -> dict[str, object]:
    """The latest obligation of each kind and the pending timers, as the graders read them
    (`evalkit.graders`)."""
    latest: dict[str, ObligationState] = {}
    for obligation in sorted(snapshot.obligations, key=lambda o: (o.opened_at, o.obligation_id)):
        latest[obligation.kind] = ObligationState(
            status=obligation.status, attempts=obligation.attempts
        )
    pending = sorted(
        (t for t in snapshot.timers if t.status is TimerStatus.PENDING),
        key=lambda t: (t.due_at, t.timer_id),
    )
    due = pending[0].due_at.astimezone(timezone).strftime("%H:%M") if pending else None
    return {"obligations": latest, "timers": TimerState(pending=len(pending), due_at=due)}


def merged(step: RecordedStep, extra: RecordedStep) -> RecordedStep:
    """`step` with what a later tick added to it (the runs of a file read on that tick)."""
    calls = (
        None
        if step.llm_calls is None or extra.llm_calls is None
        else (step.llm_calls + extra.llm_calls)
    )
    return step.model_copy(
        update={
            "run_ids": step.run_ids + extra.run_ids,
            "run_statuses": step.run_statuses + extra.run_statuses,
            "outbox_ids": step.outbox_ids + extra.outbox_ids,
            "llm_calls": calls,
            "obligations": extra.obligations,
            "timers": extra.timers,
        }
    )


def calls_for(load_ids: Sequence[str], load_id: str, delta: int) -> int | None:
    """The model calls of a step on `load_id`: all of them when no other load ran, else
    unknown (the counter is the session's, not the load's)."""
    return delta if set(load_ids) <= {load_id} else None
