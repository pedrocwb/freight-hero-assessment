"""Run records: what one agent run saw, decided and cost."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from freight_agent.domain.actions import Plan
from freight_agent.domain.base import DomainModel
from freight_agent.domain.intents import GateDecision
from freight_agent.domain.validation import Violation


class RunStatus(StrEnum):
    SKIPPED = "skipped"  # prefilter: duplicate, stale timer, nothing to do (no LLM call)
    NOOP = "noop"  # gate or decide chose to do nothing
    ACTED = "acted"
    ESCALATED = "escalated"
    FAILED = "failed"


class Usage(DomainModel):
    role: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0


class TrajectoryStep(DomainModel):
    """One tool call of the agent loop, as the run's trajectory shows it.

    `accepted` is set for action tools (staged or refused by a rule) and None for read
    tools. `cost_usd` is the model round that made the call.
    """

    tool: str
    args: dict[str, Any]
    result: str
    accepted: bool | None
    cost_usd: float = 0.0


class RunRecord(DomainModel):
    run_id: str
    event_id: str
    load_id: str
    started_at: datetime
    finished_at: datetime | None = None
    status: RunStatus
    gate: GateDecision | None = None
    sop_section_ids: tuple[str, ...] = ()
    sop_version: str | None = None  # content hash of the SOP files the run read
    plan: Plan | None = None
    violations: tuple[Violation, ...] = ()
    usage: tuple[Usage, ...] = ()
    skip_reason: str | None = None
    error: str | None = None
    trace_url: str | None = None
    # Wave 3, all optional so records stored before them still validate.
    intent: str | None = None  # the agent's reading of the event, when it labelled it
    broker_id: str | None = None  # the broker config the run used
    broker_config_version: int | None = None  # its version (canary: pinned per run)
    trajectory: tuple[TrajectoryStep, ...] = ()  # the agent's tool calls, in order
    sections_searched: tuple[str, ...] = ()  # sections search_sop returned or the agent read
    rejections: int = 0  # action tool calls a rule refused

    @property
    def cost_usd(self) -> float:
        return sum(u.cost_usd for u in self.usage)
