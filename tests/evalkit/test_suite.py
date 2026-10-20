"""The suite runner: discovery, filters, k trials, pass@k and pass^k, cost cap, exit codes.

Scenarios here are small one-step files written to a temporary evals folder (an "ok"
from the driver with nothing open, which ends at the gate), so each test controls the
tags, the model's answers and their cost. The real core suite runs in test_cli.py and
in the negative control.
"""

import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.domain.actions import Plan
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.runs import Usage
from freight_agent.evalkit.graders import Budget
from freight_agent.evalkit.metadata import RunMetadata
from freight_agent.evalkit.runner import RuntimeLike, build_runtime
from freight_agent.evalkit.spec import Scenario
from freight_agent.evalkit.suite import (
    ExitCode,
    ScenarioStatus,
    SuiteError,
    SuiteResult,
    SuiteRunner,
    discover,
    select,
    suites,
)
from freight_agent.ports.clock import Clock
from freight_agent.ports.gateways import Gateways
from freight_agent.ports.knowledge import SopIndex
from freight_agent.ports.llm import LanguageModel, LlmRequest, LlmResponse, SchemaT
from freight_agent.settings import LlmMode

ROOT = Path(__file__).resolve().parents[2]
SOPS = MemorySopIndex.from_files(ROOT / "sops")

# One step: the driver says "ok" with nothing open. The right run is a gate-only noop.
ACK_STEPS = """\
steps:
  - at: "07:05"
    message: {channel: sms, sender: drv_20931, body: "ok"}
    expect:
      no_actions: true
      run_status: [noop]
      llm_calls: 1
    script:
      gate:
        - {intent: acknowledgement, act: false, reason: "only ok"}
"""


def write_scenario(
    evals: Path,
    scenario_id: str,
    *,
    suite: str = "core",
    tags: str = "[regression, sop:acknowledgements]",
    load: str = "fixtures/load_481207.json",
    steps: str = ACK_STEPS,
) -> Path:
    folder = evals / "scenarios" / suite
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{scenario_id}.yaml"
    path.write_text(
        f"id: {scenario_id}\ntitle: {scenario_id} title\nsuite: {suite}\ntags: {tags}\n"
        f"load: {load}\ndate: 2026-10-13\nstart: '06:55'\n{steps}",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def evals(tmp_path: Path) -> Path:
    root = tmp_path / "evals"
    (root / "fixtures").mkdir(parents=True)
    shutil.copy(ROOT / "evals" / "fixtures" / "load_481207.json", root / "fixtures")
    return root


def metadata(mode: LlmMode = "scripted") -> RunMetadata:
    return RunMetadata(created_at=datetime(2026, 10, 19, 12, tzinfo=UTC), mode=mode)


class AckModel:
    """A stand-in live model: the gate says "acknowledgement" and whether to act; decide
    returns an empty plan. Every call costs `cost_usd`."""

    def __init__(self, *, act: bool = False, cost_usd: float = 0.0) -> None:
        self._act = act
        self._cost = cost_usd
        self.calls = 0

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        self.calls += 1
        if schema is GateDecision:
            output = GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=self._act, reason="ok")
        else:
            output = Plan()
        usage = Usage(
            role=request.role.value,
            model="fake/model",
            input_tokens=100,
            output_tokens=20,
            cost_usd=self._cost,
        )
        return LlmResponse(parsed=schema.model_validate(output.model_dump()), usage=usage)


def factory_of(*models: AckModel) -> Callable[[Clock], LanguageModel]:
    """One model per trial, in order; the last one repeats."""
    queue = list(models)

    def build(clock: Clock) -> LanguageModel:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return build


async def run_suite(
    evals: Path,
    *,
    mode: LlmMode = "scripted",
    model: Callable[[Clock], LanguageModel] | None = None,
    k: int = 1,
    cost_cap_usd: float | None = None,
    budget: Budget | None = None,
) -> SuiteResult:
    runner = SuiteRunner(
        evals_dir=evals, knowledge=SOPS, mode=mode, model_factory=model, budget=budget
    )
    return await runner.run(
        discover(evals, "core"),
        suite="core",
        k=k,
        cost_cap_usd=cost_cap_usd,
        metadata=metadata(mode),
    )


# --- discovery and filters ------------------------------------------------------------


@pytest.mark.unit
def test_discovery_reads_every_scenario_of_the_suite_folder_in_file_order(evals: Path) -> None:
    write_scenario(evals, "b_second")
    write_scenario(evals, "a_first")
    write_scenario(evals, "other", suite="extra")

    assert [s.id for s in discover(evals, "core")] == ["a_first", "b_second"]
    assert suites(evals) == ["core", "extra"]


@pytest.mark.unit
def test_the_real_core_suite_has_its_22_scenarios() -> None:
    assert len(discover(ROOT / "evals", "core")) == 22


@pytest.mark.unit
def test_an_unknown_suite_names_the_suites_that_exist(evals: Path) -> None:
    write_scenario(evals, "a_first")

    with pytest.raises(SuiteError, match=r"no suite 'nope'.*core"):
        discover(evals, "nope")


@pytest.mark.unit
def test_every_bad_scenario_file_is_named_in_one_error(evals: Path) -> None:
    write_scenario(evals, "good")
    write_scenario(evals, "broken_yaml").write_text("id: [unclosed\n", encoding="utf-8")
    write_scenario(evals, "bad_time", steps="steps:\n  - at: '7 am'\n")

    with pytest.raises(SuiteError) as caught:
        discover(evals, "core")

    message = str(caught.value)
    assert "broken_yaml.yaml" in message
    assert "bad_time.yaml" in message
    assert "good.yaml" not in message


@pytest.mark.unit
def test_a_scenario_in_the_wrong_folder_or_with_a_repeated_id_is_an_error(evals: Path) -> None:
    write_scenario(evals, "a_first")
    write_scenario(evals, "a_copy").write_text(
        (evals / "scenarios" / "core" / "a_first.yaml").read_text(), encoding="utf-8"
    )
    misplaced = write_scenario(evals, "misplaced")
    misplaced.write_text(misplaced.read_text().replace("suite: core", "suite: other"))

    with pytest.raises(SuiteError) as caught:
        discover(evals, "core")

    message = str(caught.value)
    assert "id 'a_first' is also used by" in message
    assert "misplaced.yaml: suite is 'other'" in message


def _scenarios(evals: Path, tags: dict[str, str]) -> list[Scenario]:
    for scenario_id, tag_list in tags.items():
        write_scenario(evals, scenario_id, tags=tag_list)
    return discover(evals, "core")


@pytest.mark.unit
def test_tags_select_scenarios_with_any_of_them(evals: Path) -> None:
    scenarios = _scenarios(
        evals,
        {
            "r1": "[regression, sop:a]",
            "c1": "[capability, sop:b]",
            "r2": "[regression, sop:b]",
        },
    )

    assert [s.id for s in select(scenarios, tags=["capability"])] == ["c1"]
    assert [s.id for s in select(scenarios, tags=["sop:b", "capability"])] == ["c1", "r2"]
    assert [s.id for s in select(scenarios)] == ["c1", "r1", "r2"]


@pytest.mark.unit
def test_scenario_ids_and_tags_combine(evals: Path) -> None:
    scenarios = _scenarios(evals, {"r1": "[regression, sop:a]", "c1": "[capability, sop:a]"})

    assert [s.id for s in select(scenarios, ids=["r1"])] == ["r1"]
    with pytest.raises(SuiteError, match="no scenario matches"):
        select(scenarios, tags=["capability"], ids=["r1"])


@pytest.mark.unit
def test_an_unknown_scenario_id_is_an_error(evals: Path) -> None:
    scenarios = _scenarios(evals, {"r1": "[regression, sop:a]"})

    with pytest.raises(SuiteError, match=r"no scenario 'r9' in this suite"):
        select(scenarios, ids=["r9"])


# --- trials and aggregation -------------------------------------------------------------


@pytest.mark.unit
async def test_a_passing_scenario_in_scripted_mode(evals: Path) -> None:
    write_scenario(evals, "ack")

    result = await run_suite(evals)

    (summary,) = result.scenarios
    assert summary.status is ScenarioStatus.PASSED
    assert summary.passed and summary.pass_at_k
    assert summary.model_calls == 1
    assert [t.trial for t in summary.trials] == [1]
    assert result.exit_code is ExitCode.PASSED
    assert result.warnings == ()


@pytest.mark.unit
async def test_every_trial_gets_a_fresh_runtime(evals: Path) -> None:
    write_scenario(evals, "ack")
    built: list[RuntimeLike] = []

    def factory(
        clock: Clock, model: LanguageModel, gateways: Gateways, knowledge: SopIndex
    ) -> RuntimeLike:
        built.append(build_runtime(clock, model, gateways, knowledge))
        return built[-1]

    runner = SuiteRunner(evals_dir=evals, knowledge=SOPS, mode="scripted", runtime_factory=factory)
    result = await runner.run(discover(evals, "core"), suite="core", k=3, metadata=metadata())

    assert len({id(runtime) for runtime in built}) == 3
    assert [t.trial for t in result.scenarios[0].trials] == [1, 2, 3]
    assert result.scenarios[0].passed


@pytest.mark.unit
async def test_k_above_one_in_scripted_mode_runs_but_warns(evals: Path) -> None:
    write_scenario(evals, "ack")

    result = await run_suite(evals, k=2)

    assert len(result.scenarios[0].trials) == 2
    assert any("deterministic" in warning for warning in result.warnings)


@pytest.mark.unit
async def test_every_trial_gets_a_model_built_on_its_own_clock(evals: Path) -> None:
    write_scenario(evals, "ack")
    clocks: list[Clock] = []

    def build(clock: Clock) -> LanguageModel:
        clocks.append(clock)
        return AckModel()

    result = await run_suite(evals, mode="replay", model=build, k=2)

    assert result.scenarios[0].passed
    assert len(clocks) == 2
    assert clocks[0] is not clocks[1]
    # The scenario's virtual clock, not the wall clock: it reads the scenario's day.
    assert clocks[0].now().date().isoformat() == "2026-10-13"


@pytest.mark.unit
async def test_pass_at_k_and_pass_hat_k_differ_for_a_flaky_scenario(evals: Path) -> None:
    write_scenario(evals, "ack")
    # The second trial's gate acts on "ok": decide runs, so the step makes 2 model calls.
    flaky = factory_of(AckModel(), AckModel(act=True), AckModel())

    result = await run_suite(evals, mode="live", model=flaky, k=3)

    (summary,) = result.scenarios
    assert [t.passed for t in summary.trials] == [True, False, True]
    assert summary.trials_passed == 2
    assert summary.pass_at_k is True
    assert summary.passed is False  # pass^k: every trial must pass
    assert summary.status is ScenarioStatus.FAILED
    assert {f.check for f in summary.trials[1].failures} == {"expect.llm_calls"}
    assert result.exit_code is ExitCode.FAILED


@pytest.mark.unit
async def test_groups_aggregate_pass_at_k_and_pass_hat_k_per_tag(evals: Path) -> None:
    write_scenario(evals, "good", tags="[regression, sop:acknowledgements]")
    write_scenario(evals, "also_good", tags="[capability, sop:acknowledgements]")
    write_scenario(
        evals,
        "bad",
        tags="[capability, sop:escalation]",
        steps=ACK_STEPS.replace("llm_calls: 1", "llm_calls: 5"),
    )

    result = await run_suite(evals)

    groups = {g.tag: g for g in result.groups}
    assert list(groups) == ["regression", "capability", "sop:acknowledgements", "sop:escalation"]
    assert (groups["capability"].scenarios, groups["capability"].pass_hat_k) == (2, 0.5)
    assert groups["regression"].pass_hat_k == 1.0
    assert groups["sop:escalation"].pass_at_k == 0.0


@pytest.mark.unit
async def test_a_failing_capability_scenario_does_not_fail_the_run(evals: Path) -> None:
    write_scenario(evals, "good")
    write_scenario(
        evals,
        "hard",
        tags="[capability, sop:acknowledgements]",
        steps=ACK_STEPS.replace("llm_calls: 1", "llm_calls: 5"),
    )

    result = await run_suite(evals)

    assert [s.status for s in result.scenarios] == [ScenarioStatus.PASSED, ScenarioStatus.FAILED]
    assert result.exit_code is ExitCode.PASSED


@pytest.mark.unit
async def test_a_scenario_with_neither_tag_gates_the_run(evals: Path) -> None:
    write_scenario(
        evals,
        "untagged",
        tags="[sop:acknowledgements]",
        steps=ACK_STEPS.replace("llm_calls: 1", "llm_calls: 5"),
    )

    result = await run_suite(evals)

    assert result.scenarios[0].gating
    assert result.exit_code is ExitCode.FAILED


@pytest.mark.unit
async def test_a_trial_that_cannot_run_is_a_harness_error(evals: Path) -> None:
    write_scenario(evals, "good")
    write_scenario(evals, "no_fixture", load="fixtures/missing.json")

    result = await run_suite(evals)

    broken = result.scenarios[1]
    assert broken.status is ScenarioStatus.ERROR
    assert broken.trials[0].error == "load fixture not found: fixtures/missing.json"
    assert result.exit_code is ExitCode.HARNESS_ERROR


@pytest.mark.unit
async def test_a_model_that_cannot_be_built_is_a_harness_error_not_a_crash(evals: Path) -> None:
    write_scenario(evals, "ack")

    def broken(clock: Clock) -> LanguageModel:
        raise RuntimeError("no such model")

    result = await run_suite(evals, mode="live", model=broken)

    assert result.scenarios[0].trials[0].error == "RuntimeError: no such model"
    assert result.exit_code is ExitCode.HARNESS_ERROR


@pytest.mark.unit
async def test_usage_cost_tokens_and_model_calls_add_up(evals: Path) -> None:
    write_scenario(evals, "one")
    write_scenario(evals, "two")

    result = await run_suite(evals, mode="live", model=factory_of(AckModel(cost_usd=0.25)), k=2)

    assert [s.cost_usd for s in result.scenarios] == [0.5, 0.5]
    assert result.scenarios[0].tokens == 240
    assert result.scenarios[0].model_calls == 2
    assert result.cost_usd == 1.0
    assert result.tokens == 480
    assert result.model_calls == 4


@pytest.mark.unit
async def test_the_budget_is_checked_on_every_trial(evals: Path) -> None:
    write_scenario(evals, "ack")

    result = await run_suite(
        evals,
        mode="live",
        model=factory_of(AckModel(cost_usd=0.5)),
        budget=Budget(max_cost_usd=0.1),
    )

    checks = [f.check for f in result.scenarios[0].trials[0].failures]
    assert checks == ["invariant.budget.cost_usd"]


@pytest.mark.unit
async def test_the_run_stops_once_the_cost_passes_the_cap(evals: Path) -> None:
    for scenario_id in ("s1", "s2", "s3", "s4"):
        write_scenario(evals, scenario_id)

    # 0.4 per trial, k=2: s1 spends 0.8, the first trial of s2 brings it to 1.2 > 1.0.
    result = await run_suite(
        evals, mode="live", model=factory_of(AckModel(cost_usd=0.4)), k=2, cost_cap_usd=1.0
    )

    assert [s.scenario_id for s in result.scenarios] == ["s1", "s2"]
    assert result.scenarios[1].status is ScenarioStatus.INCOMPLETE
    assert len(result.scenarios[1].trials) == 1
    assert result.not_run == ("s3", "s4")
    assert result.stopped is not None
    assert "cost cap" in result.stopped
    assert result.exit_code is ExitCode.COST_CAP


@pytest.mark.unit
async def test_a_run_under_its_cap_is_not_stopped(evals: Path) -> None:
    write_scenario(evals, "s1")

    result = await run_suite(
        evals, mode="live", model=factory_of(AckModel(cost_usd=0.4)), cost_cap_usd=1.0
    )

    assert result.stopped is None
    assert result.exit_code is ExitCode.PASSED


@pytest.mark.unit
def test_modes_other_than_scripted_need_a_model_factory(evals: Path) -> None:
    with pytest.raises(ValueError, match="replay mode needs a model"):
        SuiteRunner(evals_dir=evals, knowledge=SOPS, mode="replay")
    with pytest.raises(ValueError, match="scripted mode"):
        SuiteRunner(
            evals_dir=evals, knowledge=SOPS, mode="scripted", model_factory=factory_of(AckModel())
        )


@pytest.mark.unit
async def test_the_progress_callback_sees_every_scenario(evals: Path) -> None:
    write_scenario(evals, "one")
    write_scenario(evals, "two")
    seen: list[str] = []

    runner = SuiteRunner(evals_dir=evals, knowledge=SOPS, mode="scripted")
    await runner.run(
        discover(evals, "core"),
        suite="core",
        metadata=metadata(),
        on_scenario=lambda summary: seen.append(summary.scenario_id),
    )

    assert seen == ["one", "two"]
