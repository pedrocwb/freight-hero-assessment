"""The transcript `agent play` prints, from fixed observations: scenario A played on the
real trial runtime with a scripted `FakeModel` (no network), and hand-made records for
the cases a right agent never shows (a refused call, a deferral, a skipped run)."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import click
import pytest

from freight_agent.domain.actions import Plan
from freight_agent.domain.intents import GateDecision, GateScores
from freight_agent.domain.load import Channel, DelayRisk
from freight_agent.domain.obligations import Obligation
from freight_agent.domain.outbox import OutboxItem, OutboxKind
from freight_agent.domain.runs import RunRecord, RunStatus, TrajectoryStep
from freight_agent.domain.timers import Timer
from freight_agent.evalkit.experiment import CostMeter, ScenarioOutcome, ScenarioTarget
from freight_agent.evalkit.spec import AttachmentSpec, MessageStep, PingStep, Step
from freight_agent.evalkit.transcript import (
    Experiment,
    PlayResult,
    describe_input,
    describe_item,
    did_nothing,
    explain_run,
    ledger_changes,
    left_open,
    obligation_name,
    play_json,
    refusal_reason,
    render_play,
    render_summary,
)
from freight_agent.interfaces.llm import ModelRole
from tests.evalkit.test_player import EVALS, SOPS, real, scenario_a_agent
from tests.fakes import FakeModel
from tests.prompts import PROMPTS

pytestmark = pytest.mark.unit

LA = ZoneInfo("America/Los_Angeles")
A = real("a_client_a_high_risk")


def played(model: FakeModel) -> PlayResult:
    target = ScenarioTarget(
        {A.id: A},
        evals_dir=EVALS,
        knowledge=SOPS,
        prompts=PROMPTS,
        model_factory=lambda: model,
        meter=CostMeter(cap_usd=1.0),
    )
    outputs = asyncio.run(target({"scenario_id": A.id}))
    return PlayResult.from_outputs(A, outputs)


@pytest.fixture(scope="module")
def right() -> PlayResult:
    return played(scenario_a_agent())


def text(rendered: str) -> str:
    return click.unstyle(rendered)


def at(hh_mm: str) -> datetime:
    return datetime.fromisoformat(f"2026-10-13T{hh_mm}:00").replace(tzinfo=LA)


# --- a whole play -----------------------------------------------------------------------


def test_a_right_play_shows_what_came_in_what_the_agent_did_and_passes(
    right: PlayResult,
) -> None:
    out = text(render_play(right))
    lines = out.splitlines()

    assert lines[0] == "== a_client_a_high_risk  (core)"
    assert "07:00  in     location ping, delay risk high" in lines
    assert '07:12  in     SMS from drv_20931 (driver): "What\'s the PO number?"' in lines
    assert "07:31  in     clock moves to 07:31" in lines
    assert '       did    SMS to drv_20931 (driver): "The PO number is 55821."' in lines
    assert "              ETA request opened, attempt 1" in lines
    assert "              follow-up timer for 07:31" in lines
    assert "              ETA request: attempt 2" in lines
    assert '              Hero task (not urgent): "Call the driver"' in lines
    assert "              ETA request closed: exhausted" in lines
    assert "       open   ETA request open, 1 attempt · next follow-up 07:31" in lines
    assert "       open   nothing" in lines
    assert any(x.startswith("       why    intent question · followed") for x in lines)
    assert out.count("checks ✓ all") == 4
    assert "expect.actions[0]" not in out  # compact: passing checks are not listed
    assert "note" not in out
    assert lines[-1] == "PASSED  a_client_a_high_risk  4/4 steps passed  $0.0000"
    assert right.passed and right.steps_passed == 4


def test_verbose_lists_every_check_and_the_notes(right: PlayResult) -> None:
    out = text(render_play(right, verbose=True))

    assert "       note   A1: the risk goes high" in out
    assert "                ✓ expect.llm_calls" in out


def test_long_messages_wrap_under_their_column(right: PlayResult) -> None:
    lines = text(render_play(right, width=60)).splitlines()

    email = next(i for i, x in enumerate(lines) if "email to dsp_4410" in x)
    assert all(len(x) <= 60 for x in lines[email : email + 3])
    assert lines[email + 1].startswith(" " * 14) and lines[email + 1].strip()


def test_a_failing_step_shows_its_failed_checks_and_the_play_fails() -> None:
    model = FakeModel()
    model.push(ModelRole.AGENT, Plan(rationale="nothing to do"))  # step 0: no SMS

    result = played(model)
    out = text(render_play(result, k=2))

    assert out.splitlines()[0] == "== a_client_a_high_risk  (core)  (run 1 of 2)"
    assert "       did    nothing" in out
    assert "checks ✗ 4 of 7 failed" in out
    assert "  ✗ expect.actions[0]: expected exactly 1 sms to drv_20931," in out
    assert "✓ forbid[0]" not in out
    assert not result.passed and result.steps_passed == 0
    assert out.splitlines()[-1].startswith("FAILED  a_client_a_high_risk  0/4 steps passed")


def test_a_play_that_stopped_early_says_so_and_shows_the_error() -> None:
    model = FakeModel()  # nothing queued: the first run escalates, the gate later fails

    result = played(model)
    out = text(render_play(result))

    assert not result.passed
    assert out.count("not played") == len(A.steps) - len(result.observations)
    if result.error:
        assert "✗ error:" in out


def test_json_carries_each_step(right: PlayResult) -> None:
    data = play_json(right)

    assert (data["passed"], data["steps_passed"], data["error"]) == (True, 4, None)
    step = data["steps"][1]
    assert step["input"] == 'SMS from drv_20931 (driver): "What\'s the PO number?"'
    assert step["actions"] == ['SMS to drv_20931 (driver): "The PO number is 55821."']
    assert step["runs"][0]["intent"] == "question"
    assert all(c["passed"] for c in step["checks"])


# --- the pieces -------------------------------------------------------------------------


def test_inputs_read_as_plain_words(right: PlayResult) -> None:
    snapshot_load = right.observations[0].snapshot.load
    pod = MessageStep(
        channel=Channel.EMAIL,
        sender=None,
        body="POD attached",
        attachments=(AttachmentSpec(filename="pod.pdf", fixture="x.pdf"),),
    )

    assert describe_input(Step(at="09:00", message=pod), snapshot_load) == (
        'email from an unknown sender, with pod.pdf: "POD attached"'
    )
    ping = Step(at="09:00", ping=PingStep(delay_risk=DelayRisk.MEDIUM))
    assert describe_input(ping, snapshot_load) == "location ping, delay risk medium"


def item(kind: OutboxKind, **payload: Any) -> OutboxItem:
    return OutboxItem(
        outbox_id="out_1",
        idempotency_key="e:0",
        run_id="run_1",
        load_id="481207",
        kind=kind,
        payload=payload,
        created_at=datetime(2026, 10, 13, tzinfo=UTC),
    )


def test_other_actions_take_one_line(right: PlayResult) -> None:
    load = right.observations[0].snapshot.load

    hero = item(OutboxKind.HERO_TASK, title="Call the driver", urgent=True)
    assert describe_item(hero, load) == 'Hero task (urgent): "Call the driver"'
    stage = describe_item(item(OutboxKind.STAGE_UPDATE, stage="delivered"), load)
    assert stage == "stage changed to delivered"
    assert describe_item(item(OutboxKind.TMS_NOTE, text="ETA 09:40"), load) == (
        'TMS note: "ETA 09:40"'
    )
    assert describe_item(item(OutboxKind.BROKER_SLACK, text="late"), load) == (
        'Slack to the broker: "late"'
    )


def run(**fields: Any) -> RunRecord:
    base: dict[str, Any] = {
        "run_id": "run_1",
        "event_id": "evt_1",
        "load_id": "481207",
        "started_at": datetime(2026, 10, 13, tzinfo=UTC),
        "status": RunStatus.ACTED,
    }
    return RunRecord.model_validate({**base, **fields})


def test_why_names_the_intent_the_act_probability_the_sections_and_the_refusals() -> None:
    refused = TrajectoryStep(
        tool="send_sms",
        args={},
        result="rejected by attempt_cap: 2 of 2 ETA requests used",
        accepted=False,
    )
    record = run(
        gate=GateDecision(intent="question", act=True, reason="asks"),
        gate_scores=GateScores(model="jev", intents={"question": 0.9}, act=0.87, threshold=0.2),
        sop_section_ids=("driver_questions",),
        trajectory=(refused,),
    )

    assert explain_run(record) == (
        "intent question (act 87%) · followed driver_questions · "
        "send_sms refused: attempt_cap: 2 of 2 ETA requests used"
    )
    assert explain_run(run()) == "no intent"


def test_refusal_reasons_drop_the_prefix() -> None:
    assert (
        refusal_reason("rejected by sms_rate: 3 in the last hour") == "sms_rate: 3 in the last hour"
    )
    assert refusal_reason("rejected: invalid arguments") == "rejected: invalid arguments"


def test_runs_that_did_nothing_say_why() -> None:
    assert did_nothing(run(status=RunStatus.NOOP)) == "nothing"
    skipped = run(status=RunStatus.SKIPPED, skip_reason="duplicate")
    assert did_nothing(skipped) == "nothing (skipped: duplicate)"
    assert "the run failed (boom)" in did_nothing(run(status=RunStatus.FAILED, error="boom"))


def test_ledger_changes_show_a_deferral_and_what_is_left_open(right: PlayResult) -> None:
    before = right.observations[0].snapshot
    (open_one,) = before.obligations
    deferred: Obligation = open_one.model_copy(
        update={"deferrals_used": 1, "deferred_until": at("07:20")}
    )
    timer = Timer(
        timer_id="tmr_9",
        load_id="481207",
        obligation_id=open_one.obligation_id,
        due_at=at("07:22"),
        created_at=at("07:05"),
    )
    after = before.model_copy(
        update={"obligations": (deferred,), "timers": (*before.timers, timer)}
    )

    assert ledger_changes(before, after, LA) == [
        "ETA request deferred until 07:20",
        "follow-up timer for 07:22",
    ]
    assert left_open(after, LA) == (
        "ETA request open, 1 attempt · next follow-up 07:22 (2 pending)"
    )


def test_obligation_names_read_as_words() -> None:
    assert obligation_name("eta_request") == "ETA request"
    assert obligation_name("pod_follow_up") == "POD follow up"


# --- the summary ------------------------------------------------------------------------


def test_the_summary_counts_scenarios_pass_hat_k_cost_and_links() -> None:
    b = real("b_ack_with_nothing_open")
    found = [
        ScenarioOutcome(scenario=A, passed=(True, True), failures=((), ()), cost_usd=0.004),
        ScenarioOutcome(scenario=b, passed=(True, False), failures=((), ()), cost_usd=0.001),
    ]

    out = text(
        render_summary(
            found, k=2, cost_usd=0.005, experiments=[Experiment("core", "core-k2-x", "https://ls")]
        )
    )

    assert out.splitlines() == [
        "2 scenarios: 1 passed, 1 failed",
        "pass^2: 1/2 (a scenario passes when all 2 runs pass)",
        "cost: $0.0050",
        "LangSmith experiment core-k2-x: https://ls",
    ]


def test_the_summary_names_what_the_cost_cap_stopped() -> None:
    found = [ScenarioOutcome(scenario=A, passed=(), failures=(), not_run=1)]

    out = text(render_summary(found, k=1, cost_usd=0.0))

    assert "1 scenario: 0 passed, 0 failed" in out
    assert "not run (cost cap): a_client_a_high_risk" in out
