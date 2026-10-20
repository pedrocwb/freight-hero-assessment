"""The LLM judge grades a step's texts against a rubric, on the `judge` role.

The judge model is a `ScriptedModel` here: the tests pin the request (role, prompt, no
tools), which texts are shown, and how a model failure or an empty step comes back.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.domain.outbox import OutboxItem, OutboxKind
from freight_agent.evalkit.judge import (
    JUDGE_SYSTEM_PROMPT,
    Judge,
    JudgeResult,
    JudgeSpec,
    JudgeVerdict,
    judged_texts,
    render_judge_prompt,
    skipped,
)
from freight_agent.ports.llm import LlmError, ModelRole

T0 = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
RUBRIC = "The SMS is polite and asks for the ETA to the pickup."


def item(n: int, kind: OutboxKind, **payload: Any) -> OutboxItem:
    return OutboxItem(
        outbox_id=f"out_{n}",
        idempotency_key=f"ev:{n}",
        run_id="run_1",
        load_id="481207",
        kind=kind,
        payload=payload,
        created_at=T0,
    )


SMS = item(1, OutboxKind.SMS, contact_id="drv_20931", text="Hi, what is your ETA?", purpose="x")
EMAIL = item(
    2,
    OutboxKind.EMAIL,
    to_contact_ids=["dsp_4410"],
    subject="Load 481207",
    body="Please send\n the ETA.",
)
HERO = item(3, OutboxKind.HERO_TASK, title="Call the driver", description="No answer", urgent=True)
STAGE = item(4, OutboxKind.STAGE_UPDATE, stage="at_pickup")
NOTE = item(5, OutboxKind.TMS_NOTE, text="ETA asked at 07:01")


@pytest.mark.unit
def test_every_text_is_shown_with_its_kind_and_recipient_and_stage_updates_are_not() -> None:
    texts = judged_texts([SMS, EMAIL, HERO, STAGE, NOTE])

    assert texts == [
        "[sms to drv_20931] text: Hi, what is your ETA?",
        "[email to dsp_4410] subject: Load 481207 | body: Please send the ETA.",
        "[hero_task (urgent)] title: Call the driver | description: No answer",
        "[tms_note] text: ETA asked at 07:01",
    ]


@pytest.mark.unit
def test_the_prompt_holds_the_rubric_and_numbered_texts() -> None:
    prompt = render_judge_prompt(f"  {RUBRIC}\n", ["one", "two"])

    assert prompt.startswith(f"## Rubric\n\n{RUBRIC}\n\n## Texts the agent created")
    assert "(2)\n\n1. one\n2. two\n" in prompt


@pytest.mark.unit
async def test_the_judge_calls_the_judge_role_without_tools_and_returns_the_verdict() -> None:
    model = ScriptedModel(model_name="judge/model")
    verdict = JudgeVerdict(reasons=("polite", "asks for the ETA"), score=0.9)
    model.push(ModelRole.JUDGE, verdict)

    result = await Judge(model, timeout_s=12).grade(JudgeSpec(rubric=RUBRIC), [SMS])

    (call,) = model.calls
    assert (call.role, call.schema) == (ModelRole.JUDGE, "JudgeVerdict")
    assert (call.request.system, call.request.tools, call.request.max_tool_rounds) == (
        JUDGE_SYSTEM_PROMPT,
        (),
        0,
    )
    assert call.request.timeout_s == 12
    assert RUBRIC in call.request.user and "Hi, what is your ETA?" in call.request.user
    assert result.verdict == verdict
    assert result.usage is not None and result.usage.role == "judge"
    assert (result.min_score, result.passed) == (0.7, True)


@pytest.mark.unit
async def test_a_step_with_no_text_is_not_sent_to_the_model_and_fails() -> None:
    model = ScriptedModel()

    result = await Judge(model).grade(JudgeSpec(rubric=RUBRIC), [STAGE])

    assert model.calls == []
    assert result.error == "nothing to judge: the step created no text"
    assert result.passed is False


@pytest.mark.unit
async def test_a_model_failure_is_an_error_result_not_an_exception() -> None:
    model = ScriptedModel()
    model.push(ModelRole.JUDGE, LlmError("timed out"))

    result = await Judge(model).grade(JudgeSpec(rubric=RUBRIC, min_score=0.5), [SMS])

    assert result.error == "the judge model failed: LlmError: timed out"
    assert (result.verdict, result.passed) == (None, False)


@pytest.mark.unit
async def test_other_errors_propagate_so_a_broken_harness_is_loud() -> None:
    model = ScriptedModel()
    model.push(ModelRole.JUDGE, RuntimeError("no cassette"))

    with pytest.raises(RuntimeError, match="no cassette"):
        await Judge(model).grade(JudgeSpec(rubric=RUBRIC), [SMS])


@pytest.mark.unit
def test_a_verdict_passes_at_the_minimum_and_a_skipped_one_always_passes() -> None:
    def verdict(score: float) -> JudgeResult:
        return JudgeResult(
            rubric=RUBRIC, min_score=0.6, verdict=JudgeVerdict(score=score, reasons=())
        )

    assert verdict(0.6).passed and not verdict(0.5).passed
    assert skipped(JudgeSpec(rubric=RUBRIC), "no verdict").passed


@pytest.mark.unit
@pytest.mark.parametrize(
    "data",
    [{"rubric": ""}, {"rubric": "x", "min_score": 1.5}, {"rubric": "x", "min_score": -0.1}],
)
def test_a_rubric_must_be_written_and_the_minimum_is_between_0_and_1(data: Any) -> None:
    with pytest.raises(ValidationError):
        JudgeSpec.model_validate(data)


@pytest.mark.unit
def test_a_score_outside_0_to_1_is_refused() -> None:
    with pytest.raises(ValidationError):
        JudgeVerdict(score=2.0)
