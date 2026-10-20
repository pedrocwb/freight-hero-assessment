"""The agent loop's feedback on a LangSmith experiment: `rejections`, `tools_used` and
`judge_score` per trial, the run's means on the experiment, and the trajectory in each
step's outputs. The trial is graded by hand (synthetic run records, as card W3-AGT will
record them) and fed to the experiment's spans; the client is the fake of
`test_langsmith_experiment.py`."""

from pathlib import Path
from typing import Any

import pytest

from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.runs import RunStatus, TrajectoryStep
from freight_agent.evalkit.judge import JudgeResult, JudgeVerdict
from freight_agent.evalkit.langsmith_experiment import LangSmithExperiment, example_inputs
from freight_agent.evalkit.results import ScenarioResult, StepObservation
from freight_agent.evalkit.suite import ScenarioSummary, SuiteResult, discover
from tests.evalkit.test_graders import observe, run
from tests.evalkit.test_langsmith_experiment import FakeClient, evals, metadata, write

__all__ = ["evals"]  # the fixture, shared

REFUSED = TrajectoryStep(
    tool="send_email", args={"to_contact_ids": ["x@y.z"]}, result="no", accepted=False
)
SEARCH = TrajectoryStep(
    tool="search_sop", args={"query": "q"}, result="1. escalation", accepted=None
)
READ = TrajectoryStep(
    tool="read_sop_section", args={"section_id": "escalation"}, result="b", accepted=None
)


def judged(obs: StepObservation, score: float, reason: str) -> StepObservation:
    verdict = JudgeVerdict(score=score, reasons=(reason,))
    result = JudgeResult(rubric="r", min_score=0.5, verdict=verdict)
    return obs.model_copy(update={"judge": result})


def graded() -> ScenarioResult:
    agent_run = run(
        RunStatus.ESCALATED,
        gate=GateDecision(intent=Intent.QUESTION, act=True, reason="r"),
        intent="other",
        trajectory=(SEARCH, READ, REFUSED, SEARCH),
        rejections=1,
        broker_id="acme",
    )
    first = judged(observe(runs=[agent_run]), 0.8, "polite")
    second = judged(observe(runs=[run(RunStatus.NOOP, run_id="run_2")], step_index=1), 0.4, "vague")
    return ScenarioResult(scenario_id="a_ack", passed=True, observations=(first, second))


async def record(evals: Path, client: FakeClient, result: ScenarioResult) -> LangSmithExperiment:
    write(evals, "a_ack")
    (scenario,) = discover(evals, "core")
    experiment = LangSmithExperiment(
        client, suite="core", dataset_scenarios=[scenario], metadata=metadata(), k=1
    )
    experiment.start()
    async with experiment.trial(scenario, 1) as span:
        span.graded(result)
    summary = ScenarioSummary(
        scenario_id="a_ack", title="t", tags=("regression",), k=1, trials=(result,)
    )
    suite = SuiteResult(suite="core", mode="live", k=1, metadata=metadata(), scenarios=(summary,))
    experiment.finish(suite)
    return experiment


@pytest.mark.unit
async def test_a_trial_gets_rejections_tools_used_and_the_judges_mean(evals: Path) -> None:
    client = FakeClient()

    await record(evals, client, graded())

    (trial,) = client.runs_named("a_ack")
    feedback = client.feedback_of(trial["id"])
    assert feedback["rejections"]["score"] == 1.0
    assert feedback["rejections"]["comment"] == "send_email x1"
    assert feedback["tools_used"]["score"] == 4.0
    assert feedback["tools_used"]["comment"] == (
        "read_sop_section x1, search_sop x2, send_email x1"
    )
    assert feedback["judge_score"]["score"] == pytest.approx(0.6)
    assert feedback["judge_score"]["comment"] == "step 0: 0.80: polite\nstep 1: 0.40: vague"


@pytest.mark.unit
async def test_the_experiment_gets_the_refusals_per_run_and_the_mean_verdict(evals: Path) -> None:
    client = FakeClient()

    await record(evals, client, graded())

    project_id = client.projects[0]["id"]
    totals = {f["key"]: f["score"] for f in client.feedback if f.get("project_id") == project_id}
    assert totals["rejections_per_run"] == pytest.approx(0.5)  # 1 refusal over 2 runs
    assert totals["judge_score"] == pytest.approx(0.6)


@pytest.mark.unit
async def test_a_trial_without_a_judged_step_gets_no_judge_score(evals: Path) -> None:
    client = FakeClient()
    plain = ScenarioResult(scenario_id="a_ack", passed=True, observations=(observe(),))

    await record(evals, client, plain)

    (trial,) = client.runs_named("a_ack")
    feedback = client.feedback_of(trial["id"])
    assert "judge_score" not in feedback
    assert (feedback["rejections"]["score"], feedback["rejections"]["comment"]) == (0.0, None)
    assert feedback["tools_used"]["score"] == 0.0
    totals = {f["key"] for f in client.feedback if f.get("project_id") == client.projects[0]["id"]}
    assert "judge_score" not in totals and "rejections_per_run" not in totals


@pytest.mark.unit
async def test_the_trial_outputs_show_the_trajectory_and_the_verdict(evals: Path) -> None:
    client = FakeClient()

    await record(evals, client, graded())

    (trial,) = client.runs_named("a_ack")
    outputs: dict[str, Any] = trial["outputs"]
    first = outputs["steps"][0]
    (agent_run,) = first["runs"]
    assert (agent_run["intent"], agent_run["gate_intent"]) == ("other", "question")
    assert agent_run["broker_id"] == "acme"
    assert [s["tool"] for s in agent_run["trajectory"]] == [
        "search_sop",
        "read_sop_section",
        "send_email",
        "search_sop",
    ]
    assert agent_run["trajectory"][2]["accepted"] is False
    assert agent_run["sections_read"] == ["escalation"]
    assert agent_run["rejections"] == 1
    assert first["judge"]["verdict"] == {"reasons": ["polite"], "score": 0.8}
    assert "usage" not in first["judge"]


@pytest.mark.unit
def test_a_scenarios_broker_and_attachments_are_dataset_inputs() -> None:
    root = Path(__file__).resolve().parents[2]
    (p1,) = [s for s in discover(root / "evals", "docs") if s.id == "p1_pod_clean_delivers"]

    inputs = example_inputs(p1)

    assert inputs["load_overrides"]["broker"] == {"id": "acme", "name": "Acme Freight"}
    (attachment,) = inputs["steps"][1]["message"]["attachments"]
    assert attachment["fixture"] == "fixtures/docs/pod_clean.pdf"
