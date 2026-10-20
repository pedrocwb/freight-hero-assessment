"""Evals on a broker's own configuration: the broker export, its SOP index, and the lint
that finds the broker's intents no scenario covers.

**The export** (`evals/brokers/<broker_id>.json`, documented in `evals/brokers/README.md`)
is one JSON object: the fields of `Broker.model_dump(mode="json")` plus `sections`, the
broker's published SOP sections (its own `client` and `load_type` layers; the standard
layer comes from the `sops/` tree every eval reads). `agent eval run --broker-file` and
a scenario's `broker_file` play a trial on it: the trial's store holds the broker, the SOP
index holds its sections in place of whatever the `sops/` tree has for that broker, and
the load's broker becomes the export's.

**The lint** (`agent eval lint --broker-file`) lists every intent of the broker's catalog
that no scenario covers. A scenario covers an intent when a step expects it
(`expect.intent`), scripts it (a gate decision or an agent turn with that intent), or
leads to it by construction: a ping into high risk is `delay_risk_high`, a ping out of
it `delay_risk_low`, a message with only an attachment `document`, a time step that
fires the follow-up timer of a kind the scenario scheduled that kind's follow-up intent.
A tag `intent:<id>` declares it when nothing else shows it. An intent of the standard
catalog is covered by a scenario of any broker (the code path is the same); an intent the
broker added is covered only by scenarios that run on this broker.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from freight_agent.adapters.knowledge.memory import MemorySopIndex, content_hash
from freight_agent.domain.actions import ScheduleTimer, SendEmail, SendSms
from freight_agent.domain.broker import Broker, standard_broker
from freight_agent.domain.load import DelayRisk
from freight_agent.domain.sop import SopLayer, SopSection
from freight_agent.evalkit.spec import Scenario, Step
from freight_agent.ports.knowledge import SopIndex

SHARED_SUITES = ("core", "agent")  # suites written for any broker: `--broker-file` runs them
INTENT_TAG = "intent:"


class BrokerFileError(Exception):
    """A broker export cannot be read. The message names the file and the problem."""


class BrokerExport(BaseModel):
    """A broker's catalog and its published sections, as one eval input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    broker: Broker
    sections: tuple[SopSection, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _flat(cls, data: Any) -> Any:
        """The file is flat (the broker's fields next to `sections`); code may also pass
        `{"broker": ..., "sections": ...}`."""
        if isinstance(data, dict) and "broker" not in data:
            fields = dict(cast(dict[str, Any], data))
            sections = fields.pop("sections", ())
            return {"broker": fields, "sections": sections}
        return data

    @model_validator(mode="after")
    def _own_sections(self) -> "BrokerExport":
        broker_id = self.broker.broker_id
        foreign = [
            s.section_id
            for s in self.sections
            if s.layer is SopLayer.STANDARD or s.client_id != broker_id
        ]
        if foreign:
            raise ValueError(
                f"sections {', '.join(foreign)} are not {broker_id}'s own (layer client or "
                f"load_type, client_id {broker_id}); the standard layer comes from sops/"
            )
        return self

    @property
    def broker_id(self) -> str:
        return self.broker.broker_id

    def label(self) -> str:
        """`acme@v2`: the broker and its config version, for reports."""
        return f"{self.broker_id}@v{self.broker.config_version}"

    def to_json(self) -> str:
        data = self.broker.model_dump(mode="json")
        data["sections"] = [s.model_dump(mode="json", exclude_defaults=True) for s in self.sections]
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def read_broker_export(path: Path) -> BrokerExport:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BrokerFileError(f"cannot read the broker file {path}: {exc}") from exc
    try:
        return BrokerExport.model_validate_json(text)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'file'}: {e['msg']}" for e in exc.errors()[:5]
        )
        raise BrokerFileError(f"{path} is not a broker export: {problems}") from exc


def load_metadata(export: BrokerExport) -> dict[str, Any]:
    """The `broker` key of the load metadata that puts a load on this broker."""
    return {"broker": {"id": export.broker_id, "name": export.broker.name}}


# --- the SOP index of a broker trial --------------------------------------------------


class BrokerSopIndex(MemorySopIndex):
    """The base index (the `sops/` tree) with the export's sections as the broker's own
    layers: whatever the base holds for that broker is replaced, every other broker and
    the standard layer read through unchanged."""

    def __init__(self, base: MemorySopIndex, export: BrokerExport) -> None:
        super().__init__((), version="")
        self._base = base
        self._broker_id = export.broker_id
        self._own = export.sections

    @property
    def content_version(self) -> str:
        return f"{self._base.content_version}+{self._broker_id}:{content_hash(self._own)}"

    async def sections_for(self, broker_id: str) -> Sequence[SopSection]:
        base = await self._base.sections_for(broker_id)
        if broker_id != self._broker_id:
            return base
        return (*(s for s in base if s.client_id != broker_id), *self._own)


def broker_index(base: SopIndex, export: BrokerExport) -> SopIndex:
    if not isinstance(base, MemorySopIndex):
        raise BrokerFileError(
            f"a broker export runs on the in-memory SOP index (sops/), not {type(base).__name__}"
        )
    return BrokerSopIndex(base, export)


# --- coverage lint ----------------------------------------------------------------------


@dataclass(frozen=True)
class LintIssue:
    intent: str
    message: str

    def __str__(self) -> str:
        return f"error: intent {self.intent}: {self.message}"


def runs_on(scenario: Scenario, broker_id: str, export_of: dict[str, str | None]) -> bool:
    """Whether the scenario plays its load on this broker. `export_of` maps a scenario's
    `broker_file` to the broker id it holds (None: unreadable)."""
    if scenario.broker_file is not None:
        return export_of.get(scenario.broker_file) == broker_id
    if scenario.suite == broker_id:
        return True
    override = scenario.load_overrides.get("broker")
    if isinstance(override, dict):
        return cast(dict[str, Any], override).get("id") == broker_id
    return scenario.suite in SHARED_SUITES


def covered_intents(scenario: Scenario, broker: Broker) -> set[str]:
    found = {t.removeprefix(INTENT_TAG) for t in scenario.tags if t.startswith(INTENT_TAG)}
    high = False
    scheduled: set[str] = set()
    for step in scenario.steps:
        found.update(_stated(step))
        if step.ping is not None:
            if step.ping.delay_risk is DelayRisk.HIGH:
                found.add("delay_risk_high")
                high = True
            elif high:
                found.add("delay_risk_low")
                high = False
        if step.message is not None and step.message.attachments and not step.message.body.strip():
            found.add("document")
        if step.message is None and step.ping is None and step.script.agent:
            found.update(i for k in scheduled if (i := broker.follow_up_intent(k)) is not None)
        scheduled.update(_kinds_scheduled(step))
    return found


def _stated(step: Step) -> Iterable[str]:
    if step.expect.intent is not None:
        yield step.expect.intent
    yield from (d.intent for d in step.script.gate)
    yield from (t.intent for t in step.script.agent if t.intent is not None)


def _kinds_scheduled(step: Step) -> set[str]:
    kinds: set[str] = set()
    for turn in step.script.agent:
        for action in turn.actions:
            if isinstance(action, ScheduleTimer):
                kinds.add(action.kind)
            elif isinstance(action, SendSms | SendEmail) and action.requests:
                kinds.add(action.requests)
        for call in turn.tool_calls:
            kind = call.args.get("kind") or call.args.get("requests")
            if call.name in ("schedule_timer", "send_sms", "send_email") and isinstance(kind, str):
                kinds.add(kind)
    return kinds


def lint_broker(
    broker: Broker, scenarios: Sequence[Scenario], export_of: dict[str, str | None]
) -> list[LintIssue]:
    """Every intent of the broker's catalog that no scenario covers, in catalog order."""
    standard = {i.intent_id for i in standard_broker().intents}
    anywhere: set[str] = set()
    on_broker: set[str] = set()
    for scenario in scenarios:
        covered = covered_intents(scenario, broker)
        anywhere |= covered
        if runs_on(scenario, broker.broker_id, export_of):
            on_broker |= covered
    issues: list[LintIssue] = []
    for intent in broker.intents:
        intent_id = intent.intent_id
        if intent_id in on_broker or (intent_id in standard and intent_id in anywhere):
            continue
        where = (
            "no scenario covers it"
            if intent_id in standard
            else f"no scenario that runs on {broker.broker_id} covers it"
        )
        issues.append(
            LintIssue(
                intent_id,
                f"{where} (expect.intent, a scripted gate or agent intent, or a tag "
                f"{INTENT_TAG}{intent_id}); `agent eval draft` can draft one",
            )
        )
    return issues


def exports_by_file(evals_dir: Path, scenarios: Sequence[Scenario]) -> dict[str, str | None]:
    """The broker id of each `broker_file` the scenarios name (None when unreadable)."""
    found: dict[str, str | None] = {}
    for scenario in scenarios:
        name = scenario.broker_file
        if name is None or name in found:
            continue
        try:
            found[name] = read_broker_export(evals_dir / name).broker_id
        except BrokerFileError:
            found[name] = None
    return found
