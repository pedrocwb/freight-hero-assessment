"""The Hero tasks the runtime writes on its own: what they tell a Hero."""

import pytest

from freight_agent.application.context import RunContext
from freight_agent.application.escalation import (
    BLOCKED_TITLE,
    QUOTE_LIMIT,
    RejectionBudget,
    blocked_actions,
    deadline_passed,
    describe_event,
    model_failure,
    sop_gap,
    unknown_sender,
)
from freight_agent.domain.actions import SendEmail, SendSms, UpdateStage
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Channel, Load, Stage
from freight_agent.domain.validation import Severity, ValidationOutcome, Violation
from freight_agent.interfaces.llm import LlmError
from tests.unit.application.test_runtime_harness import DRIVER, LOAD_ID, at, plan

pytestmark = pytest.mark.unit


def context(load: Load, event: Event) -> RunContext:
    return RunContext(
        event=event,
        load=load,
        now=event.occurred_at,
        obligations=(),
        pending_timers=(),
        recent_events=(event,),
        recent_outbound=(),
    )


def event(type_: EventType, **fields: object) -> Event:
    base: dict[str, object] = {
        "event_id": "e-1",
        "load_id": LOAD_ID,
        "type": type_,
        "occurred_at": at(7, 31),
        "channel": Channel.SYSTEM,
    }
    return Event.model_validate({**base, **fields})


@pytest.mark.parametrize(
    ("trigger", "expected"),
    [
        (
            event(
                EventType.INBOUND_MESSAGE, channel=Channel.SMS, sender_contact_id=DRIVER, body="Hi"
            ),
            'the sms message from drv_20931 at 2026-10-13 07:31 PDT: "Hi"',
        ),
        (
            event(EventType.INBOUND_MESSAGE, channel=Channel.EMAIL, body="Who is this?"),
            'the email message from an unknown sender at 2026-10-13 07:31 PDT: "Who is this?"',
        ),
        (
            event(EventType.RISK_CHANGED, data={"previous": "high", "current": "medium"}),
            "the delay risk change from high to medium at 2026-10-13 07:31 PDT",
        ),
        (
            event(EventType.TIMER_DUE, data={"timer_id": "timer_1", "kind": "eta_request"}),
            "the eta_request follow-up timer due at 2026-10-13 07:31 PDT",
        ),
        (
            event(EventType.TIMER_DUE, data={"timer_id": "timer_1"}),
            "the follow-up follow-up timer due at 2026-10-13 07:31 PDT",
        ),
        (
            event(EventType.ATTACHMENT_RECEIVED),
            "the attachment_received event e-1 at 2026-10-13 07:31 PDT",
        ),
    ],
)
def test_describe_event(load: Load, trigger: Event, expected: str) -> None:
    assert describe_event(context(load, trigger)) == expected


def test_a_long_message_is_clipped(load: Load) -> None:
    long = event(EventType.INBOUND_MESSAGE, sender_contact_id=DRIVER, body="x" * 1000)

    text = describe_event(context(load, long))

    assert text.endswith('..."')
    assert len(text.split(": ", 1)[1]) == QUOTE_LIMIT + 2  # the quotes


def test_the_escalation_texts(load: Load) -> None:
    ctx = context(
        load,
        event(EventType.INBOUND_MESSAGE, channel=Channel.SMS, sender_contact_id="x", body="Hi"),
    )

    sender = unknown_sender(ctx, "x is not a contact of load 481207")
    gap = sop_gap(ctx, Intent.STATUS_UPDATE)
    failure = model_failure(ctx, LlmError("timeout after 60 s"))
    late = deadline_passed(ctx, 240.0)

    assert sender.reason == "unknown_sender: x is not a contact of load 481207"
    assert sender.task.description == (
        'The agent did not answer the sms message from x at 2026-10-13 07:31 PDT: "Hi". '
        "x is not a contact of load 481207."
    )
    assert gap.task.description.startswith(
        "No SOP section of Northline Logistics (northline) covers intent status_update at "
        "stage to_pickup"
    )
    assert failure.reason == "llm_error: timeout after 60 s"
    assert "(LlmError: timeout after 60 s)" in failure.task.description
    assert late.reason == "deadline: the run took more than 240 s"
    assert "within 240 seconds" in late.task.description
    assert [e.task.urgent for e in (sender, gap, failure, late)] == [False, False, True, True]


def test_a_blocked_plan_lists_each_violation(load: Load) -> None:
    ctx = context(load, event(EventType.INBOUND_MESSAGE, sender_contact_id=DRIVER, body="Hi"))
    leak = SendEmail(to_contact_ids=("x",), body="Call 555-123-4567")
    sms = SendSms(contact_id=DRIVER, text="ok")
    violations = (
        Violation(
            rule="recipient_is_contact",
            action_index=0,
            message="not a contact: x",
            severity=Severity.HIGH,
        ),
        Violation(
            rule="no_contact_details_in_text",
            action_index=0,
            message="outbound text contains a phone number",
            severity=Severity.HIGH,
        ),
    )
    outcome = ValidationOutcome(approved=(sms, sms), violations=violations)

    blocked = blocked_actions(ctx, plan(leak, sms, sms), outcome)

    assert blocked.reason == "blocked: no_contact_details_in_text, recipient_is_contact"
    assert "- action 0 (send_email): recipient_is_contact: not a contact: x" in (
        blocked.task.description
    )
    assert "2 other actions of the plan ran." in blocked.task.description
    assert "redirect the agent" in blocked.task.description


def test_a_spent_rejection_budget_is_named_first_with_the_count(load: Load) -> None:
    ctx = context(load, event(EventType.INBOUND_MESSAGE, sender_contact_id=DRIVER, body="Hi"))
    sms = SendSms(contact_id=DRIVER, text="ok")
    refused = Violation(rule="stage_moves_forward", action_index=1, message="already to_pickup")
    outcome = ValidationOutcome(approved=(sms,), violations=(refused,))
    budget = RejectionBudget(refused=4, limit=3)

    blocked = blocked_actions(ctx, plan(sms, UpdateStage(stage=Stage.TO_PICKUP)), outcome, budget)

    assert budget.exceeded and not RejectionBudget(refused=3, limit=3).exceeded
    assert blocked.reason == "blocked: max_rejections"
    assert blocked.task.title == BLOCKED_TITLE and blocked.task.urgent
    lines = blocked.task.description.splitlines()
    assert lines[1] == "The agent's action calls were refused 4 times, over the limit of 3."
    assert lines[2] == "- action 1 (update_stage): stage_moves_forward: already to_pickup"
    assert lines[3] == (
        "1 other action of the plan ran. Check what the agent was trying to do, and do it by hand."
    )


def test_a_budget_within_its_limit_changes_nothing(load: Load) -> None:
    ctx = context(load, event(EventType.INBOUND_MESSAGE, sender_contact_id=DRIVER, body="Hi"))
    leak = SendEmail(to_contact_ids=("x",), body="hi")
    high = Violation(
        rule="recipient_is_contact", action_index=0, message="x", severity=Severity.HIGH
    )
    outcome = ValidationOutcome(approved=(), violations=(high,))

    within = blocked_actions(ctx, plan(leak), outcome, RejectionBudget(refused=1, limit=3))

    assert within == blocked_actions(ctx, plan(leak), outcome)
