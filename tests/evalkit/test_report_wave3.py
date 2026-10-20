"""Wave 3 in the report, the metadata and the suite: the agent loop section, the judge's
cost, the broker, the prompt hash across the AGT module moves, and trial warnings."""

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.domain.runs import RunStatus, TrajectoryStep, Usage
from freight_agent.evalkit.judge import JudgeResult, JudgeSpec, JudgeVerdict
from freight_agent.evalkit.metadata import PROMPT_MODULES, RunMetadata, prompts_sha256
from freight_agent.evalkit.report import render_markdown
from freight_agent.evalkit.results import ScenarioResult
from freight_agent.evalkit.runner import RunOutcome
from freight_agent.evalkit.spec import Scenario
from freight_agent.evalkit.suite import (
    ScenarioSummary,
    SuiteResult,
    SuiteRunner,
    discover,
    grade,
    select,
)
from tests.evalkit.test_graders import observe, run

META = RunMetadata(created_at=datetime(2026, 10, 20, tzinfo=UTC), mode="live", broker="acme@v2")
READ = TrajectoryStep(
    tool="read_sop_section", args={"section_id": "escalation"}, result="b", accepted=None
)
REFUSED = TrajectoryStep(tool="send_sms", args={}, result="no", accepted=False)


def trial(scenario_id: str, *, judged: float | None, loop: bool) -> ScenarioResult:
    agent_run = run(
        RunStatus.ACTED,
        trajectory=(READ, REFUSED) if loop else (),
        rejections=1 if loop else 0,
    )
    obs = observe(runs=[agent_run])
    if judged is not None:
        verdict = JudgeVerdict(score=judged, reasons=("r",))
        obs = obs.model_copy(
            update={"judge": JudgeResult(rubric="x", min_score=0.5, verdict=verdict)}
        )
    judge_usage = (Usage(role="judge", model="j", cost_usd=0.003),) if judged is not None else ()
    return ScenarioResult(
        scenario_id=scenario_id, passed=True, observations=(obs,), judge_usage=judge_usage
    )


def suite(*trials: ScenarioResult) -> SuiteResult:
    summaries = tuple(
        ScenarioSummary(
            scenario_id=t.scenario_id, title="t", tags=("regression",), k=1, trials=(t,)
        )
        for t in trials
    )
    return SuiteResult(suite="agent", mode="live", k=1, metadata=META, scenarios=summaries)


@pytest.mark.unit
def test_the_agent_loop_section_lists_scores_calls_refusals_and_sections() -> None:
    text = render_markdown(
        suite(
            trial("t1", judged=0.9, loop=True),
            trial("p3", judged=0.4, loop=False),
            trial("plain", judged=None, loop=False),
        )
    )

    section = text.split("## Agent loop", 1)[1].split("## Cost", 1)[0]
    assert "| t1 | 0.90 min, 0.90 mean | 2 | 1 | escalation |" in section
    assert "| p3 | 0.40 min, 0.40 mean | 0 | 0 | - |" in section
    assert "plain" not in section
    assert "The judge's share: $0.0060." in text
    assert "| Broker | acme@v2 |" in text


@pytest.mark.unit
def test_a_run_without_loop_or_judge_has_no_agent_loop_section() -> None:
    text = render_markdown(suite(trial("plain", judged=None, loop=False)))

    assert "## Agent loop" not in text
    assert "judge's share" not in text


@pytest.mark.unit
def test_the_prompt_hash_skips_modules_that_do_not_exist_and_raises_on_broken_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "broken_prompts_mod.py").write_text("import not_a_dependency_at_all\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    with_missing = prompts_sha256((*PROMPT_MODULES, "freight_agent.application.not_a_module"))

    assert with_missing == prompts_sha256()
    assert prompts_sha256(PROMPT_MODULES[:1]) != prompts_sha256()
    with pytest.raises(ModuleNotFoundError):
        prompts_sha256(("broken_prompts_mod",))
    sys.modules.pop("broken_prompts_mod", None)


@pytest.mark.unit
def test_graded_trials_carry_the_judge_usage_and_warnings_of_the_outcome() -> None:
    scenario = Scenario.model_validate(
        {
            "id": "s",
            "title": "t",
            "suite": "agent",
            "load": "fixtures/load_481207.json",
            "date": "2026-10-13",
            "start": "07:00",
            "steps": [{"at": "07:05"}],
        }
    )
    usage = (Usage(role="judge", model="j", cost_usd=0.01),)
    outcome = RunOutcome(
        scenario_id="s", observations=(observe(),), judge_usage=usage, warnings=("w",)
    )

    result = grade(scenario, outcome)

    assert (result.judge_usage, result.warnings) == (usage, ("w",))
    assert result.cost_usd == pytest.approx(0.01)
    assert result.judge_cost_usd == pytest.approx(0.01)


@pytest.mark.unit
async def test_a_skipped_rubric_warns_once_per_scenario_over_its_trials() -> None:
    root = Path(__file__).resolve().parents[2]
    (scenario,) = select(discover(root / "evals", "core"), ids=["b_ack_with_nothing_open"])
    step = scenario.steps[0]
    rubric = step.expect.model_copy(update={"judge": JudgeSpec(rubric="quiet")})
    judged = scenario.model_copy(update={"steps": (step.model_copy(update={"expect": rubric}),)})
    runner = SuiteRunner(
        evals_dir=root / "evals",
        knowledge=MemorySopIndex.from_files(root / "sops"),
        mode="scripted",
    )

    result = await runner.run([judged], suite="core", metadata=META, k=2)

    assert result.scenarios[0].passed
    assert result.warnings[0].startswith("scripted mode is deterministic")
    assert result.warnings[1:] == (
        "b_ack_with_nothing_open: step 0: the rubric was not judged (scripted mode, no "
        "script.judge)",
    )
