"""The `agent` command line: eval run, list and report, sop lint, and the exit codes.

Every test runs from an empty temporary folder with the variables of `Settings` cleared,
so no `.env` or shell variable of the developer leaks in; the paths point back at the
repository's evals, SOPs and models through the same variables a deployment would set.
Run folders always go to a temporary `--out`.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from freight_agent import cli
from freight_agent.domain.actions import Plan
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.runs import Usage
from freight_agent.evalkit.report import REPORT_FILE, RESULTS_FILE
from freight_agent.evalkit.suite import ModelFactory
from freight_agent.ports.clock import Clock
from freight_agent.ports.llm import LanguageModel, LlmRequest, LlmResponse, SchemaT
from freight_agent.settings import LlmMode, Settings

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EVALS_DIR", str(ROOT / "evals"))
    monkeypatch.setenv("SOPS_DIR", str(ROOT / "sops"))
    monkeypatch.setenv("MODELS_CONFIG", str(ROOT / "config" / "models.yaml"))
    monkeypatch.setenv("CASSETTES_DIR", str(tmp_path / "cassettes"))
    return tmp_path


def agent(*args: str) -> Result:
    return runner.invoke(cli.app, list(args))


def run_folders(out: Path) -> list[Path]:
    return sorted(p for p in out.iterdir() if p.is_dir())


def own_evals(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenarios: dict[str, str]) -> Path:
    """A private evals folder with the shipped fixture and the given scenario files."""
    evals = tmp_path / "evals"
    (evals / "fixtures").mkdir(parents=True)
    shutil.copy(ROOT / "evals" / "fixtures" / "load_481207.json", evals / "fixtures")
    (evals / "scenarios" / "core").mkdir(parents=True)
    for name, text in scenarios.items():
        (evals / "scenarios" / "core" / f"{name}.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("EVALS_DIR", str(evals))
    return evals


def ack_scenario(scenario_id: str, *, llm_calls: int = 1) -> str:
    """The driver says "ok" with nothing open: a gate-only noop (1 model call)."""
    return f"""\
id: {scenario_id}
title: {scenario_id}
suite: core
tags: [regression, sop:acknowledgements]
load: fixtures/load_481207.json
date: 2026-10-13
start: "06:55"
steps:
  - at: "07:05"
    message: {{channel: sms, sender: drv_20931, body: "ok"}}
    expect: {{no_actions: true, run_status: [noop], llm_calls: {llm_calls}}}
    script:
      gate: [{{intent: acknowledgement, act: false, reason: only ok}}]
"""


# --- top level --------------------------------------------------------------------------


@pytest.mark.unit
def test_version() -> None:
    result = agent("--version")

    assert result.exit_code == 0
    assert result.output.strip() == "freight-agent 0.1.0"


@pytest.mark.unit
def test_help_lists_the_command_groups() -> None:
    result = agent("--help")

    assert result.exit_code == 0
    assert "eval" in result.output
    assert "sop" in result.output


@pytest.mark.unit
def test_importing_the_cli_loads_neither_the_harness_nor_langchain() -> None:
    probe = (
        "import sys, freight_agent.cli; "
        "heavy = ('langchain', 'langgraph', 'freight_agent.evalkit.runner', "
        "'freight_agent.bootstrap'); "
        "print(sorted(m for m in heavy if m in sys.modules))"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, cwd=ROOT
    )

    assert done.stdout.strip() == "[]"


# --- eval run ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_core_suite_passes_in_scripted_mode_and_writes_its_report(env: Path) -> None:
    out = env / "results"

    result = agent("eval", "run", "--suite", "core", "--mode", "scripted", "--out", str(out))

    assert result.exit_code == 0, result.output
    assert "22 scenarios: 22 passed, 0 failed, 0 errors" in result.output
    assert "verdict: passed (exit code 0)" in result.output
    (folder,) = run_folders(out)
    assert folder.name.endswith("-core-scripted")
    data = json.loads((folder / RESULTS_FILE).read_text(encoding="utf-8"))
    assert len(data["scenarios"]) == 22
    assert data["metadata"]["settings"]["LLM_MODE"] == "scripted"
    assert "| a_client_a_high_risk |" in (folder / REPORT_FILE).read_text(encoding="utf-8")
    assert f"report: {folder / REPORT_FILE}" in result.output


@pytest.mark.unit
def test_filters_pick_scenarios_by_id_and_tag(env: Path) -> None:
    out = env / "results"

    by_id = agent("eval", "run", "--scenario", "b_ack_with_nothing_open", "--out", str(out))
    by_tag = agent("eval", "run", "--tags", "capability", "--out", str(out))

    assert by_id.exit_code == 0, by_id.output
    assert "1 scenarios: 1 passed" in by_id.output
    assert by_tag.exit_code == 0, by_tag.output
    assert "c3_relative_eta" in by_tag.output
    assert "d4_lumper_no_sop" in by_tag.output
    assert "2 scenarios: 2 passed" in by_tag.output


@pytest.mark.unit
def test_comma_separated_filters_are_split(env: Path) -> None:
    result = agent(
        "eval",
        "run",
        "--scenario",
        "b_ack_with_nothing_open,c3_relative_eta",
        "--out",
        str(env / "results"),
    )

    assert result.exit_code == 0, result.output
    assert "2 scenarios: 2 passed" in result.output


@pytest.mark.unit
def test_k_above_one_in_scripted_mode_warns(env: Path) -> None:
    result = agent(
        "eval", "run", "--scenario", "b_ack_with_nothing_open", "--k", "2", "--out", str(env)
    )

    assert result.exit_code == 0, result.output
    assert "warning: scripted mode is deterministic" in result.output


@pytest.mark.unit
def test_a_failing_regression_scenario_exits_1(monkeypatch: pytest.MonkeyPatch, env: Path) -> None:
    own_evals(
        monkeypatch, env, {"good": ack_scenario("good"), "bad": ack_scenario("bad", llm_calls=3)}
    )

    result = agent("eval", "run", "--out", str(env / "results"))

    assert result.exit_code == 1, result.output
    assert "FAIL       bad" in result.output
    assert "step 0 expect.llm_calls: expected 3 model call(s), got 1" in result.output
    assert "verdict: failed (exit code 1)" in result.output


@pytest.mark.unit
def test_a_bad_scenario_file_exits_2_and_names_the_file(
    monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    own_evals(monkeypatch, env, {"good": ack_scenario("good"), "broken": "id: [unclosed\n"})

    result = agent("eval", "run", "--out", str(env / "results"))

    assert result.exit_code == 2
    assert "broken.yaml" in result.stderr
    assert not (env / "results").exists()


@pytest.mark.unit
def test_an_unknown_scenario_or_suite_exits_2(env: Path) -> None:
    unknown_scenario = agent("eval", "run", "--scenario", "nope", "--out", str(env))
    unknown_suite = agent("eval", "run", "--suite", "nope", "--out", str(env))

    assert unknown_scenario.exit_code == 2
    assert "no scenario 'nope'" in unknown_scenario.stderr
    assert unknown_suite.exit_code == 2
    assert "no suite 'nope'" in unknown_suite.stderr


@pytest.mark.unit
def test_an_unknown_model_set_exits_2(env: Path) -> None:
    result = agent("eval", "run", "--model-set", "cheap", "--out", str(env))

    assert result.exit_code == 2
    assert "unknown model set 'cheap'" in result.stderr
    assert "models.cheap.yaml does not exist" in result.stderr


@pytest.mark.unit
def test_a_missing_sop_folder_exits_2(monkeypatch: pytest.MonkeyPatch, env: Path) -> None:
    monkeypatch.setenv("SOPS_DIR", str(env / "no_sops"))

    result = agent("eval", "run", "--out", str(env))

    assert result.exit_code == 2
    assert "the SOP folder" in result.stderr


@pytest.mark.unit
def test_live_mode_without_the_key_exits_2_before_any_trial(env: Path) -> None:
    result = agent("eval", "run", "--mode", "live", "--out", str(env / "results"))

    assert result.exit_code == 2
    assert "OPENROUTER_API_KEY is not set" in result.stderr
    assert not (env / "results").exists()


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["scripted", "replay"])
def test_langsmith_experiments_are_live_only(env: Path, mode: str) -> None:
    result = agent("eval", "run", "--mode", mode, "--langsmith", "--out", str(env / "results"))

    assert result.exit_code == 2
    assert "--langsmith records live runs only" in result.stderr
    assert not (env / "results").exists()


@pytest.mark.unit
def test_langsmith_without_its_key_exits_2_before_any_trial(
    monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    result = agent("eval", "run", "--mode", "live", "--langsmith", "--out", str(env / "results"))

    assert result.exit_code == 2
    assert "--langsmith needs LANGSMITH_API_KEY" in result.stderr
    assert not (env / "results").exists()


@pytest.mark.unit
def test_replay_without_cassettes_exits_2_and_says_how_to_record(env: Path) -> None:
    out = env / "results"

    result = agent(
        "eval",
        "run",
        "--mode",
        "replay",
        "--scenario",
        "b_ack_with_nothing_open",
        "--out",
        str(out),
    )

    assert result.exit_code == 2, result.output
    assert "ERROR      b_ack_with_nothing_open" in result.output
    assert "CassetteMissing: no cassette for the gate call of model openai/gpt-4.1-nano" in (
        result.output
    )
    assert "verdict: error (exit code 2)" in result.output
    (folder,) = run_folders(out)
    assert "record it" in (folder / REPORT_FILE).read_text(encoding="utf-8")


class CostlyModel:
    """A stand-in live model: every call costs $0.50; the gate never acts."""

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        output = (
            GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=False, reason="ok")
            if schema is GateDecision
            else Plan()
        )
        usage = Usage(role=request.role.value, model="fake", cost_usd=0.5)
        return LlmResponse(parsed=schema.model_validate(output.model_dump()), usage=usage)


@pytest.mark.unit
def test_the_cost_cap_stops_the_run_and_exits_3(monkeypatch: pytest.MonkeyPatch, env: Path) -> None:
    own_evals(monkeypatch, env, {f"s{i}": ack_scenario(f"s{i}") for i in range(1, 5)})

    def factory(settings: Settings, mode: LlmMode) -> ModelFactory:
        def build(clock: Clock) -> LanguageModel:
            return CostlyModel()

        return build

    monkeypatch.setattr(cli, "eval_model_factory", factory)

    result = agent("eval", "run", "--mode", "live", "--cost-cap-usd", "1", "--out", str(env / "r"))

    assert result.exit_code == 3, result.output
    assert "stopped by the cost cap: $1.5000 spent, over the cap of $1.00" in result.output
    assert "not run: s4" in result.output
    assert "verdict: aborted (exit code 3)" in result.output


@pytest.mark.unit
def test_a_run_compares_with_a_baseline(monkeypatch: pytest.MonkeyPatch, env: Path) -> None:
    evals = own_evals(monkeypatch, env, {"one": ack_scenario("one")})
    out = env / "results"
    assert agent("eval", "run", "--out", str(out)).exit_code == 0
    (baseline,) = run_folders(out)
    (evals / "scenarios" / "core" / "one.yaml").write_text(ack_scenario("one", llm_calls=4))

    result = agent("eval", "run", "--compare", str(baseline), "--out", str(out))

    assert result.exit_code == 1
    assert "vs baseline: 1 new failure (one), 0 fixed, cost +$0.0000" in result.output
    latest = run_folders(out)[-1]
    assert "New failures: one" in (latest / REPORT_FILE).read_text(encoding="utf-8")


@pytest.mark.unit
def test_a_missing_baseline_exits_2_before_running(env: Path) -> None:
    result = agent("eval", "run", "--compare", str(env / "nothing"), "--out", str(env / "r"))

    assert result.exit_code == 2
    assert "cannot read the baseline" in result.stderr
    assert not (env / "r").exists()


# --- eval list / report -----------------------------------------------------------------


@pytest.mark.unit
def test_list_shows_every_scenario_with_its_tags() -> None:
    result = agent("eval", "list")

    assert result.exit_code == 0
    assert "core (22 scenarios)" in result.output
    line = next(x for x in result.output.splitlines() if "d4_lumper_no_sop" in x)
    assert "capability, sop:driver_questions" in line


@pytest.mark.unit
def test_report_renders_a_results_file_again(env: Path) -> None:
    out = env / "results"
    agent("eval", "run", "--scenario", "b_ack_with_nothing_open", "--out", str(out))
    (folder,) = run_folders(out)
    (folder / REPORT_FILE).unlink()

    result = agent("eval", "report", str(folder / RESULTS_FILE))

    assert result.exit_code == 0, result.output
    assert "PASS       b_ack_with_nothing_open" in result.output
    assert "verdict: passed" in result.output
    assert (folder / REPORT_FILE).is_file()


@pytest.mark.unit
def test_report_of_something_else_exits_2(env: Path) -> None:
    result = agent("eval", "report", str(env))

    assert result.exit_code == 2
    assert "no results.json" in result.stderr


# --- sop lint ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sop_lint_passes_on_the_shipped_sops() -> None:
    result = agent("sop", "lint")

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1].startswith("0 errors, ")


def write_sop(root: Path, relative: str, header: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{header}\n---\nThe procedure.\n", encoding="utf-8")


@pytest.mark.unit
def test_sop_lint_warnings_alone_exit_0(monkeypatch: pytest.MonkeyPatch, env: Path) -> None:
    sops = env / "sops"
    write_sop(sops, "standard/uncovered.md", "id: uncovered\ntitle: Uncovered")
    monkeypatch.setenv("SOPS_DIR", str(sops))

    result = agent("sop", "lint")

    assert result.exit_code == 0
    assert "warning: standard/uncovered.md: no eval scenario covers section uncovered" in (
        result.output
    )
    assert "0 errors, 1 warnings" in result.output


@pytest.mark.unit
def test_sop_lint_errors_exit_1(monkeypatch: pytest.MonkeyPatch, env: Path) -> None:
    sops = env / "sops"
    write_sop(sops, "standard/acknowledgements.md", "id: acknowledgements\nintents: [nope]")
    monkeypatch.setenv("SOPS_DIR", str(sops))

    result = agent("sop", "lint")

    assert result.exit_code == 1
    assert "error: standard/acknowledgements.md: 'nope' in `intents` is not a known intent" in (
        result.output
    )
    assert "1 errors, 0 warnings" in result.output
