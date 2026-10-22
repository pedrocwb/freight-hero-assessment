"""Scenario specification: the contract between scenario authors and the player.

Every scenario is played live, on the models of `config/models.yaml`: in a LangSmith
experiment (`evalkit.experiment`), the CLI player or LangGraph Studio.
Player semantics (implemented in `evalkit.runner`):

1. The virtual clock starts at `date` + `start` in `timezone`.
2. For each step, the runner first advances the clock to the step's `at` time,
   stopping at every pending timer's due time on the way. At each stop it
   fires due timers, processes the queue and dispatches the outbox (timers have
   zero processing latency).
3. If the step injects a message or a ping, it is ingested with
   `occurred_at = at`. The clock then advances by `run_latency_seconds`
   (never past the next step's time) and the runtime ticks. A message's
   `attachments` are fixture files: their bytes go into the trial's blob store and the
   message carries them as attachments, so the document pipeline reads them on that
   tick, before the queue runs (`docs/decisions.md`, "Documents").
4. A step with no injection only advances time (that is how timer steps work).
5. Expectations are checked against the outbox items created during the step
   and against the ledger and timers at the end of the step. The trajectory checks
   (`intent`, `tools_used`, `tools_refused`, `sections_read`, `min_rejections`,
   `max_rejections`) read the step's run records; `judge` asks the judge model, right
   after the step, to grade the texts the step created against a rubric
   (`evalkit.judge`). `llm_calls` counts the model calls of the step with an agent
   loop as one call, whatever its rounds (`evalkit.counting`): the gate's call, one per
   agent run, one per document read.
6. `load_overrides` is merged into the fixture's top-level keys before the load
   is built (a key in the override replaces the fixture's value), for example
   `{"broker": {"id": "keystone", "name": "Keystone Freight"}, "load_types": ["reefer"]}`.
7. Tags of the form `sop:<section_id>` declare which SOP sections a scenario
   covers; the SOP coverage lint reads them.
8. `memories` are facility notes an earlier load left, written into the trial's facility
   memory before the first step: each names a stop of the scenario's load (by sequence),
   for the load's client unless it names another `client`. `expect.memories` and
   `expect.no_memories` check the notes of the load's stops at the end of a step, after
   the tick's facility memory extraction (`application/memory.py`).
"""

import re
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from freight_agent.domain.actions import Purpose
from freight_agent.domain.load import Channel, DelayRisk, Stage
from freight_agent.domain.memory import FactKind
from freight_agent.domain.obligations import ObligationStatus
from freight_agent.domain.outbox import OutboxKind
from freight_agent.domain.runs import RunStatus
from freight_agent.evalkit.judge import JudgeSpec

_TIME = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")

StepKind = Literal["message", "ping", "attachment", "wait"]


class _Spec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AttachmentSpec(_Spec):
    """A file sent with a message: fixture bytes the trial stores as a blob (card W3-EVK)."""

    filename: str
    fixture: str = Field(description="The file, relative to the evals directory")
    content_type: str = "application/pdf"


class MessageStep(_Spec):
    channel: Channel
    sender: str | None = Field(description="Contact id; null means an unknown sender")
    body: str
    message_id: str | None = Field(default=None, description="Defaults to a derived id")
    attachments: tuple[AttachmentSpec, ...] = ()


class PingStep(_Spec):
    delay_risk: DelayRisk
    lat: float = 37.9577
    lon: float = -121.2908


class ActionMatcher(_Spec):
    """Matches outbox items. Text checks look at text, body, title and description."""

    kind: OutboxKind
    to: str | None = Field(default=None, description="Contact id recipient (SMS or email)")
    contains: tuple[str, ...] = Field(default=(), description="Case-insensitive substrings")
    regex: str | None = None
    urgent: bool | None = None
    purpose: Purpose | None = Field(
        default=None, description="Why an SMS or email was sent (payload purpose)"
    )
    stage: Stage | None = Field(default=None, description="The new stage of a stage_update")
    count: int | None = Field(
        default=None, description="Exact number of matches; default at least 1"
    )

    @field_validator("regex")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid regex {value!r}: {exc}") from exc
        return value


class MemorySeed(_Spec):
    """A facility note an earlier load left, in the trial's memory before the first step."""

    stop: int = Field(description="The stop sequence of the scenario's load the note is about")
    text: str = Field(min_length=1)
    kind: FactKind = FactKind.OTHER
    learned_on: date = Field(description="The day it was told")
    load_id: str = Field(default="earlier-load", description="The load it was told on")
    client: str | None = Field(default=None, description="Default: the load's broker")


class MemoryMatcher(_Spec):
    """Matches the facility notes of one stop of the load, at the end of a step."""

    stop: int = Field(description="The stop sequence whose facility's notes are checked")
    contains: tuple[str, ...] = Field(default=(), description="Case-insensitive substrings")
    regex: str | None = None
    kind: FactKind | None = None
    count: int | None = Field(
        default=None, description="Exact number of matches; default at least 1"
    )

    @field_validator("regex")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid regex {value!r}: {exc}") from exc
        return value


class ObligationExpectation(_Spec):
    status: ObligationStatus | None = None
    attempts: int | None = None
    deferred: bool | None = None


class TimerExpectation(_Spec):
    pending: int | None = None
    due_at: str | None = Field(
        default=None, description="Local HH:MM of the earliest pending timer"
    )


class Expectation(_Spec):
    actions: tuple[ActionMatcher, ...] = ()
    no_actions: bool = Field(default=False, description="No outbox item at all during the step")
    obligations: dict[str, ObligationExpectation | None] = Field(
        default_factory=dict,
        description="Latest obligation of each kind; null means none exists",
    )
    timers: TimerExpectation | None = None
    run_status: tuple[RunStatus, ...] = Field(default=(), description="Statuses of the step's runs")
    llm_calls: int | None = Field(
        default=None, description="Exact number of model calls in the step"
    )
    runs: int | None = Field(
        default=None, description="Exact number of runs in the step; 0 means no run at all"
    )
    # Wave 3: the agent loop's trajectory and the judge.
    intent: str | None = Field(
        default=None,
        description="A run of the step has this intent (the agent's label, else the gate's)",
    )
    tools_used: tuple[str, ...] = Field(
        default=(), description="Tools the agent called during the step (at least these)"
    )
    tools_refused: tuple[str, ...] = Field(
        default=(), description="Action tools a rule refused at least once during the step"
    )
    sections_read: tuple[str, ...] = Field(
        default=(),
        description="SOP sections the agent searched up or read during the step (at least these)",
    )
    min_rejections: int | None = Field(
        default=None, ge=0, description="At least this many refused tool calls in the step"
    )
    max_rejections: int | None = Field(
        default=None, ge=0, description="At most this many refused tool calls in the step"
    )
    judge: JudgeSpec | None = Field(
        default=None, description="The judge grades the step's texts against this rubric"
    )
    # Facility memory: the notes of the load's stops at the end of the step.
    memories: tuple[MemoryMatcher, ...] = Field(
        default=(), description="Facility notes that must exist at the end of the step"
    )
    no_memories: tuple[MemoryMatcher, ...] = Field(
        default=(), description="Facility notes that must not exist at the end of the step"
    )

    @model_validator(mode="after")
    def _rejection_bounds(self) -> "Expectation":
        low, high = self.min_rejections, self.max_rejections
        if low is not None and high is not None and low > high:
            raise ValueError(f"min_rejections {low} is above max_rejections {high}")
        return self


class Step(_Spec):
    at: str
    note: str = ""
    message: MessageStep | None = None
    ping: PingStep | None = None
    expect: Expectation = Expectation()
    forbid: tuple[ActionMatcher, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> "Step":
        if not _TIME.match(self.at):
            raise ValueError(f"step time must be HH:MM or HH:MM:SS, got {self.at!r}")
        if self.message is not None and self.ping is not None:
            raise ValueError("a step injects at most one message or ping")
        return self

    @property
    def kind(self) -> StepKind:
        """What the step injects; `wait` injects nothing, the clock moves so due timers
        fire."""
        if self.message is not None:
            return "attachment" if self.message.attachments else "message"
        return "ping" if self.ping is not None else "wait"


class Scenario(_Spec):
    id: str
    title: str
    suite: str
    tags: tuple[str, ...] = ("regression",)
    description: str = ""
    load: str = Field(description="Load metadata fixture, relative to the evals directory")
    load_overrides: dict[str, Any] = Field(default_factory=dict)
    timezone: str = "America/Los_Angeles"
    date: date
    start: str
    run_latency_seconds: int = 60
    memories: tuple[MemorySeed, ...] = Field(
        default=(), description="Facility notes in the memory before the first step"
    )
    steps: tuple[Step, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> "Scenario":
        times = [self.start, *(s.at for s in self.steps)]
        if any(not _TIME.match(t) for t in times):
            raise ValueError("times must be HH:MM or HH:MM:SS")
        normalized = [t if len(t) == 8 else f"{t}:00" for t in times]
        if normalized != sorted(normalized):
            raise ValueError("start and step times must be in order")
        return self


class _StrictLoader(yaml.SafeLoader):
    """A repeated key is an error: plain YAML keeps the last one and silently drops
    the expectation written first."""


def _mapping_without_duplicates(loader: yaml.SafeLoader, node: yaml.MappingNode) -> Any:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)  # pyright: ignore[reportUnknownMemberType]
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_without_duplicates
)


def load_scenario(path: Path) -> Scenario:
    with path.open(encoding="utf-8") as handle:
        return Scenario.model_validate(yaml.load(handle, Loader=_StrictLoader))
