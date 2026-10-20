"""`agent eval draft --broker-file`: draft eval scenarios from a broker's SOP sections.

One `compile` call per intent: the model gets the intent, the sections that apply to it
(and the rules that apply to every event), and the load fixture's contacts and stops, and
answers a `ScenarioDraft`, a small structured scenario (steps with a message or a ping,
the actions to expect and to forbid, by contact role). Code turns it into a `Scenario`
(contact roles become the load's contact ids, the stage goes into `load_overrides`, the
broker into `broker_file`), checks that it parses, and writes it to
`evals/scenarios/<broker_id>/_draft_<intent>.yaml`, tagged `draft` and `capability`: a
draft never gates a run until a person reviews it, renames it and adds a script for
scripted mode. An existing file is never overwritten.

By default the intents drafted are those `agent eval lint` reports: the broker's intents
no scenario covers.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from freight_agent.domain.broker import IntentDef
from freight_agent.domain.load import Channel, DelayRisk, Stage
from freight_agent.domain.outbox import OutboxKind
from freight_agent.domain.runs import Usage
from freight_agent.domain.sop import ResolvedSection
from freight_agent.evalkit.brokers import BrokerExport, broker_index
from freight_agent.evalkit.spec import Scenario, load_scenario
from freight_agent.ports.knowledge import SopIndex
from freight_agent.ports.llm import LanguageModel, LlmError, LlmRequest, ModelRole

DRAFT_TAG = "draft"
DRAFT_PREFIX = "_draft_"
DEFAULT_LOAD = "fixtures/load_481207.json"
_PICKUP_STAGES = (Stage.DISPATCHED, Stage.TO_PICKUP, Stage.AT_PICKUP)

ContactRole = Literal["driver", "dispatcher", "broker", "unknown"]


class _Draft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DraftMessage(_Draft):
    channel: Channel
    sender: ContactRole = Field(description="Who writes, by role; unknown for a stranger")
    body: str = Field(min_length=1, description="The message text, as the sender writes it")


class DraftAction(_Draft):
    kind: OutboxKind
    to: ContactRole | None = Field(default=None, description="The recipient, by role")
    contains: tuple[str, ...] = Field(
        default=(), description="Exact facts the text must hold (a PO, a time), never wording"
    )
    count: int | None = Field(default=None, ge=0, description="Exact number; default at least 1")


class DraftStep(_Draft):
    at: str = Field(pattern=r"^\d{2}:\d{2}$", description="Local time HH:MM, steps in order")
    note: str = Field(description="What happens in this step, in one sentence")
    message: DraftMessage | None = None
    delay_risk: DelayRisk | None = Field(
        default=None, description="A location ping with this delay risk, instead of a message"
    )
    expect_actions: tuple[DraftAction, ...] = ()
    expect_no_actions: bool = Field(default=False, description="Nothing at all may be sent")
    forbid: tuple[DraftAction, ...] = ()


class ScenarioDraft(_Draft):
    """The `compile` model's answer: one scenario for one intent."""

    title: str = Field(min_length=1)
    description: str
    stage: Stage = Field(description="The load's stage when the scenario starts")
    steps: tuple[DraftStep, ...] = Field(min_length=1)


DRAFT_SYSTEM_PROMPT = """\
You write test scenarios for a freight load agent. The agent follows a broker's standard
operating procedures (SOP sections) to answer drivers, dispatchers and brokers by SMS,
email and Slack, and creates tasks for the Hero team (people who make calls).

You get one intent of the broker's catalog (what an event means), the SOP sections that
apply to it, the rules for every event, and the load: its contacts by role and channel,
its stops and appointments. Write one scenario that tests what the sections say the
agent must do for that intent, step by step:

- Each step happens at a local time (HH:MM, in order) and brings one message from a
  contact (by role) or one location ping with a delay risk, or nothing (time passes, so
  a follow-up timer can fire).
- Expect facts, never wording: the kind of each action, who it goes to, a fact the text
  must contain (a PO number, a time from the load), how many. Forbid what the sections
  forbid (an SMS when nothing must be sent, an email to the wrong contact).
- Use only the contacts, stops, times and numbers of the load. Write messages the way a
  driver or a dispatcher writes them: short, informal.
- Keep it small: two to four steps.
"""


def render_draft_prompt(
    export: BrokerExport,
    intent: IntentDef,
    sections: Sequence[ResolvedSection],
    load: dict[str, Any],
) -> str:
    procedure = [s for s in sections if intent.intent_id in s.intents]
    rules = [s for s in sections if not s.intents]
    examples = "".join(f"\n- {e}" for e in intent.examples)
    blocks = [
        f"## Broker\n\n{export.broker.name} ({export.broker_id})",
        f"## Intent\n\n{intent.intent_id}: {intent.title}. {intent.description}"
        + (f"\n\nExamples:{examples}" if examples else ""),
        "## Sections for this intent\n\n" + (_sections(procedure) or "(none)"),
        "## Rules for every event\n\n" + (_sections(rules) or "(none)"),
        "## The load\n\n" + json.dumps(_load_summary(load), indent=2, ensure_ascii=False),
    ]
    return "\n\n".join(blocks) + "\n\nWrite the scenario."


def _sections(sections: Sequence[ResolvedSection]) -> str:
    return "\n\n".join(f"### {s.section_id}: {s.title}\n\n{s.body.strip()}" for s in sections)


def _load_summary(load: dict[str, Any]) -> dict[str, Any]:
    keys = ("load_id", "shipper", "carrier", "commodity", "contacts", "stops", "instructions")
    return {k: load[k] for k in keys if k in load}


# --- from a draft to a scenario ---------------------------------------------------------


class DraftError(Exception):
    """The model's draft cannot become a valid scenario."""


def to_scenario(
    draft: ScenarioDraft,
    *,
    export: BrokerExport,
    intent: str,
    load: dict[str, Any],
    load_path: str,
    broker_file: str,
    section_ids: Sequence[str],
) -> Scenario:
    roles = _contacts_by_role(load)
    first = draft.steps[0].at
    data: dict[str, Any] = {
        "id": f"draft_{intent}",
        "title": draft.title,
        "suite": export.broker_id,
        "tags": [DRAFT_TAG, "capability", f"intent:{intent}", *(f"sop:{s}" for s in section_ids)],
        "description": draft.description,
        "load": load_path,
        "broker_file": broker_file,
        "load_overrides": {"stage": draft.stage.value},
        "date": _scenario_date(load, draft.stage).isoformat(),
        "start": _minutes_before(first, 5),
        "steps": [_step(s, roles, intent) for s in draft.steps],
    }
    try:
        return Scenario.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
        )
        raise DraftError(f"the draft is not a valid scenario: {problems}") from exc


def _step(step: DraftStep, roles: dict[str, str], intent: str) -> dict[str, Any]:
    data: dict[str, Any] = {"at": step.at, "note": step.note}
    if step.message is not None:
        data["message"] = {
            "channel": step.message.channel.value,
            "sender": roles.get(step.message.sender),
            "body": step.message.body,
        }
    elif step.delay_risk is not None:
        data["ping"] = {"delay_risk": step.delay_risk.value}
    expect: dict[str, Any] = {}
    if step.expect_actions:
        expect["actions"] = [_matcher(a, roles) for a in step.expect_actions]
    if step.expect_no_actions:
        expect["no_actions"] = True
    if step.message is not None or step.delay_risk is not None:
        expect["intent"] = intent
    if expect:
        data["expect"] = expect
    if step.forbid:
        data["forbid"] = [_matcher(a, roles) for a in step.forbid]
    return data


def _matcher(action: DraftAction, roles: dict[str, str]) -> dict[str, Any]:
    matcher: dict[str, Any] = {"kind": action.kind.value}
    if action.to is not None and action.to in roles:
        matcher["to"] = roles[action.to]
    if action.contains:
        matcher["contains"] = list(action.contains)
    if action.count is not None:
        matcher["count"] = action.count
    return matcher


def _contacts_by_role(load: dict[str, Any]) -> dict[str, str]:
    """The first contact id of each role in the load fixture."""
    found: dict[str, str] = {}
    for contact in cast(list[dict[str, Any]], load.get("contacts") or []):
        role, contact_id = contact.get("role"), contact.get("id")
        if isinstance(role, str) and isinstance(contact_id, str):
            found.setdefault(role, contact_id)
    return found


def _scenario_date(load: dict[str, Any], stage: Stage) -> date:
    """The appointment day of the stop the stage is about (pickup or delivery)."""
    wanted = "pickup" if stage in _PICKUP_STAGES else "delivery"
    for stop in cast(list[dict[str, Any]], load.get("stops") or []):
        appointment = cast(dict[str, Any], stop.get("appointment") or {})
        when = appointment.get("start") or appointment.get("time")
        if stop.get("type") == wanted and isinstance(when, str):
            return datetime.fromisoformat(when).date()
    raise DraftError(f"the load fixture has no {wanted} stop with an appointment")


def _minutes_before(hh_mm: str, minutes: int) -> str:
    at = datetime.combine(date(2000, 1, 2), time.fromisoformat(hh_mm)) - timedelta(minutes=minutes)
    return "00:00" if at.day == 1 else at.strftime("%H:%M")


def scenario_yaml(scenario: Scenario, *, model: str) -> str:
    header = (
        f"# Draft from the compile model ({model}) for intent {scenario.id.removeprefix('draft_')}"
        f" of broker {scenario.suite}.\n"
        "# Review every step and expectation against the SOP, add a `script` for scripted\n"
        "# mode, then rename the file without the _draft_ prefix and drop the draft tag.\n"
    )
    data = scenario.model_dump(mode="json", exclude_defaults=True)
    return header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


# --- drafting ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Drafted:
    intent: str
    status: Literal["written", "exists", "failed", "skipped"]
    path: Path | None = None
    message: str = ""


async def draft_scenarios(
    model: LanguageModel,
    export: BrokerExport,
    *,
    intents: Sequence[str],
    knowledge: SopIndex,
    evals_dir: Path,
    broker_file: str,
    out_dir: Path | None = None,
    load_path: str = DEFAULT_LOAD,
    cost_cap_usd: float | None = None,
    timeout_s: float = 120.0,
) -> tuple[list[Drafted], list[Usage]]:
    """One draft per intent, in order. Returns what happened to each and the model usage.

    The cost cap is checked after each call; the intents left are `skipped`."""
    out = (out_dir or evals_dir / "scenarios") / export.broker_id
    load = cast(dict[str, Any], json.loads((evals_dir / load_path).read_text(encoding="utf-8")))
    sections = await broker_index(knowledge, export).visible(
        export.broker_id, tuple(cast(list[str], load.get("load_types") or []))
    )
    results: list[Drafted] = []
    usage: list[Usage] = []
    for intent_id in intents:
        path = out / f"{DRAFT_PREFIX}{intent_id}.yaml"
        intent = export.broker.intent(intent_id)
        if intent is None:
            results.append(Drafted(intent_id, "failed", message="not an intent of the broker"))
            continue
        if path.exists():
            results.append(Drafted(intent_id, "exists", path, "kept: drafts are never overwritten"))
            continue
        if cost_cap_usd is not None and sum(u.cost_usd for u in usage) > cost_cap_usd:
            results.append(Drafted(intent_id, "skipped", message="the cost cap was reached"))
            continue
        request = LlmRequest(
            role=ModelRole.COMPILE,
            system=DRAFT_SYSTEM_PROMPT,
            user=render_draft_prompt(export, intent, sections, load),
            max_tool_rounds=0,
            timeout_s=timeout_s,
        )
        try:
            response = await model.generate(request, ScenarioDraft)
        except LlmError as exc:
            results.append(Drafted(intent_id, "failed", message=f"the compile model failed: {exc}"))
            continue
        usage.append(response.usage)
        applying = [s.section_id for s in sections if not s.intents or intent_id in s.intents]
        try:
            scenario = to_scenario(
                response.parsed,
                export=export,
                intent=intent_id,
                load=load,
                load_path=load_path,
                broker_file=broker_file,
                section_ids=applying,
            )
        except DraftError as exc:
            results.append(Drafted(intent_id, "failed", message=str(exc)))
            continue
        out.mkdir(parents=True, exist_ok=True)
        path.write_text(scenario_yaml(scenario, model=response.usage.model), encoding="utf-8")
        load_scenario(path)  # what was written reads back as the same scenario
        results.append(Drafted(intent_id, "written", path))
    return results, usage
