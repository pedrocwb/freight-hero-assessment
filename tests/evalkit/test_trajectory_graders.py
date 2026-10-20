"""Wave 3 graders: the agent loop's trajectory and the judge's verdict.

The observations are synthetic run records (what card W3-AGT's agent node records), so
the graders are pinned before the agent loop exists. As in `test_graders.py`, every check
has a passing case and a failing one, and the failure message lists what happened.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.runs import RunRecord, RunStatus, TrajectoryStep
from freight_agent.evalkit.graders import (
    rejections,
    run_intent,
    sections_read,
    tool_calls,
)
from freight_agent.evalkit.judge import JudgeResult, JudgeVerdict
from freight_agent.evalkit.results import StepObservation
from freight_agent.evalkit.spec import Expectation
from tests.evalkit.test_graders import checks, expect, grade, observe, run

RUBRIC = "The SMS is polite and asks for the ETA."


def tool(name: str, accepted: bool | None = None, **args: Any) -> TrajectoryStep:
    return TrajectoryStep(tool=name, args=args, result="ok", accepted=accepted)


def agent_run(
    *trajectory: TrajectoryStep,
    intent: str | None = None,
    gate: str | None = Intent.QUESTION,
    searched: tuple[str, ...] = (),
    refused: int = 0,
    run_id: str = "run_1",
) -> RunRecord:
    return run(
        RunStatus.ACTED,
        run_id=run_id,
        gate=None if gate is None else GateDecision(intent=gate, act=True, reason="r"),
        intent=intent,
        trajectory=trajectory,
        sections_searched=searched,
        rejections=refused,
    )


SEARCH = tool("search_sop", query="lumper")
READ = tool("read_sop_section", section_id="escalation")
REFUSED_EMAIL = tool("send_email", accepted=False, to_contact_ids=["x@outside.example"])
HERO = tool("create_hero_task", accepted=True, title="t", description="d")


# --- helpers ----------------------------------------------------------------------------


@pytest.mark.unit
def test_a_runs_intent_is_the_agents_label_else_the_gates() -> None:
    assert run_intent(agent_run(intent="other")) == "other"
    assert run_intent(agent_run()) == Intent.QUESTION
    assert run_intent(agent_run(gate=None)) is None


@pytest.mark.unit
def test_sections_read_join_the_searched_ones_and_the_read_calls_once_each() -> None:
    runs = [
        agent_run(SEARCH, READ, searched=("driver_questions", "escalation")),
        agent_run(tool("read_sop_section", section_id="documents"), run_id="run_2"),
        agent_run(tool("read_sop_section", section_id=7), run_id="run_3"),  # not an id
    ]

    assert sections_read(runs) == ["driver_questions", "escalation", "documents"]
    assert [c.tool for c in tool_calls(runs)] == [
        "search_sop",
        "read_sop_section",
        "read_sop_section",
        "read_sop_section",
    ]


@pytest.mark.unit
def test_rejections_add_up_over_the_steps_runs() -> None:
    assert rejections([agent_run(refused=1), agent_run(refused=3, run_id="run_2")]) == 4


# --- expect.intent ------------------------------------------------------------------------


@pytest.mark.unit
def test_intent_passes_when_any_run_of_the_step_has_it() -> None:
    obs = observe(runs=[agent_run(run_id="run_1"), agent_run(intent="other", run_id="run_2")])

    assert grade(expect(intent="other"), obs) == []
    assert grade(expect(intent="question"), obs) == []


@pytest.mark.unit
def test_intent_fails_and_lists_the_intents_found() -> None:
    failures = grade(expect(intent="document_processed"), observe(runs=[agent_run()]))

    assert checks(failures) == ["expect.intent"]
    assert "expected a run with intent document_processed, got [question]" in failures[0].message
    assert "gate=question" in failures[0].message


@pytest.mark.unit
def test_intent_with_no_run_says_so() -> None:
    (failure,) = grade(expect(intent="question"), observe())

    assert "got [no intent]; runs: (none)" in failure.message


# --- tools ------------------------------------------------------------------------------


@pytest.mark.unit
def test_tools_used_is_a_subset_check() -> None:
    obs = observe(runs=[agent_run(SEARCH, READ, HERO)])

    assert grade(expect(tools_used=["search_sop", "create_hero_task"]), obs) == []
    failures = grade(expect(tools_used=["search_sop", "get_ledger", "send_sms"]), obs)
    assert checks(failures) == ["expect.tools_used"]
    assert failures[0].message == (
        "expected calls to get_ledger, send_sms; tool calls: search_sop, "
        "read_sop_section(escalation), create_hero_task"
    )


@pytest.mark.unit
def test_tools_refused_needs_a_call_a_rule_refused() -> None:
    obs = observe(runs=[agent_run(REFUSED_EMAIL, HERO)])

    assert grade(expect(tools_refused=["send_email"]), obs) == []
    failures = grade(expect(tools_refused=["create_hero_task"]), obs)
    assert checks(failures) == ["expect.tools_refused"]
    assert "send_email refused, create_hero_task" in failures[0].message


@pytest.mark.unit
def test_no_tool_call_at_all_is_listed_as_none() -> None:
    (failure,) = grade(expect(tools_used=["search_sop"]), observe(runs=[agent_run()]))

    assert failure.message.endswith("tool calls: (none)")


# --- sections ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sections_read_pass_from_a_search_or_a_read_call() -> None:
    obs = observe(runs=[agent_run(SEARCH, READ, searched=("driver_questions",))])

    assert grade(expect(sections_read=["driver_questions", "escalation"]), obs) == []


@pytest.mark.unit
def test_sections_read_fail_and_list_what_was_read() -> None:
    failures = grade(expect(sections_read=["documents"]), observe(runs=[agent_run(SEARCH, READ)]))

    assert checks(failures) == ["expect.sections_read"]
    assert "expected the agent to read documents; it read [escalation]" in failures[0].message


@pytest.mark.unit
def test_sections_read_with_nothing_read() -> None:
    (failure,) = grade(expect(sections_read=["documents"]), observe(runs=[agent_run()]))

    assert "it read [nothing]" in failure.message


# --- rejections -------------------------------------------------------------------------


@pytest.mark.unit
def test_rejections_within_the_bounds_pass() -> None:
    obs = observe(runs=[agent_run(REFUSED_EMAIL, refused=1)])

    assert grade(expect(min_rejections=1, max_rejections=1), obs) == []


@pytest.mark.unit
def test_too_few_and_too_many_rejections_fail() -> None:
    obs = observe(runs=[agent_run(REFUSED_EMAIL, refused=1)])

    low = grade(expect(min_rejections=4), obs)
    high = grade(expect(max_rejections=0), obs)

    assert checks(low) == ["expect.min_rejections"]
    assert low[0].message.startswith("expected at least 4 refused tool call(s), got 1;")
    assert checks(high) == ["expect.max_rejections"]
    assert high[0].message.startswith("expected at most 0 refused tool call(s), got 1;")


@pytest.mark.unit
def test_min_rejections_above_max_is_a_spec_error() -> None:
    with pytest.raises(ValidationError, match="min_rejections 3 is above max_rejections 1"):
        Expectation(min_rejections=3, max_rejections=1)


# --- judge ------------------------------------------------------------------------------


def judged(obs: StepObservation, result: JudgeResult | None) -> StepObservation:
    return obs.model_copy(update={"judge": result})


def verdict(score: float, *reasons: str, min_score: float = 0.7) -> JudgeResult:
    return JudgeResult(
        rubric=RUBRIC, min_score=min_score, verdict=JudgeVerdict(score=score, reasons=reasons)
    )


JUDGE_STEP = {"judge": {"rubric": RUBRIC}}


@pytest.mark.unit
def test_a_verdict_at_or_above_the_minimum_passes() -> None:
    assert grade(expect(**JUDGE_STEP), judged(observe(), verdict(0.7))) == []


@pytest.mark.unit
def test_a_verdict_below_the_minimum_fails_with_the_judges_reasons() -> None:
    failures = grade(expect(**JUDGE_STEP), judged(observe(), verdict(0.2, "rude", "no ETA")))

    assert checks(failures) == ["expect.judge"]
    assert failures[0].message == ("the judge scored 0.20, below the minimum of 0.70: rude; no ETA")


@pytest.mark.unit
def test_a_verdict_without_reasons_says_so() -> None:
    (failure,) = grade(expect(**JUDGE_STEP), judged(observe(), verdict(0.0)))

    assert failure.message.endswith(": no reasons given")


@pytest.mark.unit
def test_a_skipped_judge_is_not_a_failure() -> None:
    result = JudgeResult(rubric=RUBRIC, min_score=0.7, skipped="scripted, no verdict")

    assert grade(expect(**JUDGE_STEP), judged(observe(), result)) == []


@pytest.mark.unit
def test_a_judge_error_fails_the_check() -> None:
    result = JudgeResult(rubric=RUBRIC, min_score=0.7, error="nothing to judge")

    failures = grade(expect(**JUDGE_STEP), judged(observe(), result))

    assert [(f.check, f.message) for f in failures] == [("expect.judge", "nothing to judge")]


@pytest.mark.unit
def test_a_rubric_the_runner_never_judged_fails() -> None:
    failures = grade(expect(**JUDGE_STEP), observe())

    assert [f.message for f in failures] == ["the step has a rubric but the judge did not run"]


@pytest.mark.unit
def test_a_step_without_a_rubric_ignores_a_verdict() -> None:
    assert grade(expect(), judged(observe(), verdict(0.0))) == []


# --- failure messages describe the loop ---------------------------------------------------


@pytest.mark.unit
def test_run_descriptions_name_the_intent_tools_and_rejections() -> None:
    obs = observe(runs=[agent_run(REFUSED_EMAIL, intent="other", refused=1)])

    (failure,) = grade(expect(runs=0), obs)

    assert "intent=other" in failure.message
    assert "tools=send_email refused" in failure.message
    assert "rejections=1" in failure.message
