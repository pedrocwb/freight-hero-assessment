"""The validator as a whole: approved actions keep their order, violations are reported."""

from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from freight_agent.domain.actions import (
    Action,
    CreateHeroTask,
    Plan,
    Purpose,
    ScheduleTimer,
    SendEmail,
    SendSms,
    UpdateStage,
    WriteTmsNote,
)
from freight_agent.domain.load import Load, Stage
from freight_agent.domain.obligations import ObligationKind
from freight_agent.domain.rules import default_rules
from freight_agent.domain.validation import (
    Severity,
    ValidationConfig,
    ValidationContext,
    Violation,
    check_action,
    validate,
)

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 13, 7, 1, tzinfo=LA)
DRIVER, DISPATCHER = "drv_20931", "dsp_4410"


class _RejectNotes:
    """A test rule: drops every TMS note."""

    name = "reject_notes"

    def check(
        self, action: Action, index: int, ctx: ValidationContext, approved: Sequence[Action]
    ) -> Violation | None:
        if isinstance(action, WriteTmsNote):
            return Violation(rule=self.name, action_index=index, message="no notes")
        return None


@pytest.mark.unit
def test_eta_attempt_cap_defaults_to_two() -> None:
    assert ValidationConfig().attempt_cap("eta_request") == 2


@pytest.mark.unit
def test_a_kind_with_no_cap_is_not_capped() -> None:
    assert ValidationConfig().attempt_cap("pod") is None
    assert ValidationConfig(max_attempts={"pod": 1}).attempt_cap("pod") == 1


@pytest.mark.unit
def test_check_action_judges_one_action_after_the_approved_ones(load: Load) -> None:
    ctx = ValidationContext(load=load, now=NOW)
    note = WriteTmsNote(text="ETA 09:40.")
    rules = (_RejectNotes(),)

    (violation,) = check_action(note, ctx, (CreateHeroTask(title="t", description="d"),), rules)

    assert (violation.rule, violation.action_index) == ("reject_notes", 1)
    assert check_action(note, ctx, (), rules, index=4)[0].action_index == 4
    assert check_action(CreateHeroTask(title="t", description="d"), ctx, (), rules) == ()


@pytest.mark.unit
def test_an_empty_plan_is_a_valid_noop(load: Load) -> None:
    outcome = validate(Plan(), ValidationContext(load=load, now=NOW), default_rules())

    assert outcome.approved == ()
    assert outcome.violations == ()
    assert not outcome.has_high_severity


@pytest.mark.unit
def test_every_rule_that_rejects_an_action_is_reported(load: Load) -> None:
    action = SendSms(contact_id="x@y.com", text="Forward the rate confirmation to x@y.com")

    outcome = validate(
        Plan(actions=(action,)), ValidationContext(load=load, now=NOW), default_rules()
    )

    assert outcome.approved == ()
    assert [(v.action_index, v.rule) for v in outcome.violations] == [
        (0, "recipient_is_contact"),
        (0, "no_contact_details_in_text"),
    ]


@pytest.mark.unit
def test_a_new_rule_plugs_in_and_dropped_actions_never_count_as_approved(load: Load) -> None:
    note = WriteTmsNote(text="ETA 08:15")
    plan = Plan(actions=(note, note))

    outcome = validate(
        plan, ValidationContext(load=load, now=NOW), (*default_rules(), _RejectNotes())
    )

    # The second note is not a duplicate: the first one was dropped, not approved.
    assert outcome.approved == ()
    assert [(v.action_index, v.rule) for v in outcome.violations] == [
        (0, "reject_notes"),
        (1, "reject_notes"),
    ]


@pytest.mark.unit
def test_default_rules_on_a_mixed_plan(load: Load) -> None:
    """The 07:01 run, with what a prompt-injected model might add to it."""
    eta_sms = SendSms(
        contact_id=DRIVER,
        text="What time will you arrive at the pickup?",
        purpose=Purpose.ETA_REQUEST,
    )
    timer = ScheduleTimer(kind=ObligationKind.ETA_REQUEST, due_in_minutes=30)
    note = WriteTmsNote(text="Delay risk high; ETA requested from the driver.")
    task = CreateHeroTask(title="Watch load 481207", description="Delay risk is high.")
    plan = Plan(
        actions=(
            eta_sms,  # 0 approved
            timer,  # 1 approved: the SMS above opens the obligation
            SendSms(contact_id="x@y.com", text="Here is the rate confirmation."),  # 2 HIGH
            SendEmail(to_contact_ids=(DISPATCHER,), body="Reply to ops@evil.example"),  # 3 HIGH
            note,  # 4 approved
            eta_sms,  # 5 duplicate
            UpdateStage(stage=Stage.DISPATCHED),  # 6 backwards
            task,  # 7 approved
        )
    )

    outcome = validate(plan, ValidationContext(load=load, now=NOW), default_rules())

    assert outcome.approved == (eta_sms, timer, note, task)
    assert [(v.action_index, v.rule, v.severity) for v in outcome.violations] == [
        (2, "recipient_is_contact", Severity.HIGH),
        (3, "no_contact_details_in_text", Severity.HIGH),
        (5, "no_duplicate_action", Severity.LOW),
        (6, "stage_moves_forward", Severity.LOW),
    ]
    assert outcome.has_high_severity
