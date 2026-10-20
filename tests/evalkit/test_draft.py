"""Scenario drafts from a broker's sections, on the `compile` role (a scripted model here)."""

import shutil
from pathlib import Path
from typing import Any

import pytest

from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.domain.runs import Usage
from freight_agent.evalkit.brokers import BrokerExport, read_broker_export
from freight_agent.evalkit.draft import (
    DRAFT_SYSTEM_PROMPT,
    DraftError,
    ScenarioDraft,
    draft_scenarios,
    render_draft_prompt,
    to_scenario,
)
from freight_agent.evalkit.spec import load_scenario
from freight_agent.ports.llm import LlmError, LlmRequest, LlmResponse, ModelRole, SchemaT

ROOT = Path(__file__).resolve().parents[2]
SOPS = MemorySopIndex.from_files(ROOT / "sops")
ACME = read_broker_export(ROOT / "evals" / "brokers" / "acme.json")
BROKER_FILE = "brokers/acme.json"

DETENTION: dict[str, Any] = {
    "title": "A wait over two hours at Valley DC is a detention request",
    "description": "The driver has waited 2 h 15 min at the receiver.",
    "stage": "at_delivery",
    "steps": [
        {
            "at": "11:45",
            "note": "The driver reports the wait",
            "message": {
                "channel": "sms",
                "sender": "driver",
                "body": "checked in at valley dc at 09:10, still waiting",
            },
            "expect_actions": [
                {"kind": "tms_note", "count": 1},
                {"kind": "hero_task", "contains": ["Request detention at"], "count": 1},
                {"kind": "sms", "to": "driver", "count": 1},
            ],
            "forbid": [{"kind": "email", "to": "dispatcher"}],
        },
        {"at": "12:30", "note": "Time passes", "expect_no_actions": True},
        {"at": "12:40", "note": "A ping", "delay_risk": "low"},
        {
            "at": "12:50",
            "note": "A stranger writes",
            "message": {"channel": "sms", "sender": "unknown", "body": "who is this"},
        },
    ],
}


@pytest.fixture
def evals(tmp_path: Path) -> Path:
    root = tmp_path / "evals"
    (root / "fixtures").mkdir(parents=True)
    (root / "brokers").mkdir()
    shutil.copy(ROOT / "evals" / "fixtures" / "load_481207.json", root / "fixtures")
    shutil.copy(ROOT / "evals" / "brokers" / "acme.json", root / "brokers")
    return root


def compile_model(*drafts: dict[str, Any] | Exception) -> ScriptedModel:
    model = ScriptedModel(model_name="compile/model")
    model.push(
        ModelRole.COMPILE,
        *(d if isinstance(d, Exception) else ScenarioDraft.model_validate(d) for d in drafts),
    )
    return model


async def draft(
    evals: Path, model: Any, intents: list[str], **kwargs: Any
) -> tuple[list[Any], list[Usage]]:
    return await draft_scenarios(
        model,
        ACME,
        intents=intents,
        knowledge=SOPS,
        evals_dir=evals,
        broker_file=BROKER_FILE,
        **kwargs,
    )


@pytest.mark.unit
async def test_a_draft_is_written_as_a_valid_scenario_tagged_draft(evals: Path) -> None:
    model = compile_model(DETENTION)

    results, usage = await draft(evals, model, ["detention_request"])

    (result,) = results
    assert result.status == "written"
    assert result.path == evals / "scenarios" / "acme" / "_draft_detention_request.yaml"
    text = result.path.read_text()
    assert text.startswith("# Draft from the compile model (compile/model) for intent")
    scenario = load_scenario(result.path)
    assert (scenario.id, scenario.suite, scenario.broker_file) == (
        "draft_detention_request",
        "acme",
        BROKER_FILE,
    )
    assert scenario.tags[:3] == ("draft", "capability", "intent:detention_request")
    assert "sop:detention" in scenario.tags and "sop:communication_rules" in scenario.tags
    assert scenario.load_overrides == {"stage": "at_delivery"}
    assert (scenario.date.isoformat(), scenario.start) == ("2026-10-14", "11:40")
    first, quiet, ping, stranger = scenario.steps
    assert first.message is not None and first.message.sender == "drv_20931"
    assert first.expect.intent == "detention_request"
    assert [(m.kind, m.to, m.count) for m in first.expect.actions] == [
        ("tms_note", None, 1),
        ("hero_task", None, 1),
        ("sms", "drv_20931", 1),
    ]
    assert [(m.kind, m.to) for m in first.forbid] == [("email", "dsp_4410")]
    assert quiet.expect.no_actions and quiet.expect.intent is None
    assert ping.ping is not None and ping.ping.delay_risk == "low"
    assert stranger.message is not None and stranger.message.sender is None
    assert [u.role for u in usage] == ["compile"]


@pytest.mark.unit
async def test_the_compile_call_carries_the_intent_its_sections_and_the_load(evals: Path) -> None:
    model = compile_model(DETENTION)

    await draft(evals, model, ["detention_request"])

    (call,) = model.calls
    assert (call.role, call.schema) == (ModelRole.COMPILE, "ScenarioDraft")
    assert call.request.system == DRAFT_SYSTEM_PROMPT and call.request.tools == ()
    user = call.request.user
    assert "detention_request: Detention request." in user
    assert "### detention: Detention: truck held at a facility" in user
    assert "## Rules for every event\n\n### communication_rules" in user
    assert '"load_id": "481207"' in user and '"instructions"' in user


@pytest.mark.unit
async def test_an_existing_draft_is_never_overwritten(evals: Path) -> None:
    path = evals / "scenarios" / "acme" / "_draft_detention_request.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("# a person's edits\n")
    model = compile_model()

    (result,), usage = await draft(evals, model, ["detention_request"])

    assert (result.status, result.path) == ("exists", path)
    assert path.read_text() == "# a person's edits\n"
    assert model.calls == [] and usage == []


@pytest.mark.unit
async def test_unknown_intents_model_failures_and_bad_drafts_are_reported(evals: Path) -> None:
    unordered = {**DETENTION, "steps": [DETENTION["steps"][1], DETENTION["steps"][0]]}
    model = compile_model(LlmError("timed out"), unordered)

    results, _ = await draft(evals, model, ["nope", "detention_request", "pod_follow_up_due"])

    assert [(r.intent, r.status) for r in results] == [
        ("nope", "failed"),
        ("detention_request", "failed"),
        ("pod_follow_up_due", "failed"),
    ]
    assert results[0].message == "not an intent of the broker"
    assert results[1].message == "the compile model failed: timed out"
    assert results[2].message.startswith("the draft is not a valid scenario:")
    assert not (evals / "scenarios" / "acme").exists()


class Pricey:
    """A compile model that costs a cent per call."""

    def __init__(self) -> None:
        self.model = compile_model(DETENTION, DETENTION)

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        response = await self.model.generate(request, schema)
        usage = Usage(role="compile", model="m", cost_usd=0.01)
        return LlmResponse(parsed=response.parsed, usage=usage)


@pytest.mark.unit
async def test_the_cost_cap_skips_the_intents_left(evals: Path) -> None:
    results, usage = await draft(
        evals,
        Pricey(),
        ["detention_request", "pod_follow_up_due", "question"],
        cost_cap_usd=0.015,
    )

    assert [r.status for r in results] == ["written", "written", "skipped"]
    assert results[2].message == "the cost cap was reached"
    assert len(usage) == 2


@pytest.mark.unit
def test_a_load_without_the_stop_of_the_stage_cannot_be_dated() -> None:
    with pytest.raises(DraftError, match="no delivery stop"):
        to_scenario(
            ScenarioDraft.model_validate(DETENTION),
            export=ACME,
            intent="detention_request",
            load={"stops": []},
            load_path="fixtures/x.json",
            broker_file=BROKER_FILE,
            section_ids=(),
        )


@pytest.mark.unit
def test_an_early_first_step_starts_the_scenario_at_midnight() -> None:
    early = {**DETENTION, "stage": "to_pickup", "steps": [{"at": "00:03", "note": "n"}]}
    load = {"stops": [{"type": "pickup", "appointment": {"start": "2026-10-13T06:00"}}]}

    scenario = to_scenario(
        ScenarioDraft.model_validate(early),
        export=BrokerExport(broker=ACME.broker),
        intent="question",
        load=load,
        load_path="fixtures/x.json",
        broker_file=BROKER_FILE,
        section_ids=(),
    )

    assert (scenario.date.isoformat(), scenario.start) == ("2026-10-13", "00:00")


@pytest.mark.unit
def test_the_prompt_says_when_no_section_applies() -> None:
    intent = ACME.broker.intent("other")
    assert intent is not None

    prompt = render_draft_prompt(ACME, intent, [], {"load_id": "1"})

    assert "## Sections for this intent\n\n(none)" in prompt
    assert "## Rules for every event\n\n(none)" in prompt
