"""Graders judge one step at a time, plus scenario-wide invariants; `grade` runs them by
group (one LangSmith evaluator each) and `step_checks` lists every declared check of a
step, passed or failed, for the platform.

Every check has a positive case and a negative one: a grader that never fails
proves nothing, so each test that expects a failure also pins its check name.
"""

import itertools
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from freight_agent.application.contracts import LoadSnapshot
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.load import (
    Appointment,
    AppointmentKind,
    Channel,
    Contact,
    DelayRisk,
    Load,
    Role,
    Stage,
    Stop,
    StopType,
)
from freight_agent.domain.obligations import Obligation, ObligationKind, ObligationStatus
from freight_agent.domain.outbox import OutboxItem, OutboxKind
from freight_agent.domain.runs import RunRecord, RunStatus, Usage
from freight_agent.domain.timers import Timer, TimerStatus
from freight_agent.domain.validation import Violation
from freight_agent.evalkit.graders import (
    GROUPS,
    CheckGroup,
    StepCheck,
    check_invariants,
    declared_checks,
    grade_step,
    step_checks,
)
from freight_agent.evalkit.graders import grade as grade_scenario
from freight_agent.evalkit.results import Failure, StepObservation
from freight_agent.evalkit.spec import Scenario, Step

LA = ZoneInfo("America/Los_Angeles")
TZ = "America/Los_Angeles"
T0 = datetime(2026, 10, 13, 7, 0, tzinfo=LA)
DRIVER, DISPATCHER, BROKER = "drv_20931", "dsp_4410", "brk_0087"

LOAD = Load(
    load_id="481207",
    broker_id="northline",
    broker_name="Northline Logistics",
    shipper="Riverbend Foods",
    carrier_name="Prairie Haul LLC",
    mc_number="778120",
    commodity="Canned tomato sauce",
    stage=Stage.TO_PICKUP,
    delay_risk=DelayRisk.LOW,
    contacts=(
        Contact(id=DRIVER, role=Role.DRIVER, channels=(Channel.SMS,)),
        Contact(id=DISPATCHER, role=Role.DISPATCHER, channels=(Channel.EMAIL,)),
        Contact(id=BROKER, role=Role.BROKER, channels=(Channel.EMAIL, Channel.SLACK)),
    ),
    stops=(
        Stop(
            sequence=1,
            type=StopType.PICKUP,
            name="Riverbend plant",
            address="Stockton, CA",
            timezone=TZ,
            appointment=Appointment(kind=AppointmentKind.FIXED, at=T0),
        ),
    ),
)

_ids = itertools.count(1)


# --- builders -------------------------------------------------------------------------


def item(kind: OutboxKind, key: str | None = None, **payload: Any) -> OutboxItem:
    n = next(_ids)
    return OutboxItem(
        outbox_id=f"out_{n}",
        idempotency_key=key or f"ev_{n}:0",
        run_id="run_1",
        load_id=LOAD.load_id,
        kind=kind,
        payload=payload,
        created_at=T0,
    )


def sms(text: str = "Hi, what is your ETA to Riverbend?", to: str | None = DRIVER) -> OutboxItem:
    if to is None:
        return item(OutboxKind.SMS, text=text, purpose="eta_request")
    return item(OutboxKind.SMS, contact_id=to, text=text, purpose="eta_request")


def email(
    body: str = "Please share the driver's ETA.",
    subject: str = "Load 481207",
    to: Sequence[str] = (DISPATCHER,),
) -> OutboxItem:
    return item(
        OutboxKind.EMAIL, to_contact_ids=list(to), subject=subject, body=body, purpose="eta"
    )


def hero(title: str = "Call the driver", description: str = "", *, urgent: bool) -> OutboxItem:
    return item(OutboxKind.HERO_TASK, title=title, description=description, urgent=urgent)


def obligation(
    obligation_id: str = "obl_1",
    *,
    status: ObligationStatus = ObligationStatus.OPEN,
    attempts: int = 1,
    deferrals_used: int = 0,
    opened_at: datetime = T0,
) -> Obligation:
    return Obligation(
        obligation_id=obligation_id,
        load_id=LOAD.load_id,
        kind=ObligationKind.ETA_REQUEST,
        status=status,
        attempts=attempts,
        deferrals_used=deferrals_used,
        opened_at=opened_at,
    )


def timer(timer_id: str, due_at: datetime, status: TimerStatus = TimerStatus.PENDING) -> Timer:
    return Timer(
        timer_id=timer_id,
        load_id=LOAD.load_id,
        obligation_id="obl_1",
        due_at=due_at,
        created_at=T0,
        status=status,
    )


def run(
    status: RunStatus,
    *,
    run_id: str = "run_1",
    usage: Sequence[Usage] = (),
    **fields: Any,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        event_id=f"ev_{run_id}",
        load_id=LOAD.load_id,
        started_at=T0,
        status=status,
        usage=tuple(usage),
        **fields,
    )


def call(
    role: str = "gate",
    *,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost: float = 0.0,
    latency_ms: int = 0,
) -> Usage:
    return Usage(
        role=role,
        model="m",
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        cost_usd=cost,
        latency_ms=latency_ms,
    )


def observe(
    *outbox: OutboxItem,
    runs: Sequence[RunRecord] = (),
    llm_calls: int = 0,
    obligations: Sequence[Obligation] = (),
    timers: Sequence[Timer] = (),
    step_index: int = 0,
) -> StepObservation:
    return StepObservation(
        step_index=step_index,
        at=T0,
        outbox=outbox,
        runs=tuple(runs),
        llm_calls=llm_calls,
        snapshot=LoadSnapshot(
            load=LOAD,
            obligations=tuple(obligations),
            timers=tuple(timers),
            events=(),
            outbox=outbox,
            runs=tuple(runs),
        ),
    )


def step(**spec: Any) -> Step:
    """Steps are written like the scenario YAML, so the tests read like scenarios."""
    return Step.model_validate({"at": "07:01", **spec})


def expect(**expectation: Any) -> Step:
    return step(expect=expectation)


def grade(spec: Step, obs: StepObservation) -> list[Failure]:
    return grade_step(spec, obs, timezone=TZ)


def checks(failures: Sequence[Failure]) -> list[str]:
    return [f.check for f in failures]


# --- expect.actions -------------------------------------------------------------------


@pytest.mark.unit
def test_an_empty_expectation_passes_whatever_happened() -> None:
    assert grade(step(), observe(sms(), hero(urgent=True))) == []


@pytest.mark.unit
def test_action_matches_by_kind() -> None:
    spec = expect(actions=[{"kind": "sms"}])

    assert grade(spec, observe(sms())) == []
    assert checks(grade(spec, observe(hero(urgent=False)))) == ["expect.actions[0]"]


@pytest.mark.unit
def test_action_to_matches_the_sms_contact() -> None:
    spec = expect(actions=[{"kind": "sms", "to": DRIVER}])

    assert grade(spec, observe(sms(to=DRIVER))) == []
    assert checks(grade(spec, observe(sms(to=DISPATCHER)))) == ["expect.actions[0]"]


@pytest.mark.unit
def test_action_to_matches_membership_in_the_email_recipients() -> None:
    spec = expect(actions=[{"kind": "email", "to": BROKER}])

    assert grade(spec, observe(email(to=(DISPATCHER, BROKER)))) == []
    assert checks(grade(spec, observe(email(to=(DISPATCHER,))))) == ["expect.actions[0]"]


@pytest.mark.unit
def test_action_to_never_matches_a_kind_without_recipients() -> None:
    spec = expect(actions=[{"kind": "hero_task", "to": DRIVER}])

    assert checks(grade(spec, observe(hero(urgent=True)))) == ["expect.actions[0]"]


@pytest.mark.unit
def test_action_contains_needs_every_substring_case_insensitively() -> None:
    spec = expect(actions=[{"kind": "sms", "contains": ["eta", "RIVERBEND"]}])

    assert grade(spec, observe(sms("What is your ETA to Riverbend?"))) == []
    assert checks(grade(spec, observe(sms("What is your ETA?")))) == ["expect.actions[0]"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "outbox",
    [
        item(OutboxKind.TMS_NOTE, text="needle"),
        item(OutboxKind.TMS_NOTE, body="needle"),
        item(OutboxKind.TMS_NOTE, subject="needle"),
        item(OutboxKind.TMS_NOTE, title="needle"),
        item(OutboxKind.TMS_NOTE, description="needle"),
    ],
    ids=["text", "body", "subject", "title", "description"],
)
def test_action_text_checks_look_at_every_text_field(outbox: OutboxItem) -> None:
    spec = expect(actions=[{"kind": "tms_note", "contains": ["NEEDLE"], "regex": "ne+dle"}])

    assert grade(spec, observe(outbox)) == []


@pytest.mark.unit
def test_action_contains_may_span_fields_of_one_item() -> None:
    spec = expect(actions=[{"kind": "email", "contains": ["load 481207", "eta"]}])

    assert grade(spec, observe(email(subject="Load 481207", body="Driver ETA?"))) == []


@pytest.mark.unit
def test_action_text_checks_ignore_non_text_payload_values() -> None:
    spec = expect(actions=[{"kind": "stage_update", "contains": ["to_delivery"]}])

    assert checks(grade(spec, observe(item(OutboxKind.STAGE_UPDATE, stage="to_delivery")))) == [
        "expect.actions[0]"
    ]


@pytest.mark.unit
def test_action_regex_searches_case_insensitively() -> None:
    spec = expect(actions=[{"kind": "sms", "regex": r"\beta\b.*riverbend"}])

    assert grade(spec, observe(sms("Your ETA to RIVERBEND?"))) == []
    assert checks(grade(spec, observe(sms("Your ETA please")))) == ["expect.actions[0]"]


@pytest.mark.unit
def test_an_invalid_regex_is_rejected_when_the_scenario_loads() -> None:
    with pytest.raises(ValidationError, match="invalid regex '\\('"):
        expect(actions=[{"kind": "sms", "regex": "("}])


@pytest.mark.unit
def test_action_urgent_compares_the_payload_flag() -> None:
    urgent = expect(actions=[{"kind": "hero_task", "urgent": True}])
    calm = expect(actions=[{"kind": "hero_task", "urgent": False}])

    assert grade(urgent, observe(hero(urgent=True))) == []
    assert grade(calm, observe(hero(urgent=False))) == []
    assert checks(grade(urgent, observe(hero(urgent=False)))) == ["expect.actions[0]"]
    assert checks(grade(calm, observe(hero(urgent=True)))) == ["expect.actions[0]"]


@pytest.mark.unit
def test_action_urgent_false_does_not_match_a_missing_flag() -> None:
    spec = expect(actions=[{"kind": "hero_task", "urgent": False}])

    assert checks(grade(spec, observe(item(OutboxKind.HERO_TASK, title="t")))) == [
        "expect.actions[0]"
    ]


@pytest.mark.unit
def test_action_without_count_needs_at_least_one_match() -> None:
    spec = expect(actions=[{"kind": "sms"}])

    assert grade(spec, observe(sms(), sms())) == []
    failures = grade(spec, observe())
    assert checks(failures) == ["expect.actions[0]"]
    assert "expected at least 1 sms, found 0" in failures[0].message


@pytest.mark.unit
def test_action_count_is_exact() -> None:
    spec = expect(actions=[{"kind": "sms", "count": 2}])

    assert grade(spec, observe(sms(), sms())) == []
    assert checks(grade(spec, observe(sms()))) == ["expect.actions[0]"]
    failures = grade(spec, observe(sms(), sms(), sms()))
    assert checks(failures) == ["expect.actions[0]"]
    assert "expected exactly 2 sms, found 3" in failures[0].message


@pytest.mark.unit
def test_action_count_zero_means_none_may_match() -> None:
    spec = expect(actions=[{"kind": "sms", "to": DISPATCHER, "count": 0}])

    assert grade(spec, observe(sms(to=DRIVER))) == []
    assert checks(grade(spec, observe(sms(to=DISPATCHER)))) == ["expect.actions[0]"]


@pytest.mark.unit
def test_each_action_matcher_is_checked_on_its_own_index() -> None:
    spec = expect(actions=[{"kind": "sms"}, {"kind": "hero_task"}, {"kind": "email"}])

    assert checks(grade(spec, observe(sms()))) == ["expect.actions[1]", "expect.actions[2]"]


@pytest.mark.unit
def test_action_failure_states_the_expectation_and_lists_the_step_actions() -> None:
    spec = expect(
        actions=[
            {"kind": "sms", "to": DRIVER, "contains": ["ETA"], "regex": "eta", "urgent": False}
        ]
    )
    long_text = "x" * 100
    outbox = (
        sms(long_text, to=DISPATCHER),
        email(subject="Load 481207", body="hello", to=(DISPATCHER, BROKER)),
        hero(title="Call", description="the driver", urgent=True),
        item(OutboxKind.STAGE_UPDATE, stage="to_delivery"),
    )

    [failure] = grade(spec, observe(*outbox, step_index=3))

    assert failure.step_index == 3
    message = failure.message
    assert "expected at least 1 sms to drv_20931 containing 'ETA' matching /eta/i not urgent" in (
        message
    )
    assert f"sms to dsp_4410: '{'x' * 80}...'" in message
    assert "email to dsp_4410, brk_0087: 'hello Load 481207'" in message
    assert "hero_task (urgent): 'Call the driver'" in message
    assert "stage_update: 'stage=to_delivery'" in message


@pytest.mark.unit
def test_action_failure_says_when_the_step_had_no_actions() -> None:
    [failure] = grade(expect(actions=[{"kind": "hero_task", "urgent": True}]), observe())

    assert "expected at least 1 hero_task urgent, found 0" in failure.message
    assert "step actions: (none)" in failure.message


# --- expect.no_actions ----------------------------------------------------------------


@pytest.mark.unit
def test_no_actions_passes_on_an_empty_step() -> None:
    assert grade(expect(no_actions=True), observe()) == []


@pytest.mark.unit
def test_no_actions_fails_and_lists_what_was_sent() -> None:
    [failure] = grade(expect(no_actions=True), observe(sms("ok thanks")))

    assert failure.check == "expect.no_actions"
    assert "expected no actions, found 1" in failure.message
    assert "sms to drv_20931: 'ok thanks'" in failure.message


@pytest.mark.unit
def test_no_actions_false_does_not_check_anything() -> None:
    assert grade(expect(no_actions=False), observe(sms())) == []


# --- forbid ---------------------------------------------------------------------------


@pytest.mark.unit
def test_forbid_passes_when_nothing_matches() -> None:
    spec = step(forbid=[{"kind": "email", "to": DISPATCHER}, {"kind": "broker_slack"}])

    assert grade(spec, observe(sms(), email(to=(BROKER,)))) == []


@pytest.mark.unit
def test_forbid_fails_on_any_match_and_lists_the_matches() -> None:
    spec = step(forbid=[{"kind": "broker_slack"}, {"kind": "email", "to": DISPATCHER}])

    failures = grade(spec, observe(sms(), email("Driver late", to=(DISPATCHER,))))

    assert checks(failures) == ["forbid[1]"]
    assert "expected no email to dsp_4410, found 1" in failures[0].message
    assert "email to dsp_4410: 'Driver late Load 481207'" in failures[0].message
    assert "sms" not in failures[0].message


# --- expect.obligations ---------------------------------------------------------------


@pytest.mark.unit
def test_obligation_null_means_none_may_exist() -> None:
    spec = expect(obligations={"eta_request": None})

    assert grade(spec, observe()) == []
    failures = grade(spec, observe(obligations=[obligation()]))
    assert checks(failures) == ["expect.obligations.eta_request"]
    assert "expected no eta_request obligation" in failures[0].message
    assert "obl_1: status=open, attempts=1, deferrals_used=0" in failures[0].message


@pytest.mark.unit
def test_obligation_expectation_fails_when_none_exists() -> None:
    failures = grade(expect(obligations={"eta_request": {"status": "open"}}), observe())

    assert checks(failures) == ["expect.obligations.eta_request"]
    assert "expected an eta_request obligation, found none" in failures[0].message


@pytest.mark.unit
def test_obligation_status_must_be_equal() -> None:
    spec = expect(obligations={"eta_request": {"status": "satisfied"}})

    assert grade(spec, observe(obligations=[obligation(status=ObligationStatus.SATISFIED)])) == []
    failures = grade(spec, observe(obligations=[obligation()]))
    assert checks(failures) == ["expect.obligations.eta_request.status"]
    assert "expected status satisfied, got open" in failures[0].message


@pytest.mark.unit
def test_obligation_attempts_must_be_equal() -> None:
    spec = expect(obligations={"eta_request": {"attempts": 2}})

    assert grade(spec, observe(obligations=[obligation(attempts=2)])) == []
    failures = grade(spec, observe(obligations=[obligation(attempts=1)]))
    assert checks(failures) == ["expect.obligations.eta_request.attempts"]
    assert "expected attempts 2, got 1" in failures[0].message


@pytest.mark.unit
def test_obligation_deferred_true_needs_a_used_deferral() -> None:
    spec = expect(obligations={"eta_request": {"deferred": True}})

    assert grade(spec, observe(obligations=[obligation(deferrals_used=1)])) == []
    failures = grade(spec, observe(obligations=[obligation(deferrals_used=0)]))
    assert checks(failures) == ["expect.obligations.eta_request.deferred"]
    assert "expected a deferral, got deferrals_used=0" in failures[0].message


@pytest.mark.unit
def test_obligation_deferred_false_needs_no_deferral() -> None:
    spec = expect(obligations={"eta_request": {"deferred": False}})

    assert grade(spec, observe(obligations=[obligation(deferrals_used=0)])) == []
    failures = grade(spec, observe(obligations=[obligation(deferrals_used=1)]))
    assert checks(failures) == ["expect.obligations.eta_request.deferred"]
    assert "expected no deferral, got deferrals_used=1" in failures[0].message


@pytest.mark.unit
def test_obligation_fields_are_checked_independently() -> None:
    spec = expect(
        obligations={"eta_request": {"status": "exhausted", "attempts": 2, "deferred": True}}
    )

    assert checks(grade(spec, observe(obligations=[obligation()]))) == [
        "expect.obligations.eta_request.status",
        "expect.obligations.eta_request.attempts",
        "expect.obligations.eta_request.deferred",
    ]


@pytest.mark.unit
def test_obligation_check_uses_the_latest_by_opened_at() -> None:
    older = obligation("obl_9", status=ObligationStatus.CANCELLED, opened_at=T0)
    newer = obligation("obl_1", status=ObligationStatus.OPEN, opened_at=T0 + timedelta(hours=1))
    spec = expect(obligations={"eta_request": {"status": "open"}})

    assert grade(spec, observe(obligations=[newer, older])) == []


@pytest.mark.unit
def test_obligation_check_breaks_opened_at_ties_by_id() -> None:
    first = obligation("obl_1", status=ObligationStatus.CANCELLED)
    second = obligation("obl_2", status=ObligationStatus.OPEN)
    spec = expect(obligations={"eta_request": {"status": "open"}})

    assert grade(spec, observe(obligations=[second, first])) == []


# --- expect.timers --------------------------------------------------------------------


@pytest.mark.unit
def test_timers_pending_counts_only_pending_timers() -> None:
    timers = [
        timer("timer_1", T0, TimerStatus.SUPERSEDED),
        timer("timer_2", T0 + timedelta(minutes=30)),
    ]

    assert grade(expect(timers={"pending": 1}), observe(timers=timers)) == []
    failures = grade(expect(timers={"pending": 2}), observe(timers=timers))
    assert checks(failures) == ["expect.timers.pending"]
    assert "expected 2 pending timer(s), got 1" in failures[0].message
    assert "timer_1 due 07:00 superseded; timer_2 due 07:30 pending" in failures[0].message


@pytest.mark.unit
def test_timers_due_at_is_the_local_time_of_the_earliest_pending_timer() -> None:
    timers = [
        timer("timer_1", T0 + timedelta(minutes=5), TimerStatus.CANCELLED),
        timer("timer_3", datetime(2026, 10, 13, 15, 1, tzinfo=UTC)),  # 08:01 in Los Angeles
        timer("timer_2", T0 + timedelta(minutes=31)),
    ]

    assert grade(expect(timers={"due_at": "07:31"}), observe(timers=timers)) == []
    failures = grade(expect(timers={"due_at": "08:01"}), observe(timers=timers))
    assert checks(failures) == ["expect.timers.due_at"]
    assert "expected the earliest pending timer due at 08:01 (America/Los_Angeles)" in (
        failures[0].message
    )
    assert "got 07:31" in failures[0].message


@pytest.mark.unit
def test_timers_due_at_fails_when_no_timer_is_pending() -> None:
    timers = [timer("timer_1", T0, TimerStatus.FIRED)]

    failures = grade(expect(timers={"due_at": "07:31"}), observe(timers=timers))

    assert checks(failures) == ["expect.timers.due_at"]
    assert "got no pending timer" in failures[0].message
    assert "timer_1 due 07:00 fired" in failures[0].message


@pytest.mark.unit
def test_timer_failures_say_when_there_are_no_timers() -> None:
    failures = grade(expect(timers={"pending": 1, "due_at": "07:31"}), observe())

    assert checks(failures) == ["expect.timers.pending", "expect.timers.due_at"]
    assert all("timers: (none)" in f.message for f in failures)


@pytest.mark.unit
def test_timers_without_fields_check_nothing() -> None:
    assert grade(expect(timers={}), observe(timers=[timer("timer_1", T0)])) == []


# --- expect.run_status ----------------------------------------------------------------


@pytest.mark.unit
def test_run_status_compares_the_statuses_in_order() -> None:
    runs = [run(RunStatus.SKIPPED, run_id="run_1"), run(RunStatus.ACTED, run_id="run_2")]

    assert grade(expect(run_status=["skipped", "acted"]), observe(runs=runs)) == []
    failures = grade(expect(run_status=["acted", "skipped"]), observe(runs=runs))
    assert checks(failures) == ["expect.run_status"]
    assert "expected runs [acted, skipped], got [skipped, acted]" in failures[0].message


@pytest.mark.unit
def test_run_status_fails_on_a_missing_run() -> None:
    failures = grade(expect(run_status=["acted"]), observe())

    assert checks(failures) == ["expect.run_status"]
    assert "got []" in failures[0].message
    assert "runs: (none)" in failures[0].message


@pytest.mark.unit
def test_run_status_empty_checks_nothing() -> None:
    assert grade(expect(run_status=[]), observe(runs=[run(RunStatus.FAILED)])) == []


@pytest.mark.unit
def test_run_status_failure_explains_each_run() -> None:
    runs = [
        run(RunStatus.SKIPPED, run_id="run_1", skip_reason="stale_timer"),
        run(
            RunStatus.ESCALATED,
            run_id="run_2",
            gate=GateDecision(intent=Intent.QUESTION, act=True, reason="asks"),
            violations=(Violation(rule="unknown_recipient", action_index=0, message="m"),),
            error="LlmError: timeout",
        ),
    ]

    [failure] = grade(expect(run_status=["acted"]), observe(runs=runs))

    assert "run_1 on ev_run_1: skipped (skip_reason=stale_timer)" in failure.message
    assert (
        "run_2 on ev_run_2: escalated (gate=question, act=True; "
        "violations=unknown_recipient; error=LlmError: timeout)"
    ) in failure.message


# --- expect.llm_calls -----------------------------------------------------------------


@pytest.mark.unit
def test_llm_calls_is_exact() -> None:
    runs = [run(RunStatus.ACTED, usage=[call("gate"), call("agent")])]

    assert grade(expect(llm_calls=2), observe(runs=runs, llm_calls=2)) == []
    failures = grade(expect(llm_calls=1), observe(runs=runs, llm_calls=2))
    assert checks(failures) == ["expect.llm_calls"]
    assert "expected 1 model call(s), got 2" in failures[0].message
    assert "run_1 on ev_run_1: acted (calls=gate, agent)" in failures[0].message


@pytest.mark.unit
def test_llm_calls_zero_fails_on_any_call() -> None:
    assert grade(expect(llm_calls=0), observe()) == []
    assert checks(grade(expect(llm_calls=0), observe(llm_calls=1))) == ["expect.llm_calls"]


@pytest.mark.unit
def test_llm_calls_unset_checks_nothing() -> None:
    assert grade(step(), observe(llm_calls=5)) == []


# --- all checks together ----------------------------------------------------------------


@pytest.mark.unit
def test_every_failing_check_is_reported_in_spec_order() -> None:
    spec = step(
        expect={
            "actions": [{"kind": "email"}],
            "no_actions": True,
            "obligations": {"eta_request": None},
            "timers": {"pending": 0},
            "run_status": ["noop"],
            "llm_calls": 0,
        },
        forbid=[{"kind": "sms"}],
    )
    obs = observe(
        sms(),
        runs=[run(RunStatus.ACTED)],
        llm_calls=1,
        obligations=[obligation()],
        timers=[timer("timer_1", T0)],
        step_index=4,
    )

    failures = grade(spec, obs)

    assert checks(failures) == [
        "expect.actions[0]",
        "expect.no_actions",
        "forbid[0]",
        "expect.obligations.eta_request",
        "expect.timers.pending",
        "expect.run_status",
        "expect.llm_calls",
    ]
    assert {f.step_index for f in failures} == {4}


# --- invariants: idempotency ------------------------------------------------------------


@pytest.mark.unit
def test_distinct_idempotency_keys_pass() -> None:
    observations = [observe(sms(), email()), observe(hero(urgent=True), step_index=1)]

    assert check_invariants(LOAD, observations) == []


@pytest.mark.unit
def test_a_repeated_idempotency_key_fails_once_per_key() -> None:
    observations = [
        observe(sms(), item(OutboxKind.TMS_NOTE, key="msg-1:0", text="n")),
        observe(
            item(OutboxKind.TMS_NOTE, key="msg-1:0", text="n"),
            item(OutboxKind.TMS_NOTE, key="msg-1:0", text="n"),
            step_index=2,
        ),
    ]

    failures = check_invariants(LOAD, observations)

    assert checks(failures) == ["invariant.idempotency"]
    assert failures[0].step_index is None
    assert "idempotency key 'msg-1:0' is used by 3 outbox items" in failures[0].message
    assert "(step 0)" in failures[0].message
    assert "(step 2)" in failures[0].message


# --- invariants: recipients -------------------------------------------------------------


@pytest.mark.unit
def test_recipients_that_are_contacts_of_the_load_pass() -> None:
    observations = [
        observe(sms(to=DRIVER), email(to=(DISPATCHER, BROKER)), item(OutboxKind.BROKER_SLACK))
    ]

    assert check_invariants(LOAD, observations) == []


@pytest.mark.unit
def test_an_sms_to_a_stranger_fails() -> None:
    failures = check_invariants(LOAD, [observe(sms(to="drv_other"), step_index=1)])

    assert checks(failures) == ["invariant.recipients"]
    assert failures[0].step_index is None
    message = failures[0].message
    assert "step 1: sms" in message
    assert "goes to 'drv_other', who is not a contact of load 481207" in message
    assert "contacts: drv_20931, dsp_4410, brk_0087" in message


@pytest.mark.unit
def test_each_stranger_in_an_email_fails() -> None:
    failures = check_invariants(LOAD, [observe(email(to=("x@a.com", DISPATCHER, "y@b.com")))])

    assert checks(failures) == ["invariant.recipients", "invariant.recipients"]
    assert "'x@a.com'" in failures[0].message
    assert "'y@b.com'" in failures[1].message


@pytest.mark.unit
@pytest.mark.parametrize(
    "outbox",
    [sms(to=None), email(to=()), item(OutboxKind.EMAIL, subject="s", body="b")],
    ids=["sms-without-contact", "email-with-empty-list", "email-without-list"],
)
def test_a_message_without_a_recipient_fails(outbox: OutboxItem) -> None:
    failures = check_invariants(LOAD, [observe(outbox)])

    assert checks(failures) == ["invariant.recipients"]
    assert "has no recipient" in failures[0].message


@pytest.mark.unit
def test_invariants_report_every_kind_of_failure_together() -> None:
    observations = [
        observe(sms(to="stranger"), item(OutboxKind.TMS_NOTE, key="k:0", text="a")),
        observe(item(OutboxKind.TMS_NOTE, key="k:0", text="b"), step_index=1),
    ]

    assert checks(check_invariants(LOAD, observations)) == [
        "invariant.idempotency",
        "invariant.recipients",
    ]


@pytest.mark.unit
def test_cost_tokens_and_latency_are_not_invariants() -> None:
    """The cost is the experiment's cap and a score of its own, not a check that fails."""
    costly = run(RunStatus.ACTED, usage=[call(cost=99.0, tokens_in=10**9, latency_ms=10**9)])

    assert check_invariants(LOAD, [observe(runs=[costly])]) == []


# --- expect.runs and purpose --------------------------------------------------------------


@pytest.mark.unit
def test_runs_zero_means_no_run_at_all() -> None:
    spec = expect(runs=0)

    assert grade(spec, observe()) == []
    failures = grade(spec, observe(runs=[run(RunStatus.SKIPPED, skip_reason="stale_timer")]))
    assert checks(failures) == ["expect.runs"]
    assert "expected 0 run(s), got 1" in failures[0].message


@pytest.mark.unit
def test_runs_counts_exactly() -> None:
    two = [run(RunStatus.ACTED), run(RunStatus.NOOP, run_id="run_2")]

    assert grade(expect(runs=2), observe(runs=two)) == []
    assert checks(grade(expect(runs=1), observe(runs=two))) == ["expect.runs"]


@pytest.mark.unit
def test_action_purpose_matches_the_payload_purpose() -> None:
    reply = item(OutboxKind.SMS, contact_id=DRIVER, text="PO is 55821", purpose="reply")
    eta = sms()

    assert grade(step(forbid=[{"kind": "sms", "purpose": "eta_request"}]), observe(reply)) == []
    failures = grade(step(forbid=[{"kind": "sms", "purpose": "eta_request"}]), observe(eta))
    assert checks(failures) == ["forbid[0]"]
    assert "with purpose eta_request" in failures[0].message
    assert grade(expect(actions=[{"kind": "sms", "purpose": "reply"}]), observe(reply)) == []


@pytest.mark.unit
def test_a_stage_update_matches_its_stage() -> None:
    update = item(OutboxKind.STAGE_UPDATE, stage="to_delivery")

    assert (
        grade(expect(actions=[{"kind": "stage_update", "stage": "to_delivery"}]), observe(update))
        == []
    )
    failures = grade(
        expect(actions=[{"kind": "stage_update", "stage": "at_delivery"}]), observe(update)
    )
    assert checks(failures) == ["expect.actions[0]"]
    assert "to stage at_delivery" in failures[0].message


# --- grade: groups and errors -------------------------------------------------------------


def scenario(*steps: dict[str, Any]) -> Scenario:
    return Scenario.model_validate(
        {
            "id": "s1",
            "title": "t",
            "suite": "core",
            "load": "fixtures/load_481207.json",
            "date": "2026-10-13",
            "start": "06:55",
            "steps": [{"at": f"07:0{i + 1}", **s} for i, s in enumerate(steps)],
        }
    )


# Each group has a failing check: no SMS to the driver (facts), no run with the intent
# (trajectory), a rubric nobody judged (judge), an SMS to a stranger (invariants).
EVERY_GROUP = scenario(
    {
        "expect": {
            "actions": [{"kind": "sms", "to": DRIVER}],
            "intent": "question",
            "judge": {"rubric": "The SMS is polite."},
        }
    }
)


@pytest.mark.unit
def test_grade_runs_every_group_in_order() -> None:
    failures = grade_scenario(EVERY_GROUP, [observe(sms(to="stranger"))])

    assert GROUPS == ("facts", "trajectory", "judge", "invariants")
    assert checks(failures) == [
        "expect.actions[0]",
        "expect.intent",
        "expect.judge",
        "invariant.recipients",
    ]
    assert [f.step_index for f in failures] == [0, 0, 0, None]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("group", "check"),
    [
        ("facts", "expect.actions[0]"),
        ("trajectory", "expect.intent"),
        ("judge", "expect.judge"),
        ("invariants", "invariant.recipients"),
    ],
)
def test_grade_keeps_only_the_checks_of_the_groups_asked(group: CheckGroup, check: str) -> None:
    failures = grade_scenario(EVERY_GROUP, [observe(sms(to="stranger"))], groups=(group,))

    assert checks(failures) == [check]


@pytest.mark.unit
def test_a_play_that_stopped_is_graded_on_the_steps_it_reached_and_fails_every_group() -> None:
    two_steps = scenario({"expect": {"llm_calls": 1}}, {"expect": {"llm_calls": 1}})

    for group in GROUPS:
        failures = grade_scenario(
            two_steps, [observe(llm_calls=1)], error="step 1: RuntimeError: boom", groups=(group,)
        )
        assert failures == [
            Failure(step_index=None, check="error", message="step 1: RuntimeError: boom")
        ]
    failures = grade_scenario(two_steps, [observe(llm_calls=2)], error="step 1: boom")
    assert [(f.step_index, f.check) for f in failures] == [
        (None, "error"),
        (0, "expect.llm_calls"),
    ]


@pytest.mark.unit
def test_a_play_with_no_step_has_only_its_error() -> None:
    failures = grade_scenario(EVERY_GROUP, [], error="unknown timezone 'Mars/Olympus'")

    assert [f.describe() for f in failures] == ["error: unknown timezone 'Mars/Olympus'"]


@pytest.mark.unit
def test_steps_are_graded_with_the_step_index_of_their_observation() -> None:
    two_steps = scenario({"expect": {"no_actions": True}}, {"expect": {"no_actions": True}})

    failures = grade_scenario(two_steps, [observe(), observe(sms(), step_index=1)])

    assert [(f.step_index, f.check) for f in failures] == [(1, "expect.no_actions")]


# --- step_checks: what the platform shows per step ---------------------------------------


@pytest.mark.unit
def test_declared_checks_name_every_check_of_the_step_in_spec_order() -> None:
    spec = step(
        expect={
            "judge": {"rubric": "polite"},
            "actions": [{"kind": "sms"}, {"kind": "email"}],
            "no_actions": True,
            "obligations": {"eta_request": None, "pod": {"status": "open"}},
            "timers": {"pending": 0},
            "run_status": ["acted"],
            "llm_calls": 1,
            "runs": 1,
            "intent": "question",
            "tools_used": ["send_sms"],
            "tools_refused": ["send_email"],
            "sections_read": ["escalation"],
            "min_rejections": 0,
            "max_rejections": 1,
        },
        forbid=[{"kind": "hero_task"}],
    )

    assert declared_checks(spec) == [
        "expect.actions[0]",
        "expect.actions[1]",
        "expect.no_actions",
        "forbid[0]",
        "expect.obligations.eta_request",
        "expect.obligations.pod",
        "expect.timers",
        "expect.run_status",
        "expect.llm_calls",
        "expect.runs",
        "expect.intent",
        "expect.tools_used",
        "expect.tools_refused",
        "expect.sections_read",
        "expect.min_rejections",
        "expect.max_rejections",
        "expect.judge",
    ]
    assert declared_checks(step()) == []


@pytest.mark.unit
def test_step_checks_list_every_declared_check_passed_or_failed() -> None:
    spec = step(
        expect={
            "actions": [{"kind": "sms", "to": DRIVER}, {"kind": "email"}],
            "obligations": {"eta_request": {"status": "open", "attempts": 2}},
            "timers": {"pending": 1, "due_at": "07:31"},
            "llm_calls": 1,
            "intent": "question",
            "judge": {"rubric": "polite"},
        },
        forbid=[{"kind": "hero_task"}],
    )
    obs = observe(
        sms(),
        obligations=[obligation()],
        timers=[timer("timer_1", T0 + timedelta(minutes=31))],
        llm_calls=1,
    )

    found = step_checks(spec, obs, timezone=TZ)

    assert [(c.name, c.passed) for c in found] == [
        ("expect.actions[0]", True),
        ("expect.actions[1]", False),
        ("forbid[0]", True),
        ("expect.obligations.eta_request", False),  # its `attempts` sub-check failed
        ("expect.timers", True),
        ("expect.llm_calls", True),
        ("expect.intent", False),
        ("expect.judge", False),
    ]
    assert all(c.detail is None for c in found if c.passed)
    by_name = {c.name: c for c in found}
    assert by_name["expect.obligations.eta_request"].detail is not None
    assert by_name["expect.obligations.eta_request"].detail.startswith("expected attempts 2, got 1")
    assert by_name["expect.judge"] == StepCheck(
        name="expect.judge",
        passed=False,
        detail="the step has a rubric but the judge did not run",
    )


@pytest.mark.unit
def test_two_failed_sub_checks_fail_their_check_once_with_both_details() -> None:
    spec = expect(timers={"pending": 2, "due_at": "08:00"})
    obs = observe(timers=[timer("timer_1", T0 + timedelta(minutes=31))])

    (check,) = step_checks(spec, obs, timezone=TZ)

    assert (check.name, check.passed) == ("expect.timers", False)
    assert check.detail is not None
    first, second = check.detail.split("; timers: timer_1 due 07:31 pending; ", 1)
    assert first.startswith("expected 2 pending timer(s), got 1")
    assert second.startswith("expected the earliest pending timer due at 08:00")


@pytest.mark.unit
def test_a_step_that_declares_nothing_has_no_checks() -> None:
    assert step_checks(step(), observe(sms()), timezone=TZ) == []
