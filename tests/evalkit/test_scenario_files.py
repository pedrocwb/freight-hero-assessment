"""Every scenario file parses, has a unique id, points at a fixture that exists and
declares the SOP sections it covers."""

from pathlib import Path

import pytest
import yaml

from freight_agent.domain.actions import Plan
from freight_agent.evalkit.spec import Expectation, MessageStep, ScriptedLlm, load_scenario

EVALS = Path(__file__).resolve().parents[2] / "evals"
FILES = sorted((EVALS / "scenarios").rglob("*.yaml"))


@pytest.mark.unit
@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_scenario_file_is_valid(path: Path) -> None:
    scenario = load_scenario(path)

    assert (EVALS / scenario.load).is_file(), f"fixture {scenario.load} not found"
    assert any(t.startswith("sop:") for t in scenario.tags), "tag the SOP sections covered"
    assert scenario.suite == path.parent.name, "the suite is the scenario's directory name"


@pytest.mark.unit
def test_scenario_ids_are_unique() -> None:
    ids = [load_scenario(p).id for p in FILES]

    assert len(ids) == len(set(ids))


@pytest.mark.unit
def test_a_repeated_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "dup.yaml"
    path.write_text(
        "id: x\ntitle: t\nsuite: core\nload: fixtures/load_481207.json\n"
        "date: 2026-10-13\nstart: '07:00'\n"
        "steps:\n  - at: '07:00'\n    expect:\n      runs: 0\n      runs: 1\n"
    )

    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key 'runs'"):
        load_scenario(path)


# --- wave 3 spec (C0) ------------------------------------------------------------------------


@pytest.mark.unit
def test_decide_is_the_older_name_of_agent_and_comes_first() -> None:
    script = ScriptedLlm.model_validate(
        {"decide": [{"rationale": "first"}], "agent": [{"rationale": "second"}]}
    )

    assert [p.rationale for p in script.agent] == ["first", "second"]
    assert script.decide == script.agent
    assert ScriptedLlm(agent=(Plan(),)).agent == (Plan(),)
    assert ScriptedLlm.model_validate({"decide": None}).agent == ()


@pytest.mark.unit
def test_a_message_step_may_carry_attachments() -> None:
    step = MessageStep.model_validate(
        {
            "channel": "sms",
            "sender": "drv_20931",
            "body": "POD attached",
            "attachments": [{"filename": "pod.pdf", "fixture": "fixtures/docs/pod_clean.pdf"}],
        }
    )

    (attachment,) = step.attachments
    assert (attachment.filename, attachment.content_type) == ("pod.pdf", "application/pdf")


@pytest.mark.unit
def test_obligation_expectations_take_any_kind_of_the_catalog() -> None:
    expectation = Expectation.model_validate({"obligations": {"pod": {"status": "open"}}})

    assert set(expectation.obligations) == {"pod"}
