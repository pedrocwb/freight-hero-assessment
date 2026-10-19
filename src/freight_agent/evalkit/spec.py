"""Scenario specification: the contract between scenario authors and the eval runner.

Runner semantics (implemented in `evalkit.runner`):

1. The virtual clock starts at `date` + `start` in `timezone`.
2. For each step, the runner first advances the clock to the step's `at` time,
   stopping at every pending timer's due time on the way. At each stop it
   fires due timers, processes the queue and dispatches the outbox (timers have
   zero processing latency).
3. If the step injects a message or a ping, it is ingested with
   `occurred_at = at`. The clock then advances by `run_latency_seconds`
   (never past the next step's time) and the runtime ticks.
4. A step with no injection only advances time (that is how timer steps work).
5. Expectations are checked against the outbox items created during the step
   and against the ledger and timers at the end of the step.
6. In `scripted` mode, model calls consume `script` entries in order, per role.
   Only inbound messages call the gate model; risk changes, timers and
   attachments get their intent from code and consume no gate entry. A run
   whose gate acts consumes one plan. Runs skipped or escalated by the
   prefilter consume nothing. Unused or missing script entries fail the step.
   In `replay` and `live` modes `script` is ignored.
7. `load_overrides` is merged into the fixture's top-level keys before the load
   is built (a key in the override replaces the fixture's value), for example
   `{"broker": {"id": "keystone", "name": "Keystone Freight"}, "load_types": ["reefer"]}`.
8. Tags of the form `sop:<section_id>` declare which SOP sections a scenario
   covers; the SOP coverage lint reads them.
"""

import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from freight_agent.domain.actions import Plan, Purpose
from freight_agent.domain.intents import GateDecision
from freight_agent.domain.load import Channel, DelayRisk, Stage
from freight_agent.domain.obligations import ObligationKind, ObligationStatus
from freight_agent.domain.outbox import OutboxKind
from freight_agent.domain.runs import RunStatus

_TIME = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")


class _Spec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MessageStep(_Spec):
    channel: Channel
    sender: str | None = Field(description="Contact id; null means an unknown sender")
    body: str
    message_id: str | None = Field(default=None, description="Defaults to a derived id")


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
    obligations: dict[ObligationKind, ObligationExpectation | None] = Field(
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


class ScriptedLlm(_Spec):
    gate: tuple[GateDecision, ...] = ()
    decide: tuple[Plan, ...] = ()


class Step(_Spec):
    at: str
    note: str = ""
    message: MessageStep | None = None
    ping: PingStep | None = None
    expect: Expectation = Expectation()
    forbid: tuple[ActionMatcher, ...] = ()
    script: ScriptedLlm = ScriptedLlm()

    @model_validator(mode="after")
    def _check(self) -> "Step":
        if not _TIME.match(self.at):
            raise ValueError(f"step time must be HH:MM or HH:MM:SS, got {self.at!r}")
        if self.message is not None and self.ping is not None:
            raise ValueError("a step injects at most one message or ping")
        return self


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
