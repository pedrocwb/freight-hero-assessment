"""Every scenario file parses, has a unique id, points at a fixture that exists and
declares the SOP sections it covers; no file carries a model script any more (every
scenario is played live), and the spec refuses one."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from freight_agent.evalkit.spec import Expectation, MessageStep, Step, load_scenario

EVALS = Path(__file__).resolve().parents[2] / "evals"
FILES = sorted((EVALS / "scenarios").rglob("*.yaml"))

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_scenario_file_is_valid(path: Path) -> None:
    scenario = load_scenario(path)

    assert (EVALS / scenario.load).is_file(), f"fixture {scenario.load} not found"
    assert any(t.startswith("sop:") for t in scenario.tags), "tag the SOP sections covered"
    assert scenario.suite == path.parent.name, "the suite is the scenario's directory name"
    for step in scenario.steps:
        for attachment in step.message.attachments if step.message else ():
            assert (EVALS / attachment.fixture).is_file(), f"{attachment.fixture} not found"


def keys(data: Any) -> set[str]:
    """Every mapping key at any depth of a YAML document."""
    if isinstance(data, dict):
        return {str(k) for k in data} | {k for v in data.values() for k in keys(v)}
    if isinstance(data, list):
        return {k for v in data for k in keys(v)}
    return set()


@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_no_scenario_file_carries_a_model_script(path: Path) -> None:
    assert "script" not in keys(yaml.safe_load(path.read_text(encoding="utf-8")))


def test_the_scenario_files_are_the_three_suites() -> None:
    assert {p.parent.name for p in FILES} == {"agent", "core", "docs"}
    assert len(FILES) == 26


def test_scenario_ids_are_unique() -> None:
    ids = [load_scenario(p).id for p in FILES]

    assert len(ids) == len(set(ids))


def test_a_repeated_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "dup.yaml"
    path.write_text(
        "id: x\ntitle: t\nsuite: core\nload: fixtures/load_481207.json\n"
        "date: 2026-10-13\nstart: '07:00'\n"
        "steps:\n  - at: '07:00'\n    expect:\n      runs: 0\n      runs: 1\n"
    )

    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key 'runs'"):
        load_scenario(path)


def test_a_step_with_a_script_is_refused() -> None:
    with pytest.raises(ValidationError, match="script"):
        Step.model_validate({"at": "07:00", "script": {"gate": []}})


# --- step kinds -----------------------------------------------------------------------------

MESSAGE = {"channel": "sms", "sender": "drv_20931", "body": "ok"}
POD = {"filename": "pod.pdf", "fixture": "fixtures/docs/pod_clean.pdf"}


@pytest.mark.parametrize(
    ("step", "kind"),
    [
        ({"message": MESSAGE}, "message"),
        ({"message": {**MESSAGE, "attachments": [POD]}}, "attachment"),
        ({"ping": {"delay_risk": "high"}}, "ping"),
        ({}, "wait"),
    ],
)
def test_a_step_kind_is_what_it_injects(step: dict[str, Any], kind: str) -> None:
    assert Step.model_validate({"at": "07:00", **step}).kind == kind


def test_every_kind_appears_in_the_shipped_scenarios() -> None:
    kinds = {step.kind for path in FILES for step in load_scenario(path).steps}

    assert kinds == {"message", "attachment", "ping", "wait"}


def test_a_step_injects_at_most_one_event() -> None:
    with pytest.raises(ValidationError, match="at most one message or ping"):
        Step.model_validate({"at": "07:00", "message": MESSAGE, "ping": {"delay_risk": "high"}})


# --- the spec ---------------------------------------------------------------------------------


def test_a_message_step_may_carry_attachments() -> None:
    step = MessageStep.model_validate({**MESSAGE, "attachments": [POD]})

    (attachment,) = step.attachments
    assert (attachment.filename, attachment.content_type) == ("pod.pdf", "application/pdf")


def test_obligation_expectations_take_any_kind_of_the_catalog() -> None:
    expectation = Expectation.model_validate({"obligations": {"pod": {"status": "open"}}})

    assert set(expectation.obligations) == {"pod"}
