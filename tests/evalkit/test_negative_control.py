"""Negative control: the core suite must fail when the agent is wrong.

A suite that never fails proves nothing. Each test here plays a deliberately wrong
agent through the real core suite in scripted mode: the scenario files stay as they are,
and the wrong behaviour is a changed script (what the gate and decide models answer) for
one or two steps. The test then checks that exactly the scenarios that agent breaks fail,
with the checks that describe its mistake, and that every other scenario still passes.

The last test shows the other kind of failure: a harness that cannot replay a model
(no cassette) stops the run with an error that says how to fix it.
"""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from freight_agent.adapters.llm.cassette import CassetteModel
from freight_agent.adapters.sop.files import FileSopRepository
from freight_agent.domain.actions import (
    CloseObligation,
    CloseStatus,
    Plan,
    Purpose,
    ScheduleTimer,
    SendEmail,
    SendSms,
)
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.obligations import ObligationKind
from freight_agent.evalkit.metadata import RunMetadata
from freight_agent.evalkit.spec import Scenario, ScriptedLlm
from freight_agent.evalkit.suite import (
    ExitCode,
    ScenarioStatus,
    SuiteResult,
    SuiteRunner,
    discover,
    select,
)
from freight_agent.ports.clock import Clock
from freight_agent.ports.llm import LanguageModel

ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "evals"
SOPS = FileSopRepository(ROOT / "sops")
DRIVER = "drv_20931"
DISPATCHER = "dsp_4410"

Mutation = Callable[[Scenario], Scenario]


def rescript(
    step: int,
    *,
    gate: tuple[GateDecision, ...] | None = None,
    decide: tuple[Plan, ...] | None = None,
) -> Mutation:
    """What the wrong agent's models answer at one step; the rest of the scenario is kept."""

    def mutate(scenario: Scenario) -> Scenario:
        steps = list(scenario.steps)
        old = steps[step].script
        script = ScriptedLlm(
            gate=old.gate if gate is None else gate,
            decide=old.decide if decide is None else decide,
        )
        steps[step] = steps[step].model_copy(update={"script": script})
        return scenario.model_copy(update={"steps": tuple(steps)})

    return mutate


def metadata() -> RunMetadata:
    return RunMetadata(created_at=datetime(2026, 10, 19, tzinfo=UTC), mode="scripted")


async def run_core(mutations: Mapping[str, Mutation]) -> SuiteResult:
    scenarios = discover(EVALS, "core")
    assert set(mutations) <= {s.id for s in scenarios}
    played = [mutations.get(s.id, lambda s: s)(s) for s in scenarios]
    runner = SuiteRunner(evals_dir=EVALS, sop=SOPS, mode="scripted")
    return await runner.run(played, suite="core", metadata=metadata())


def failed(result: SuiteResult) -> dict[str, set[tuple[int | None, str]]]:
    """Failing scenarios and their (step, check) pairs."""
    return {
        s.scenario_id: {(f.step_index, f.check) for t in s.trials for f in t.failures}
        for s in result.scenarios
        if not s.passed
    }


def sms(contact: str, text: str, purpose: Purpose) -> SendSms:
    return SendSms(contact_id=contact, text=text, purpose=purpose)


@pytest.mark.unit
async def test_the_unchanged_core_suite_passes() -> None:
    result = await run_core({})

    assert failed(result) == {}
    assert result.exit_code is ExitCode.PASSED


@pytest.mark.unit
async def test_an_agent_that_answers_ok_with_an_sms_fails_b1_and_b2() -> None:
    chatty_gate = (GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=True, reason="be polite"),)
    reply = (Plan(actions=(sms(DRIVER, "Thanks, have a safe trip!", Purpose.REPLY),)),)

    result = await run_core(
        {
            "b_ack_with_nothing_open": rescript(0, gate=chatty_gate, decide=reply),
            "b_ack_after_eta_request": rescript(1, gate=chatty_gate, decide=reply),
        }
    )

    failures = failed(result)
    assert set(failures) == {"b_ack_with_nothing_open", "b_ack_after_eta_request"}
    assert {
        (0, "expect.no_actions"),
        (0, "expect.run_status"),
        (0, "expect.llm_calls"),
    } <= failures["b_ack_with_nothing_open"]
    assert {(1, "expect.no_actions"), (1, "expect.run_status")} <= failures[
        "b_ack_after_eta_request"
    ]
    assert result.exit_code is ExitCode.FAILED


@pytest.mark.unit
async def test_an_agent_that_asks_for_the_eta_a_third_time_at_0801_fails_a() -> None:
    third_request = Plan(
        actions=(
            sms(DRIVER, "Checking again: what is your ETA to the pickup?", Purpose.ETA_REQUEST),
            ScheduleTimer(kind=ObligationKind.ETA_REQUEST, due_in_minutes=30),
        )
    )

    result = await run_core({"a_client_a_high_risk": rescript(3, decide=(third_request,))})

    failures = failed(result)
    assert set(failures) == {"a_client_a_high_risk"}
    # The validator's attempt cap drops the third SMS, so no SMS leaves; the scenario
    # still fails because the SOP's last step (email the dispatcher, then a Hero task,
    # then close the request) never happened.
    assert {
        (3, "expect.actions[0]"),
        (3, "expect.actions[1]"),
        (3, "expect.obligations.eta_request.status"),
    } <= failures["a_client_a_high_risk"]


@pytest.mark.unit
async def test_an_agent_that_emails_the_dispatcher_for_client_b_fails_d1() -> None:
    email_dispatcher = Plan(
        actions=(
            SendEmail(
                to_contact_ids=(DISPATCHER,),
                subject="Load 481207: driver ETA needed",
                body="Your driver does not answer. Please send the ETA.",
                purpose=Purpose.ESCALATION,
            ),
            CloseObligation(kind=ObligationKind.ETA_REQUEST, status=CloseStatus.EXHAUSTED),
        )
    )

    result = await run_core({"d1_keystone_high_risk": rescript(1, decide=(email_dispatcher,))})

    failures = failed(result)
    assert set(failures) == {"d1_keystone_high_risk"}
    # No broker Slack alert, and an email to the dispatcher that Client B forbids.
    assert {(1, "expect.actions[0]"), (1, "forbid[1]")} <= failures["d1_keystone_high_risk"]


@pytest.mark.unit
async def test_an_agent_that_ignores_the_deferral_fails_g1() -> None:
    # The gate takes "I'll tell you in 5 minutes" for a plain acknowledgement, so the old
    # 07:31 follow-up is not moved: it fires and asks for the ETA again right away.
    shrug = (GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=False, reason="nothing to do"),)
    ask_again = Plan(
        actions=(
            sms(DRIVER, "What time will you arrive at the pickup?", Purpose.ETA_REQUEST),
            ScheduleTimer(kind=ObligationKind.ETA_REQUEST, due_in_minutes=30),
        )
    )

    result = await run_core(
        {"g1_deferral_stale_timer": rescript(1, gate=shrug, decide=(ask_again,))}
    )

    failures = failed(result)
    assert set(failures) == {"g1_deferral_stale_timer"}
    assert {
        (1, "expect.no_actions"),
        (1, "expect.obligations.eta_request.deferred"),
        (1, "expect.timers.due_at"),
        (1, "expect.run_status"),
    } <= failures["g1_deferral_stale_timer"]


@pytest.mark.unit
async def test_a_missing_cassette_in_replay_fails_the_run_with_a_clear_message(
    tmp_path: Path,
) -> None:
    def replay(clock: Clock) -> LanguageModel:
        return CassetteModel(tmp_path / "cassettes", "test/never-recorded", "replay")

    runner = SuiteRunner(evals_dir=EVALS, sop=SOPS, mode="replay", model_factory=replay)
    scenarios = select(discover(EVALS, "core"), ids=["b_ack_with_nothing_open"])

    result = await runner.run(scenarios, suite="core", metadata=metadata())

    (summary,) = result.scenarios
    assert summary.status is ScenarioStatus.ERROR
    error = summary.trials[0].error or ""
    assert error.startswith("step 0: CassetteMissing: no cassette for the gate call of model")
    assert "test/never-recorded" in error
    assert str(tmp_path / "cassettes" / "test-never-recorded") in error
    assert "record it" in error
    assert result.exit_code is ExitCode.HARNESS_ERROR
