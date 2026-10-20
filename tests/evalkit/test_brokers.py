"""Broker exports: the file format, the SOP index of a broker trial, and the lint that
finds the broker's intents no scenario covers."""

import json
from pathlib import Path
from typing import Any

import pytest

from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.domain.broker import IntentDef, ObligationDef, new_broker, standard_broker
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import SopLayer, SopSection
from freight_agent.evalkit.brokers import (
    BrokerExport,
    BrokerFileError,
    BrokerSopIndex,
    broker_index,
    covered_intents,
    exports_by_file,
    lint_broker,
    load_metadata,
    read_broker_export,
    runs_on,
)
from freight_agent.evalkit.spec import Scenario
from freight_agent.evalkit.suite import discover, suites
from freight_agent.ports.knowledge import SopIndex

ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "evals"
ACME = EVALS / "brokers" / "acme.json"


def section(section_id: str, client_id: str | None = "acme", **fields: Any) -> SopSection:
    layer = SopLayer.STANDARD if client_id is None else SopLayer.CLIENT
    return SopSection(
        section_id=section_id,
        layer=layer,
        client_id=client_id,
        title=section_id.title(),
        description=f"About {section_id}",
        body=f"The {section_id} body.",
        source=f"{client_id or 'standard'}/{section_id}.md",
        **fields,
    )


def export(*sections: SopSection, broker_id: str = "acme") -> BrokerExport:
    return BrokerExport(broker=new_broker(broker_id, broker_id.title()), sections=sections)


# --- the file ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_export_round_trips_through_its_flat_json(tmp_path: Path) -> None:
    original = export(section("detention", intents=("detention_request",)))
    path = tmp_path / "acme.json"
    path.write_text(original.to_json(), encoding="utf-8")

    data = json.loads(path.read_text())
    again = read_broker_export(path)

    assert data["broker_id"] == "acme" and "sections" in data and "broker" not in data
    assert again == original
    assert again.label() == "acme@v1"
    assert load_metadata(again) == {"broker": {"id": "acme", "name": "Acme"}}


@pytest.mark.unit
def test_code_may_pass_the_broker_and_sections_apart() -> None:
    nested = BrokerExport.model_validate({"broker": new_broker("acme", "Acme").model_dump()})

    assert nested.broker_id == "acme" and nested.sections == ()


@pytest.mark.unit
@pytest.mark.parametrize("foreign", [section("x", client_id="beta"), section("y", client_id=None)])
def test_an_export_holds_only_the_brokers_own_sections(foreign: SopSection) -> None:
    with pytest.raises(ValueError, match="are not acme's own"):
        export(foreign)


@pytest.mark.unit
def test_an_unreadable_or_invalid_file_is_a_broker_file_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"broker_id": "acme"}')

    with pytest.raises(BrokerFileError, match="cannot read the broker file"):
        read_broker_export(tmp_path / "missing.json")
    with pytest.raises(BrokerFileError, match=r"is not a broker export: broker\.name"):
        read_broker_export(bad)


@pytest.mark.unit
def test_the_shipped_acme_export_is_the_onboarded_acme_sop() -> None:
    acme = read_broker_export(ACME)
    broker = acme.broker

    assert acme.label() == "acme@v2"
    assert broker.rollout.previous_version == 1
    assert {"detention_request", "pod_follow_up_due"} <= {i.intent_id for i in broker.intents}
    pod = broker.obligation("pod")
    assert pod is not None and (pod.follow_up_minutes, pod.max_attempts) == (30, 1)
    assert broker.follow_up_intent("pod") == "pod_follow_up_due"
    eta = broker.obligation("eta_request")
    assert eta is not None and (eta.follow_up_minutes, eta.max_attempts) == (20, 3)
    (requirement,) = broker.requirements_at(Stage.AT_DELIVERY)
    assert (requirement.type_id, requirement.within_minutes) == ("pod", 120)
    assert requirement.opens_obligation == "pod"
    assert [s.section_id for s in acme.sections] == [
        "communication_rules",
        "delay_risk_high",
        "detention",
        "driver_questions",
        "proof_of_delivery",
    ]


# --- the index ----------------------------------------------------------------------------


BASE = MemorySopIndex(
    (
        section("escalation", client_id=None),
        section("communication_rules", client_id=None),
        section("communication_rules", client_id="acme", intents=("question",)),
        section("delay_risk_high", client_id="beta"),
    ),
    version="base",
)


@pytest.mark.unit
async def test_the_brokers_sections_replace_its_layer_and_leave_the_rest() -> None:
    index = broker_index(BASE, export(section("detention", intents=("detention_request",))))

    acme = [s.section_id for s in await index.visible("acme", ())]
    beta = [s.section_id for s in await index.visible("beta", ())]

    assert acme == ["communication_rules", "detention", "escalation"]
    rules = await index.read("acme", (), "communication_rules")
    assert rules is not None and rules.intents == ()  # the base's acme override is gone
    assert beta == ["communication_rules", "delay_risk_high", "escalation"]


@pytest.mark.unit
async def test_the_version_names_the_base_and_the_brokers_sections() -> None:
    one = broker_index(BASE, export(section("detention")))
    two = broker_index(BASE, export(section("detention", intents=("x",))))

    assert isinstance(one, BrokerSopIndex)
    assert one.content_version.startswith("base+acme:")
    assert one.content_version != getattr(two, "content_version")
    assert await one.version("acme") == one.content_version


@pytest.mark.unit
def test_a_broker_trial_needs_the_in_memory_index() -> None:
    class Remote:
        pass

    remote: Any = Remote()
    other: SopIndex = remote
    with pytest.raises(BrokerFileError, match="in-memory SOP index"):
        broker_index(other, export())


# --- the lint -----------------------------------------------------------------------------


def scenario(*steps: dict[str, Any], **fields: Any) -> Scenario:
    return Scenario.model_validate(
        {
            "id": fields.pop("id", "s1"),
            "title": "t",
            "suite": fields.pop("suite", "core"),
            "load": "fixtures/load_481207.json",
            "date": "2026-10-13",
            "start": "06:55",
            "steps": list(steps) or [{"at": "07:00"}],
            **fields,
        }
    )


def gate(intent: str) -> dict[str, Any]:
    return {"intent": intent, "act": True, "reason": "r"}


@pytest.mark.unit
def test_intents_are_covered_by_what_the_steps_say_and_by_construction() -> None:
    broker = standard_broker()
    s = scenario(
        {"at": "07:00", "ping": {"delay_risk": "high"}, "expect": {"intent": "x_expected"}},
        {
            "at": "07:05",
            "message": {"channel": "sms", "sender": "drv_20931", "body": "eta?"},
            "script": {
                "gate": [gate("question")],
                "agent": [
                    {
                        "intent": "y_relabel",
                        "actions": [
                            {"type": "schedule_timer", "kind": "eta_request", "due_in_minutes": 30}
                        ],
                    }
                ],
            },
        },
        {"at": "07:35", "script": {"agent": [{}]}},
        {"at": "07:40", "ping": {"delay_risk": "low"}},
        {
            "at": "07:45",
            "message": {
                "channel": "sms",
                "sender": "drv_20931",
                "body": " ",
                "attachments": [{"filename": "a.pdf", "fixture": "f.pdf"}],
            },
        },
        tags=["regression", "intent:z_tagged"],
    )

    assert covered_intents(s, broker) == {
        "x_expected",
        "question",
        "y_relabel",
        "delay_risk_high",
        "eta_follow_up_due",
        "delay_risk_low",
        "document",
        "z_tagged",
    }


@pytest.mark.unit
def test_a_follow_up_needs_a_kind_the_scenario_scheduled_before_the_time_step() -> None:
    broker = new_broker("acme", "Acme").model_copy(
        update={
            "obligations": (
                *standard_broker().obligations,
                ObligationDef(kind="pod", title="POD", description="d"),
            )
        }
    )
    sms_call = {
        "name": "send_sms",
        "args": {"contact_id": "drv_20931", "text": "POD?", "requests": "pod"},
    }
    by_tool = scenario(
        {"at": "07:00", "script": {"agent": [{"tool_calls": [sms_call]}]}},
        {"at": "07:30", "script": {"agent": [{}]}},
    )
    by_answer = scenario(
        {
            "at": "07:00",
            "script": {
                "agent": [
                    {
                        "actions": [
                            {"type": "send_sms", "contact_id": "d", "text": "x", "requests": "pod"}
                        ]
                    }
                ]
            },
        },
        {"at": "07:30", "script": {"agent": [{}]}},
    )
    too_early = scenario({"at": "07:30", "script": {"agent": [{}]}})

    assert covered_intents(by_tool, broker) == {"pod_follow_up_due"}
    assert covered_intents(by_answer, broker) == {"pod_follow_up_due"}
    assert covered_intents(too_early, broker) == set()


@pytest.mark.unit
def test_a_scenario_runs_on_a_broker_by_its_file_its_folder_its_override_or_a_shared_suite() -> (
    None
):
    files = {"brokers/acme.json": "acme", "brokers/broken.json": None}

    assert runs_on(scenario(broker_file="brokers/acme.json", suite="docs"), "acme", files)
    assert not runs_on(scenario(broker_file="brokers/broken.json"), "acme", files)
    assert runs_on(scenario(suite="acme"), "acme", files)
    assert runs_on(
        scenario(suite="other", load_overrides={"broker": {"id": "acme"}}), "acme", files
    )
    assert runs_on(scenario(suite="core", load_overrides={"stage": "at_delivery"}), "acme", files)
    assert runs_on(scenario(suite="agent"), "acme", files)
    assert not runs_on(scenario(suite="docs"), "acme", files)
    assert not runs_on(
        scenario(suite="core", load_overrides={"broker": {"id": "keystone"}}), "acme", files
    )


@pytest.mark.unit
def test_a_brokers_own_intent_needs_a_scenario_on_that_broker() -> None:
    detention = IntentDef(intent_id="detention_request", title="Detention", description="d")
    broker = new_broker("acme", "Acme").model_copy(
        update={"intents": (*standard_broker().intents, detention)}
    )
    elsewhere = scenario(
        {"at": "07:00", "expect": {"intent": "detention_request"}},
        suite="docs",
        broker_file="brokers/beta.json",
    )
    standard = [
        scenario({"at": "07:00", "expect": {"intent": i.intent_id}}, id=f"s_{i.intent_id}")
        for i in standard_broker().intents
    ]

    issues = lint_broker(broker, [*standard, elsewhere], {"brokers/beta.json": "beta"})

    assert [i.intent for i in issues] == ["detention_request"]
    assert "no scenario that runs on acme covers it" in str(issues[0])
    assert str(issues[0]).startswith("error: intent detention_request:")
    on_acme = elsewhere.model_copy(update={"broker_file": "brokers/acme.json"})
    assert lint_broker(broker, [*standard, on_acme], {"brokers/acme.json": "acme"}) == []


@pytest.mark.unit
def test_a_standard_intent_may_be_covered_on_any_broker() -> None:
    covered_on_beta = scenario({"at": "07:00", "expect": {"intent": "other"}}, suite="beta")

    issues = lint_broker(standard_broker(), [covered_on_beta], {})

    assert "other" not in {i.intent for i in issues}
    assert "no scenario covers it" in str(next(i for i in issues if i.intent == "question"))


@pytest.mark.unit
def test_the_shipped_scenarios_cover_the_standard_catalog_and_all_of_acme_but_detention() -> None:
    every = [s for name in suites(EVALS) for s in discover(EVALS, name)]
    files = exports_by_file(EVALS, every)
    acme = read_broker_export(ACME).broker

    assert files == {"brokers/acme.json": "acme"}
    assert lint_broker(standard_broker(), every, files) == []
    assert [i.intent for i in lint_broker(acme, every, files)] == ["detention_request"]


@pytest.mark.unit
def test_an_unreadable_broker_file_maps_to_none(tmp_path: Path) -> None:
    named = [scenario(broker_file="brokers/nope.json"), scenario(broker_file="brokers/nope.json")]

    assert exports_by_file(tmp_path, named) == {"brokers/nope.json": None}
