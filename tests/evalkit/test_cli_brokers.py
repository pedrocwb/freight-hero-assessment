"""The broker commands: `agent eval run --broker-file`, `agent eval lint` and `agent eval
draft`. Same setup as `test_cli.py`: an empty working folder, the settings' variables
cleared and pointed back at the repository's evals, SOPs and models."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from freight_agent import cli
from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.evalkit.draft import ScenarioDraft
from freight_agent.evalkit.report import RESULTS_FILE
from freight_agent.evalkit.suite import ModelFactory
from freight_agent.ports.clock import Clock
from freight_agent.ports.llm import LanguageModel, ModelRole
from freight_agent.settings import LlmMode, Settings
from tests.evalkit.test_cli import ROOT, ack_scenario, agent, env, run_folders

__all__ = ["env"]  # the autouse fixture of test_cli.py, shared

ACME = ROOT / "evals" / "brokers" / "acme.json"


def own_evals(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenarios: dict[str, str]) -> Path:
    evals = tmp_path / "evals"
    (evals / "fixtures").mkdir(parents=True)
    (evals / "brokers").mkdir()
    shutil.copy(ROOT / "evals" / "fixtures" / "load_481207.json", evals / "fixtures")
    shutil.copy(ACME, evals / "brokers")
    (evals / "scenarios" / "core").mkdir(parents=True)
    for name, text in scenarios.items():
        (evals / "scenarios" / "core" / f"{name}.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("EVALS_DIR", str(evals))
    return evals


# --- eval run --broker-file ---------------------------------------------------------------


@pytest.mark.unit
def test_the_core_suite_runs_on_the_acme_export(env: Path) -> None:
    out = env / "results"

    result = agent("eval", "run", "--broker-file", str(ACME), "--out", str(out))

    assert result.exit_code == 0, result.output
    assert "core: 22 scenarios, mode scripted, k=1, broker acme@v2" in result.output
    (folder,) = run_folders(out)
    data = json.loads((folder / RESULTS_FILE).read_text(encoding="utf-8"))
    assert data["metadata"]["broker"] == "acme@v2"
    assert "+acme:" in data["metadata"]["sop_version"]
    load = data["scenarios"][0]["trials"][0]["observations"][0]["snapshot"]["load"]
    assert load["broker_id"] == "acme"


@pytest.mark.unit
def test_a_bad_broker_file_exits_2(env: Path) -> None:
    bad = env / "bad.json"
    bad.write_text("{}")

    result = agent("eval", "run", "--broker-file", str(bad), "--out", str(env / "results"))

    assert result.exit_code == 2
    assert "is not a broker export" in result.stderr
    assert not (env / "results").exists()


# --- eval lint ----------------------------------------------------------------------------


@pytest.mark.unit
def test_lint_passes_the_standard_catalog_on_the_shipped_scenarios() -> None:
    result = agent("eval", "lint")

    assert result.exit_code == 0, result.output
    assert result.output.strip().startswith("0 errors: standard, 12 intents,")


@pytest.mark.unit
def test_lint_reports_the_acme_intent_with_no_scenario() -> None:
    result = agent("eval", "lint", "--broker-file", str(ACME))

    assert result.exit_code == 1, result.output
    assert "error: intent detention_request: no scenario that runs on acme covers it" in (
        result.output
    )
    assert "1 errors: acme, 14 intents," in result.output


@pytest.mark.unit
def test_lint_exits_2_when_the_scenarios_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    own_evals(monkeypatch, env, {"broken": "id: broken\n"})

    result = agent("eval", "lint")

    assert result.exit_code == 2
    assert "bad scenario files" in result.stderr


# --- eval draft ---------------------------------------------------------------------------

DRAFT: dict[str, Any] = {
    "title": "Detention at Valley DC",
    "description": "A wait over two hours.",
    "stage": "at_delivery",
    "steps": [
        {
            "at": "11:45",
            "note": "The driver reports the wait",
            "message": {"channel": "sms", "sender": "driver", "body": "still waiting since 9:10"},
            "expect_actions": [{"kind": "hero_task", "count": 1}],
        }
    ],
}


def compile_models(monkeypatch: pytest.MonkeyPatch, *drafts: dict[str, Any]) -> ScriptedModel:
    model = ScriptedModel(model_name="compile/model")
    model.push(ModelRole.COMPILE, *(ScenarioDraft.model_validate(d) for d in drafts))

    def factory(settings: Settings, mode: LlmMode) -> ModelFactory:
        def build(clock: Clock) -> LanguageModel:
            return model

        return build

    monkeypatch.setattr(cli, "eval_model_factory", factory)
    return model


def shipped_evals(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A copy of the repository's evals folder, so a draft lands in the copy."""
    evals = tmp_path / "evals"
    shutil.copytree(ROOT / "evals", evals, ignore=shutil.ignore_patterns("results", "cassettes"))
    monkeypatch.setenv("EVALS_DIR", str(evals))
    return evals


@pytest.mark.unit
def test_draft_writes_the_intents_lint_reports(monkeypatch: pytest.MonkeyPatch, env: Path) -> None:
    evals = shipped_evals(monkeypatch, env)
    acme = evals / "brokers" / "acme.json"
    model = compile_models(monkeypatch, DRAFT)

    result = agent("eval", "draft", "--broker-file", str(acme), "--mode", "live")

    assert result.exit_code == 0, result.output
    path = evals / "scenarios" / "acme" / "_draft_detention_request.yaml"
    assert result.output.splitlines()[0].split() == ["written", "detention_request", str(path)]
    assert "1 compile call(s), $0.0000" in result.output
    assert "broker_file: brokers/acme.json" in path.read_text()
    assert len(model.calls) == 1
    # The draft covers the intent, so the broker's lint passes and drafting again does nothing.
    assert agent("eval", "lint", "--broker-file", str(acme)).exit_code == 0
    again = agent("eval", "draft", "--broker-file", str(acme), "--mode", "live")
    assert again.exit_code == 0
    assert "nothing to draft: every intent of acme has a scenario" in again.output
    assert len(model.calls) == 1


@pytest.mark.unit
def test_draft_takes_chosen_intents_and_keeps_existing_files(
    monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    evals = own_evals(monkeypatch, env, {"one": ack_scenario("one")})
    kept = evals / "scenarios" / "acme" / "_draft_question.yaml"
    kept.parent.mkdir(parents=True)
    kept.write_text("# mine\n")
    outside = env / "acme_copy.json"
    shutil.copy(ACME, outside)
    compile_models(monkeypatch, DRAFT)

    result = agent(
        "eval",
        "draft",
        "--broker-file",
        str(outside),
        "--mode",
        "replay",
        "--intent",
        "question,nope",
        "--intent",
        "status_update",
    )

    assert result.exit_code == 1, result.output  # "nope" failed
    assert f"exists   question                       {kept} (kept" in result.output
    assert "failed   nope                           not an intent of the broker" in result.output
    written = evals / "scenarios" / "acme" / "_draft_status_update.yaml"
    assert f"broker_file: {outside.resolve()}" in written.read_text()
    assert kept.read_text() == "# mine\n"


@pytest.mark.unit
def test_draft_refuses_scripted_mode_and_a_missing_key(env: Path) -> None:
    scripted = agent("eval", "draft", "--broker-file", str(ACME), "--mode", "scripted")
    live = agent("eval", "draft", "--broker-file", str(ACME), "--mode", "live")

    assert scripted.exit_code == 2
    assert "drafting calls the compile model" in scripted.stderr
    assert live.exit_code == 2
    assert "OPENROUTER_API_KEY is not set" in live.stderr
