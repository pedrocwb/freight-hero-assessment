"""The `agent` command line: eval run and list, sop lint, and the exit codes.

Every test runs from an empty temporary folder with the variables of `Settings` cleared,
so no `.env` or shell variable of the developer leaks in; the paths point back at the
repository's evals, SOPs and models through the same variables a deployment would set.

`agent eval run` is live by design (a LangSmith experiment on OpenRouter), so here it
never reaches either: `run_experiment`, the LangSmith client, the model and the native
tracing switch are replaced, and the test checks what the command hands them and how it
reports what comes back.
"""

import subprocess
import sys
from pathlib import Path
from typing import Any

import langsmith
import pytest
from typer.testing import CliRunner, Result

from freight_agent import bootstrap, cli
from freight_agent.evalkit import experiment
from freight_agent.evalkit.experiment import (
    ExperimentRun,
    LangSmithUnavailable,
    ScenarioOutcome,
    ScenarioTarget,
)
from freight_agent.evalkit.metadata import RunMetadata
from freight_agent.evalkit.results import Failure
from freight_agent.evalkit.suite import discover
from freight_agent.settings import Settings
from tests.fakes import FakeModel

ROOT = Path(__file__).resolve().parents[2]
CORE = {s.id: s for s in discover(ROOT / "evals", "core")}
runner = CliRunner()

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in [*Settings.model_fields, "langsmith_endpoint"]:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EVALS_DIR", str(ROOT / "evals"))
    monkeypatch.setenv("SOPS_DIR", str(ROOT / "sops"))
    monkeypatch.setenv("MODELS_CONFIG", str(ROOT / "config" / "models.yaml"))
    return tmp_path


def agent(*args: str) -> Result:
    return runner.invoke(cli.app, list(args))


class Offline:
    """Stands in for everything `agent eval run` would reach: the LangSmith client, the
    model, the tracing switch and `run_experiment`, which records its arguments and
    returns `run` (or raises it)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.run: ExperimentRun | Exception | None = None
        self.calls: list[dict[str, Any]] = []
        self.clients: list[dict[str, Any]] = []
        monkeypatch.setattr(langsmith, "Client", self._client)
        monkeypatch.setattr(bootstrap, "build_llm", lambda settings, **kwargs: FakeModel())
        monkeypatch.setattr(bootstrap, "enable_native_tracing", lambda settings: None)
        monkeypatch.setattr(experiment, "run_experiment", self._run_experiment)

    def _client(self, **kwargs: Any) -> object:
        self.clients.append(kwargs)
        return object()

    async def _run_experiment(self, client: Any, **kwargs: Any) -> ExperimentRun:
        self.calls.append({"client": client, **kwargs})
        if isinstance(self.run, Exception):
            raise self.run
        assert self.run is not None, "the test did not say what the experiment returns"
        return self.run


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> Offline:
    return Offline(monkeypatch)


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")


def outcome(
    scenario_id: str, *passed: bool, not_run: int = 0, cost_usd: float = 0.0
) -> ScenarioOutcome:
    failed = (Failure(step_index=0, check="expect.llm_calls", message="expected 1, got 2"),)
    return ScenarioOutcome(
        scenario=CORE[scenario_id],
        passed=passed,
        failures=tuple(() if p else failed for p in passed),
        cost_usd=cost_usd,
        not_run=not_run,
    )


def experiment_run(
    *outcomes: ScenarioOutcome, not_run: tuple[str, ...] = (), url: str | None = "https://ls/x"
) -> ExperimentRun:
    return ExperimentRun(
        name="core-k1-abc",
        url=url,
        outcomes=outcomes,
        cost_usd=sum(o.cost_usd for o in outcomes),
        not_run=not_run,
    )


# --- top level --------------------------------------------------------------------------


def test_version() -> None:
    result = agent("--version")

    assert result.exit_code == 0
    assert result.output.strip() == "freight-agent 0.1.0"


def test_help_lists_the_command_groups() -> None:
    result = agent("--help")

    assert result.exit_code == 0
    assert "eval" in result.output
    assert "sop" in result.output


def test_importing_the_cli_loads_neither_the_harness_nor_langchain() -> None:
    probe = (
        "import sys, freight_agent.cli; "
        "heavy = ('langchain', 'langgraph', 'langsmith', 'freight_agent.evalkit.runner', "
        "'freight_agent.bootstrap'); "
        "print(sorted(m for m in heavy if m in sys.modules))"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, cwd=ROOT
    )

    assert done.stdout.strip() == "[]"


def test_eval_report_is_gone() -> None:
    result = agent("eval", "report", "results.json")

    assert result.exit_code == 2
    assert "No such command 'report'" in result.output


# --- eval run: what it refuses ----------------------------------------------------------


def test_eval_run_needs_the_langsmith_key(offline: Offline) -> None:
    result = agent("eval", "run")

    assert result.exit_code == 2
    assert "LANGSMITH_API_KEY is not set" in result.stderr
    assert offline.calls == [] and offline.clients == []


def test_eval_run_needs_the_openrouter_key(
    monkeypatch: pytest.MonkeyPatch, offline: Offline
) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2-test")

    result = agent("eval", "run")

    assert result.exit_code == 2
    assert "OPENROUTER_API_KEY is not set" in result.stderr
    assert offline.calls == [] and offline.clients == []


@pytest.mark.usefixtures("keys")
def test_an_unknown_scenario_or_suite_exits_2(offline: Offline) -> None:
    unknown_scenario = agent("eval", "run", "--scenario", "nope")
    unknown_suite = agent("eval", "run", "--suite", "nope")

    assert unknown_scenario.exit_code == 2
    assert "no scenario 'nope'" in unknown_scenario.stderr
    assert unknown_suite.exit_code == 2
    assert "no suite 'nope'" in unknown_suite.stderr
    assert offline.calls == []


@pytest.mark.usefixtures("keys")
def test_a_bad_scenario_file_exits_2_and_names_the_file(
    monkeypatch: pytest.MonkeyPatch, env: Path, offline: Offline
) -> None:
    folder = env / "evals" / "scenarios" / "core"
    folder.mkdir(parents=True)
    (folder / "broken.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("EVALS_DIR", str(env / "evals"))

    result = agent("eval", "run")

    assert result.exit_code == 2
    assert "broken.yaml" in result.stderr
    assert offline.calls == []


@pytest.mark.usefixtures("keys")
def test_a_missing_sop_folder_exits_2(
    monkeypatch: pytest.MonkeyPatch, env: Path, offline: Offline
) -> None:
    monkeypatch.setenv("SOPS_DIR", str(env / "no_sops"))

    result = agent("eval", "run")

    assert result.exit_code == 2
    assert "the SOP folder" in result.stderr
    assert offline.calls == []


@pytest.mark.usefixtures("keys")
def test_langsmith_unavailable_exits_2_and_says_nothing_ran(offline: Offline) -> None:
    offline.run = LangSmithUnavailable("ConnectError: no route")

    result = agent("eval", "run", "--scenario", "b_ack_with_nothing_open")

    assert result.exit_code == 2
    assert "LangSmith: ConnectError: no route; nothing was run" in result.stderr


# --- eval run: the experiment and its verdict ------------------------------------------


@pytest.mark.usefixtures("keys")
def test_eval_run_hands_the_experiment_the_suite_the_choice_and_the_limits(
    offline: Offline,
) -> None:
    offline.run = experiment_run(outcome("b_ack_with_nothing_open", True, True))

    result = agent(
        "eval",
        "run",
        "--scenario",
        "c3_relative_eta,b_ack_with_nothing_open",
        "--k",
        "2",
        "--cost-cap-usd",
        "0.5",
        "--concurrency",
        "2",
    )

    assert result.exit_code == 0, result.output
    assert "core: 2 scenarios, k=2, live on OpenRouter, cost cap $0.50" in result.output
    (call,) = offline.calls
    assert call["suite"] == "core"
    assert len(call["suite_scenarios"]) == len(CORE)
    assert [s.id for s in call["chosen"]] == ["b_ack_with_nothing_open", "c3_relative_eta"]
    assert (call["k"], call["concurrency"], call["agent"]) == (2, 2, "freight-agent")
    target: ScenarioTarget = call["target"]
    assert (target.meter.cap_usd, target.meter.spent_usd) == (0.5, 0.0)
    metadata: RunMetadata = call["metadata"]
    assert (metadata.suite, metadata.k, metadata.cost_cap_usd) == ("core", 2, 0.5)
    assert metadata.git_sha is None  # the temporary folder is not a checkout
    assert "agent:standard" in metadata.prompts and metadata.models
    assert offline.clients == [{"api_key": "lsv2-test", "api_url": None}]


@pytest.mark.usefixtures("keys")
def test_a_passing_run_prints_the_table_and_exits_0(offline: Offline) -> None:
    offline.run = experiment_run(outcome("b_ack_with_nothing_open", True, cost_usd=0.0123))

    result = agent("eval", "run", "--scenario", "b_ack_with_nothing_open")

    assert result.exit_code == cli.PASSED, result.output
    assert cli.render_table(offline.run, 1) in result.output
    assert "  b_ack_with_nothing_open  pass     1/1  $0.0123" in result.output
    assert "pass^1: 1/1 scenarios, $0.0123 spent" in result.output
    assert result.output.rstrip().endswith("https://ls/x")


@pytest.mark.usefixtures("keys")
def test_a_regression_that_fails_pass_hat_k_exits_1(offline: Offline) -> None:
    offline.run = experiment_run(
        outcome("b_ack_with_nothing_open", True, False), outcome("c3_relative_eta", False, False)
    )

    result = agent("eval", "run", "--k", "2")

    assert result.exit_code == cli.FAILED, result.output
    line = next(x for x in result.output.splitlines() if "b_ack_with_nothing_open" in x)
    assert "FAIL" in line and "1/2" in line
    assert "step 0: expect.llm_calls: expected 1, got 2" in line


@pytest.mark.usefixtures("keys")
def test_a_failing_capability_scenario_alone_exits_0(offline: Offline) -> None:
    offline.run = experiment_run(
        outcome("b_ack_with_nothing_open", True), outcome("c3_relative_eta", False)
    )

    result = agent("eval", "run")

    assert result.exit_code == cli.PASSED, result.output
    assert "pass^1: 1/2 scenarios" in result.output


@pytest.mark.usefixtures("keys")
def test_examples_the_cost_cap_stopped_exit_3(offline: Offline) -> None:
    offline.run = experiment_run(
        outcome("b_ack_with_nothing_open", True),
        outcome("c1_eta_from_driver", not_run=1),
        not_run=("c1_eta_from_driver",),
    )

    result = agent("eval", "run")

    assert result.exit_code == cli.COST_CAP, result.output
    line = next(x for x in result.output.splitlines() if x.strip().startswith("c1_eta"))
    assert "not run" in line and "0/1" in line
    assert "not run (cost cap): c1_eta_from_driver" in result.output


@pytest.mark.usefixtures("keys")
def test_a_regression_failure_wins_over_the_cost_cap(offline: Offline) -> None:
    offline.run = experiment_run(
        outcome("b_ack_with_nothing_open", False),
        outcome("c1_eta_from_driver", not_run=1),
        not_run=("c1_eta_from_driver",),
    )

    result = agent("eval", "run")

    assert result.exit_code == cli.FAILED


def test_the_table_says_when_there_is_no_link() -> None:
    table = cli.render_table(experiment_run(outcome("b_ack_with_nothing_open", True), url=None), 1)

    assert table.splitlines()[0] == "experiment core-k1-abc"
    assert table.splitlines()[-1] == "  (no experiment link)"


def test_exit_codes_follow_the_outcomes() -> None:
    def code(*outcomes: ScenarioOutcome, not_run: tuple[str, ...] = ()) -> int:
        return cli.exit_code(experiment_run(*outcomes, not_run=not_run))

    assert code(outcome("b_ack_with_nothing_open", True)) == cli.PASSED
    assert code(outcome("b_ack_with_nothing_open", False)) == cli.FAILED
    assert code(outcome("c3_relative_eta", False)) == cli.PASSED  # capability
    assert code(outcome("c1_eta_from_driver", not_run=1), not_run=("c1",)) == cli.COST_CAP


# --- eval list --------------------------------------------------------------------------


def test_list_shows_every_suite_and_scenario_with_its_tags() -> None:
    result = agent("eval", "list")

    assert result.exit_code == 0
    assert [x for x in result.output.splitlines() if not x.startswith(" ")] == [
        "agent (1 scenarios)",
        "core (22 scenarios)",
        "docs (3 scenarios)",
    ]
    line = next(x for x in result.output.splitlines() if "d4_lumper_no_sop" in x)
    assert "capability, sop:driver_questions" in line


def test_list_of_one_suite() -> None:
    result = agent("eval", "list", "--suite", "docs")

    assert result.exit_code == 0
    assert result.output.splitlines()[0] == "docs (3 scenarios)"
    assert "p1_pod_clean_delivers" in result.output
    assert "a_client_a_high_risk" not in result.output


def test_list_of_an_unknown_suite_exits_2() -> None:
    result = agent("eval", "list", "--suite", "nope")

    assert result.exit_code == 2
    assert "no suite 'nope'" in result.stderr


def test_list_without_suites_says_so(monkeypatch: pytest.MonkeyPatch, env: Path) -> None:
    monkeypatch.setenv("EVALS_DIR", str(env / "empty"))

    result = agent("eval", "list")

    assert result.exit_code == 0
    assert f"no suites in {env / 'empty' / 'scenarios'}" in result.output


# --- sop lint ---------------------------------------------------------------------------


def test_sop_lint_passes_on_the_shipped_sops() -> None:
    result = agent("sop", "lint")

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1].startswith("0 errors, ")


def write_sop(root: Path, relative: str, header: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{header}\n---\nThe procedure.\n", encoding="utf-8")


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


def test_sop_lint_without_readable_scenarios_skips_the_coverage(
    monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    folder = env / "evals" / "scenarios" / "core"
    folder.mkdir(parents=True)
    (folder / "broken.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("EVALS_DIR", str(env / "evals"))

    result = agent("sop", "lint")

    assert result.exit_code == 0, result.output
    assert "warning: coverage not checked, the scenarios cannot be read" in result.output
