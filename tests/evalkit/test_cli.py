"""The `agent` command line: eval run and list, sop lint, and the exit codes.

Every test runs from an empty temporary folder with the variables of `Settings` cleared,
so no `.env` or shell variable of the developer leaks in; the paths point back at the
repository's evals, SOPs and models through the same variables a deployment would set.

`agent eval run` is live by design (a LangSmith experiment on OpenRouter), so here it
never reaches either: `run_experiment`, the LangSmith client, the model, the prompt
templates (the repository copy, in place of the Hub's) and the native tracing switch are
replaced, and the test checks what the command hands them and how it reports what comes
back.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import langsmith
import pytest
from typer.testing import CliRunner, Result

from freight_agent import bootstrap, cli
from freight_agent.adapters.decisions.openrouter import OpenRouterDecisions
from freight_agent.application.gate import GateEngine
from freight_agent.domain.actions import Plan
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
from freight_agent.interfaces.llm import ModelRole
from freight_agent.settings import Settings
from tests.evalkit.test_player import scenario_a_agent
from tests.fakes import FakeModel
from tests.prompts import PROMPTS

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
    monkeypatch.setenv("PROMPTS_CONFIG", str(ROOT / "config" / "prompts.yaml"))
    monkeypatch.setenv("PROMPTS_DIR", str(ROOT / "prompts"))
    return tmp_path


def agent(*args: str) -> Result:
    return runner.invoke(cli.app, list(args))


class Offline:
    """Stands in for everything `agent eval run` would reach: the LangSmith client, the
    model, the Hub's prompts (`prompt_settings` keeps what they were asked with), the
    tracing switch and `run_experiment`, which records its arguments and returns `run`
    (or raises it)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.run: ExperimentRun | Exception | None = None
        self.calls: list[dict[str, Any]] = []
        self.clients: list[dict[str, Any]] = []
        self.prompt_settings: list[Settings] = []
        monkeypatch.setattr(langsmith, "Client", self._client)
        monkeypatch.setattr(bootstrap, "build_llm", lambda settings, **kwargs: FakeModel())
        monkeypatch.setattr(bootstrap, "build_prompts", self._prompts)
        monkeypatch.setattr(bootstrap, "enable_native_tracing", lambda settings: None)
        monkeypatch.setattr(experiment, "run_experiment", self._run_experiment)

    def _prompts(self, settings: Settings) -> Any:
        self.prompt_settings.append(settings)
        return PROMPTS

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
    assert "prompts" in result.output


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
    assert metadata.models
    assert (metadata.prompts, metadata.prompt_refs) == (PROMPTS.versions(), PROMPTS.refs())
    assert target._prompts is PROMPTS  # pyright: ignore[reportPrivateUsage]
    (asked,) = offline.prompt_settings
    assert asked.prompts_tag is None  # each role at its pin
    prompts = ", ".join(p.describe() for p in PROMPTS.all())
    assert f"prompts: {prompts}" in result.output
    assert offline.clients == [{"api_key": "lsv2-test", "api_url": None}]


@pytest.mark.usefixtures("keys")
def test_eval_run_on_the_llm_gate_records_it_and_builds_no_decision_model(
    offline: Offline,
) -> None:
    offline.run = experiment_run(outcome("b_ack_with_nothing_open", True))

    result = agent("eval", "run", "--scenario", "b_ack_with_nothing_open", "--gate-engine", "llm")

    assert result.exit_code == 0, result.output
    assert "gate engine: llm" in result.output
    (call,) = offline.calls
    metadata: RunMetadata = call["metadata"]
    assert (metadata.gate_engine, metadata.gate_act_threshold) == ("llm", None)
    assert call["target"]._decisions is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.usefixtures("keys")
def test_eval_run_on_the_default_jev_gate_records_it_and_hands_the_trials_its_model(
    offline: Offline, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GATE_ACT_THRESHOLD", "0.15")
    offline.run = experiment_run(outcome("b_ack_with_nothing_open", True))

    result = agent("eval", "run", "--scenario", "b_ack_with_nothing_open")  # jev by default

    assert result.exit_code == 0, result.output
    assert "gate engine: jev" in result.output
    (call,) = offline.calls
    metadata: RunMetadata = call["metadata"]
    assert (metadata.gate_engine, metadata.gate_act_threshold) == ("jev", 0.15)
    assert metadata.for_langsmith()["model_decision_gate"] == "typesafe/jev-1.14"
    target: ScenarioTarget = call["target"]
    config = target._config  # pyright: ignore[reportPrivateUsage]
    assert config is not None and config.gate_engine is GateEngine.JEV
    assert isinstance(target._decisions, OpenRouterDecisions)  # pyright: ignore[reportPrivateUsage]


def test_eval_run_refuses_a_gate_engine_it_does_not_know(offline: Offline) -> None:
    result = agent("eval", "run", "--gate-engine", "gpt")

    assert result.exit_code == 2
    assert "--gate-engine is llm or jev, not 'gpt'" in result.stderr
    assert offline.calls == []


@pytest.mark.usefixtures("keys")
def test_eval_run_on_a_prompt_tag_resolves_every_role_at_it(offline: Offline) -> None:
    offline.run = experiment_run(outcome("b_ack_with_nothing_open", True))

    result = agent(
        "eval", "run", "--scenario", "b_ack_with_nothing_open", "--prompt-tag", "candidate"
    )

    assert result.exit_code == 0, result.output
    (asked,) = offline.prompt_settings
    assert asked.prompts_tag == "candidate"


@pytest.mark.usefixtures("keys")
def test_prompts_the_hub_cannot_serve_exit_2_before_anything_runs(
    offline: Offline, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(settings: Settings) -> Any:
        raise bootstrap.BootstrapError("the agent prompt freight-agent-agent:prod is not there")

    monkeypatch.setattr(bootstrap, "build_prompts", missing)

    result = agent("eval", "run", "--scenario", "b_ack_with_nothing_open")

    assert result.exit_code == cli.HARNESS_ERROR
    assert "freight-agent-agent:prod is not there" in result.stderr
    assert offline.calls == []


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


# --- play -----------------------------------------------------------------------------


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> FakeModel:
    """`agent play` on the scripted right agent of scenario A: the chat-model gate, the
    offline embeddings, the repository's prompts (no LangSmith key) and no tracing."""
    model = scenario_a_agent()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("GATE_ENGINE", "llm")
    monkeypatch.setenv("MEMORY_EMBEDDINGS", "fake")
    monkeypatch.setenv("LANGSMITH_TRACING", "")  # restored after the test
    monkeypatch.setattr(bootstrap, "build_llm", lambda settings, **kwargs: model)
    return model


def test_play_without_a_choice_exits_2() -> None:
    result = agent("play", "--no-upload")

    assert result.exit_code == cli.HARNESS_ERROR
    assert "say which scenarios to play" in result.stderr


@pytest.mark.usefixtures("scripted")
def test_play_refuses_unknown_ids_and_tags_nobody_has() -> None:
    unknown = agent("play", "nope", "--no-upload")
    no_tag = agent("play", "--tag", "sop:nope", "--no-upload")

    assert unknown.exit_code == no_tag.exit_code == cli.HARNESS_ERROR
    assert "no scenario 'nope' in any suite" in unknown.stderr
    assert "no scenario in any suite has the tag sop:nope" in no_tag.stderr


def test_play_uploads_by_default_so_it_needs_the_langsmith_key(scripted: FakeModel) -> None:
    result = agent("play", "a_client_a_high_risk")

    assert result.exit_code == cli.HARNESS_ERROR
    assert "LANGSMITH_API_KEY is not set" in result.stderr
    assert scripted.calls == []


def test_play_no_upload_prints_the_transcript_and_exits_0(scripted: FakeModel) -> None:
    result = agent("play", "a_client_a_high_risk", "--no-upload")

    assert result.exit_code == cli.PASSED, result.output
    out = result.output
    assert "playing 1 scenario, k=1, live on OpenRouter, cost cap $0.05, local only" in out
    assert "== a_client_a_high_risk  (core)" in out
    assert '       did    SMS to drv_20931 (driver): "The PO number is 55821."' in out
    assert "PASSED  a_client_a_high_risk  4/4 steps passed  $0.0000" in out
    assert out.rstrip().splitlines()[-2:] == ["1 scenario: 1 passed, 0 failed", "cost: $0.0000"]
    assert "\x1b[" not in out  # no color when the output is not a terminal
    assert scripted.remaining() == 0


def test_play_json_prints_only_the_result(scripted: FakeModel) -> None:
    result = agent("play", "a_client_a_high_risk", "--no-upload", "--json")

    assert result.exit_code == cli.PASSED, result.output
    data = json.loads(result.stdout)
    assert (data["k"], data["exit_code"], data["passed"], data["experiments"]) == (1, 0, True, [])
    (scenario,) = data["scenarios"]
    assert (scenario["id"], scenario["suite"], scenario["passed"]) == (
        "a_client_a_high_risk",
        "core",
        True,
    )
    (only,) = scenario["plays"]
    assert [s["at"] for s in only["steps"]] == ["07:00", "07:12", "07:31", "08:01"]
    assert "playing 1 scenario" in result.stderr


def test_a_failing_play_exits_1_and_shows_the_failed_check(scripted: FakeModel) -> None:
    scripted.push(ModelRole.AGENT, Plan(rationale="nothing to do"))  # a second play
    result = agent("play", "a_client_a_high_risk", "--no-upload", "--k", "2")

    assert result.exit_code == cli.FAILED, result.output
    assert "(run 1 of 2)" in result.output and "(run 2 of 2)" in result.output
    assert "✗ expect.actions[0]" in result.output
    assert "pass^2: 0/1" in result.output


def test_the_cost_cap_stops_new_plays_and_exits_3(
    scripted: FakeModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ScenarioTarget.__call__

    async def costly(self: ScenarioTarget, inputs: dict[str, Any]) -> dict[str, Any]:
        outputs = await original(self, inputs)
        self.meter.spent_usd += 1.0
        return outputs

    monkeypatch.setattr(ScenarioTarget, "__call__", costly)

    result = agent("play", "a_client_a_high_risk", "--no-upload", "--k", "2")

    assert result.exit_code == cli.COST_CAP, result.output
    assert "not run (cost cap): a_client_a_high_risk" in result.output


@pytest.mark.usefixtures("scripted")
def test_play_with_upload_runs_one_experiment_per_suite_and_links_it(
    monkeypatch: pytest.MonkeyPatch, offline: Offline
) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2-test")

    async def play_them(client: Any, **kwargs: Any) -> ExperimentRun:
        offline.calls.append(kwargs)
        target: ScenarioTarget = kwargs["target"]
        rows = [
            ({"scenario_id": s.id}, await target({"scenario_id": s.id}), None)
            for s in kwargs["chosen"]
        ]
        found = experiment.outcomes({s.id: s for s in kwargs["chosen"]}, rows)
        return ExperimentRun("core-k1-x", "https://ls/x", tuple(found), 0.0, ())

    monkeypatch.setattr(experiment, "run_experiment", play_them)
    monkeypatch.setattr(bootstrap, "build_llm", lambda settings, **kwargs: scenario_a_agent())

    result = agent("play", "a_client_a_high_risk")

    assert result.exit_code == cli.PASSED, result.output
    (call,) = offline.calls
    assert (call["suite"], call["k"], call["concurrency"]) == ("core", 1, 1)
    assert call["description"] == "agent play, suite core, k=1"
    assert "uploaded to LangSmith" in result.output
    assert "== a_client_a_high_risk  (core)" in result.output
    assert "LangSmith experiment core-k1-x: https://ls/x" in result.output


# --- eval list --------------------------------------------------------------------------


def test_list_shows_every_suite_and_scenario_with_its_tags() -> None:
    result = agent("eval", "list")

    assert result.exit_code == 0
    assert [x for x in result.output.splitlines() if not x.startswith(" ")] == [
        "agent (1 scenarios)",
        "core (22 scenarios)",
        "docs (3 scenarios)",
        "memory (3 scenarios)",
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
