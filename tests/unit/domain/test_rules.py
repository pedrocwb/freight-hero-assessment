"""Each validator rule on its own: one action, a context and the actions approved before it.

The shared `load` fixture is load 481207: driver `drv_20931` (sms), dispatcher
`dsp_4410` (email), broker `brk_0087` (email, slack), stage `to_pickup`.
"""

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from freight_agent.domain.actions import (
    Action,
    CloseObligation,
    CloseStatus,
    CreateHeroTask,
    DeferObligation,
    OpenObligation,
    PostBrokerSlack,
    PostInternalSlack,
    Purpose,
    ScheduleTimer,
    SendEmail,
    SendSms,
    UpdateStage,
    WriteTmsNote,
)
from freight_agent.domain.load import Channel, Load, Stage
from freight_agent.domain.obligations import DeferralPolicy, Obligation, ObligationKind
from freight_agent.domain.rules import (
    AttemptCap,
    ChannelAllowed,
    DeferralWithinPolicy,
    KnownObligationKind,
    NoContactDetailsInText,
    NoDuplicateAction,
    ObligationExists,
    RecipientIsContact,
    SmsRateLimit,
    StageMovesForward,
    default_rules,
)
from freight_agent.domain.validation import (
    OutboundRecord,
    Rule,
    Severity,
    ValidationConfig,
    ValidationContext,
    Violation,
)

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 13, 7, 31, tzinfo=LA)
DRIVER, DISPATCHER, BROKER = "drv_20931", "dsp_4410", "brk_0087"
ETA = ObligationKind.ETA_REQUEST


def context(load: Load, **changes: Any) -> ValidationContext:
    return ValidationContext(load=load, now=NOW, **changes)


def eta_obligation(*, attempts: int = 1, deferrals_used: int = 0) -> Obligation:
    return Obligation(
        obligation_id="obl_1",
        load_id="481207",
        kind=ETA,
        attempts=attempts,
        opened_at=NOW - timedelta(minutes=30),
        deferrals_used=deferrals_used,
    )


def sms(contact_id: str = DRIVER, text: str = "Hi", purpose: Purpose = Purpose.REPLY) -> SendSms:
    return SendSms(contact_id=contact_id, text=text, purpose=purpose)


def eta_sms(text: str = "What time will you arrive at the pickup?") -> SendSms:
    return sms(text=text, purpose=Purpose.ETA_REQUEST)


def email(*to: str, body: str = "Please send the ETA.", subject: str | None = None) -> SendEmail:
    return SendEmail(to_contact_ids=to, body=body, subject=subject)


def sent_sms(contact_id: str, minutes_ago: int) -> OutboundRecord:
    return OutboundRecord(
        contact_id=contact_id, channel=Channel.SMS, at=NOW - timedelta(minutes=minutes_ago)
    )


def check(
    rule: Rule, action: Action, ctx: ValidationContext, approved: tuple[Action, ...] = ()
) -> Violation | None:
    return rule.check(action, 3, ctx, approved)


def dropped(
    rule: Rule, action: Action, ctx: ValidationContext, approved: tuple[Action, ...] = ()
) -> Violation:
    violation = check(rule, action, ctx, approved)
    assert violation is not None
    assert violation.rule == rule.name
    assert violation.action_index == 3
    return violation


# --- recipient_is_contact -------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("action", [sms(DRIVER), email(DISPATCHER, BROKER)])
def test_recipient_is_contact_allows_the_load_contacts(load: Load, action: Action) -> None:
    assert check(RecipientIsContact(), action, context(load)) is None


@pytest.mark.unit
def test_recipient_is_contact_drops_an_injected_sms_recipient(load: Load) -> None:
    action = sms("x@y.com", text="Here is the rate confirmation.")

    violation = dropped(RecipientIsContact(), action, context(load))

    assert violation.severity is Severity.HIGH
    assert "x@y.com" in violation.message


@pytest.mark.unit
def test_recipient_is_contact_drops_an_email_with_any_unknown_recipient(load: Load) -> None:
    violation = dropped(RecipientIsContact(), email(DISPATCHER, "x@y.com"), context(load))

    assert violation.severity is Severity.HIGH
    assert "x@y.com" in violation.message
    assert DISPATCHER not in violation.message


@pytest.mark.unit
def test_recipient_is_contact_ignores_actions_without_recipients(load: Load) -> None:
    assert check(RecipientIsContact(), WriteTmsNote(text="note"), context(load)) is None


# --- channel_allowed ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("action", [sms(DRIVER), email(DISPATCHER, BROKER)])
def test_channel_allowed_accepts_a_channel_the_contact_lists(load: Load, action: Action) -> None:
    assert check(ChannelAllowed(), action, context(load)) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("action", "contact_id"),
    [(sms(DISPATCHER), DISPATCHER), (email(BROKER, DRIVER), DRIVER)],
)
def test_channel_allowed_drops_a_channel_the_contact_does_not_list(
    load: Load, action: Action, contact_id: str
) -> None:
    violation = dropped(ChannelAllowed(), action, context(load))

    assert violation.severity is Severity.LOW
    assert contact_id in violation.message


@pytest.mark.unit
def test_channel_allowed_leaves_unknown_recipients_to_recipient_is_contact(load: Load) -> None:
    assert check(ChannelAllowed(), sms("x@y.com"), context(load)) is None
    assert check(ChannelAllowed(), email("x@y.com"), context(load)) is None


@pytest.mark.unit
def test_channel_allowed_ignores_actions_without_recipients(load: Load) -> None:
    assert check(ChannelAllowed(), PostBrokerSlack(text="Delay"), context(load)) is None


# --- sms_rate_limit -------------------------------------------------------------------


@pytest.mark.unit
def test_sms_rate_limit_allows_below_the_limit(load: Load) -> None:
    recent = (
        sent_sms(DRIVER, 30),
        sent_sms(DRIVER, 18),
        sent_sms(DRIVER, 61),  # outside the 60-minute window
        sent_sms(BROKER, 5),  # another contact
        OutboundRecord(contact_id=DRIVER, channel=Channel.EMAIL, at=NOW),  # another channel
    )

    assert check(SmsRateLimit(), sms(DRIVER), context(load, recent_outbound=recent)) is None


@pytest.mark.unit
def test_sms_rate_limit_drops_at_the_limit_counting_the_window_start(load: Load) -> None:
    recent = (sent_sms(DRIVER, 60), sent_sms(DRIVER, 30), sent_sms(DRIVER, 18))

    violation = dropped(SmsRateLimit(), sms(DRIVER), context(load, recent_outbound=recent))

    assert violation.severity is Severity.LOW
    assert DRIVER in violation.message


@pytest.mark.unit
def test_sms_rate_limit_counts_sms_approved_earlier_in_the_plan(load: Load) -> None:
    recent = (sent_sms(DRIVER, 30), sent_sms(DRIVER, 18))
    ctx = context(load, recent_outbound=recent)

    assert check(SmsRateLimit(), sms(DRIVER), ctx, (sms(BROKER), email(DRIVER))) is None
    dropped(SmsRateLimit(), sms(DRIVER, text="Again"), ctx, (sms(DRIVER),))


@pytest.mark.unit
def test_sms_rate_limit_reads_the_configured_window(load: Load) -> None:
    config = ValidationConfig(sms_max_per_window=1, sms_window_minutes=10)

    allowed = context(load, recent_outbound=(sent_sms(DRIVER, 11),), config=config)
    limited = context(load, recent_outbound=(sent_sms(DRIVER, 9),), config=config)

    assert check(SmsRateLimit(), sms(DRIVER), allowed) is None
    dropped(SmsRateLimit(), sms(DRIVER), limited)


@pytest.mark.unit
def test_sms_rate_limit_ignores_other_actions(load: Load) -> None:
    recent = tuple(sent_sms(DRIVER, m) for m in (1, 2, 3))

    assert check(SmsRateLimit(), email(DRIVER), context(load, recent_outbound=recent)) is None


# --- obligation_exists ----------------------------------------------------------------

LEDGER_ACTIONS: tuple[Action, ...] = (
    ScheduleTimer(kind=ETA, due_in_minutes=30),
    DeferObligation(kind=ETA, minutes=15),
    CloseObligation(kind=ETA, status=CloseStatus.SATISFIED),
)


@pytest.mark.unit
@pytest.mark.parametrize("action", LEDGER_ACTIONS)
def test_obligation_exists_allows_ledger_actions_on_an_open_obligation(
    load: Load, action: Action
) -> None:
    ctx = context(load, open_obligations=(eta_obligation(),))

    assert check(ObligationExists(), action, ctx) is None


@pytest.mark.unit
@pytest.mark.parametrize("action", LEDGER_ACTIONS)
def test_obligation_exists_drops_ledger_actions_without_an_open_obligation(
    load: Load, action: Action
) -> None:
    violation = dropped(ObligationExists(), action, context(load))

    assert violation.severity is Severity.LOW
    assert "eta_request" in violation.message


@pytest.mark.unit
def test_obligation_exists_allows_a_timer_after_an_eta_request_sms_in_the_plan(
    load: Load,
) -> None:
    timer = ScheduleTimer(kind=ETA, due_in_minutes=30)

    assert check(ObligationExists(), timer, context(load), (eta_sms(),)) is None


@pytest.mark.unit
def test_obligation_exists_needs_an_eta_request_sms_not_any_sms(load: Load) -> None:
    timer = ScheduleTimer(kind=ETA, due_in_minutes=30)

    dropped(ObligationExists(), timer, context(load), (sms(DRIVER),))


@pytest.mark.unit
@pytest.mark.parametrize(
    "action",
    [
        DeferObligation(kind=ETA, minutes=15),
        CloseObligation(kind=ETA, status=CloseStatus.CANCELLED),
    ],
)
def test_obligation_exists_does_not_let_the_plan_defer_or_close_what_it_just_opened(
    load: Load, action: Action
) -> None:
    dropped(ObligationExists(), action, context(load), (eta_sms(),))


@pytest.mark.unit
def test_obligation_exists_drops_a_timer_after_the_plan_closed_the_obligation(load: Load) -> None:
    ctx = context(load, open_obligations=(eta_obligation(),))
    close = CloseObligation(kind=ETA, status=CloseStatus.SATISFIED)

    dropped(ObligationExists(), ScheduleTimer(kind=ETA, due_in_minutes=30), ctx, (close,))


@pytest.mark.unit
def test_obligation_exists_ignores_other_actions(load: Load) -> None:
    assert check(ObligationExists(), eta_sms(), context(load)) is None


@pytest.mark.unit
def test_obligation_exists_allows_a_timer_after_the_plan_opened_the_obligation(
    load: Load,
) -> None:
    timer = ScheduleTimer(kind="pod", due_in_minutes=120)

    assert check(ObligationExists(), timer, context(load), (OpenObligation(kind="pod"),)) is None
    dropped(ObligationExists(), timer, context(load), (OpenObligation(kind="bol"),))


@pytest.mark.unit
def test_obligation_exists_counts_an_sms_that_requests_the_kind(load: Load) -> None:
    timer = ScheduleTimer(kind="pod", due_in_minutes=120)
    ask_pod = SendSms(contact_id=DRIVER, text="Please send the POD.", requests="pod")

    assert check(ObligationExists(), timer, context(load), (ask_pod,)) is None


# --- deferral_policy ------------------------------------------------------------------


@pytest.mark.unit
def test_deferral_policy_allows_one_deferral_within_the_limit(load: Load) -> None:
    ctx = context(load, open_obligations=(eta_obligation(),))

    assert check(DeferralWithinPolicy(), DeferObligation(kind=ETA, minutes=15), ctx) is None


@pytest.mark.unit
def test_deferral_policy_drops_a_deferral_longer_than_the_limit(load: Load) -> None:
    ctx = context(load, open_obligations=(eta_obligation(),))

    violation = dropped(DeferralWithinPolicy(), DeferObligation(kind=ETA, minutes=16), ctx)

    assert violation.severity is Severity.LOW
    assert "15" in violation.message


@pytest.mark.unit
def test_deferral_policy_drops_a_deferral_beyond_the_allowed_count(load: Load) -> None:
    ctx = context(load, open_obligations=(eta_obligation(deferrals_used=1),))

    dropped(DeferralWithinPolicy(), DeferObligation(kind=ETA, minutes=5), ctx)


@pytest.mark.unit
def test_deferral_policy_counts_deferrals_approved_earlier_in_the_plan(load: Load) -> None:
    ctx = context(load, open_obligations=(eta_obligation(),))
    first = DeferObligation(kind=ETA, minutes=10)

    dropped(DeferralWithinPolicy(), DeferObligation(kind=ETA, minutes=5), ctx, (first,))


@pytest.mark.unit
def test_deferral_policy_reads_the_configured_policy(load: Load) -> None:
    config = ValidationConfig(deferral=DeferralPolicy(max_minutes=30, max_per_obligation=2))
    ctx = context(load, open_obligations=(eta_obligation(deferrals_used=1),), config=config)

    assert check(DeferralWithinPolicy(), DeferObligation(kind=ETA, minutes=30), ctx) is None


@pytest.mark.unit
def test_deferral_policy_leaves_a_missing_obligation_to_obligation_exists(load: Load) -> None:
    assert (
        check(DeferralWithinPolicy(), DeferObligation(kind=ETA, minutes=5), context(load)) is None
    )


@pytest.mark.unit
def test_deferral_policy_ignores_other_actions(load: Load) -> None:
    timer = ScheduleTimer(kind=ETA, due_in_minutes=120)

    assert check(DeferralWithinPolicy(), timer, context(load)) is None


# --- attempt_cap ----------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("obligations", [(), (eta_obligation(attempts=1),)])
def test_attempt_cap_allows_requests_below_the_cap(
    load: Load, obligations: tuple[Obligation, ...]
) -> None:
    ctx = context(load, open_obligations=obligations)

    assert check(AttemptCap(), eta_sms(), ctx) is None


@pytest.mark.unit
def test_attempt_cap_drops_a_request_at_the_cap(load: Load) -> None:
    ctx = context(load, open_obligations=(eta_obligation(attempts=2),))

    violation = dropped(AttemptCap(), eta_sms(), ctx)

    assert violation.severity is Severity.LOW
    assert violation.message == (
        "eta_request was already requested 2 times (cap 2): its requests are used up"
    )


@pytest.mark.unit
def test_attempt_cap_counts_requests_approved_earlier_in_the_plan(load: Load) -> None:
    ctx = context(load, open_obligations=(eta_obligation(attempts=1),))

    dropped(AttemptCap(), eta_sms("Your ETA, please?"), ctx, (eta_sms(),))


@pytest.mark.unit
def test_attempt_cap_starts_over_after_the_plan_closes_the_obligation(load: Load) -> None:
    ctx = context(load, open_obligations=(eta_obligation(attempts=2),))
    close = CloseObligation(kind=ETA, status=CloseStatus.EXHAUSTED)

    assert (
        check(AttemptCap(), eta_sms(), ctx, (close, CreateHeroTask(title="t", description="")))
        is None
    )


@pytest.mark.unit
def test_attempt_cap_reads_the_configured_cap(load: Load) -> None:
    ctx = context(
        load,
        open_obligations=(eta_obligation(attempts=1),),
        config=ValidationConfig(max_attempts={"eta_request": 1}),
    )

    dropped(AttemptCap(), eta_sms(), ctx)


@pytest.mark.unit
def test_attempt_cap_ignores_messages_that_request_nothing(load: Load) -> None:
    ctx = context(load, open_obligations=(eta_obligation(attempts=2),))

    assert check(AttemptCap(), sms(text="PO 55821"), ctx) is None
    assert check(AttemptCap(), email(DISPATCHER), ctx) is None
    assert check(AttemptCap(), WriteTmsNote(text="note"), ctx) is None


@pytest.mark.unit
def test_attempt_cap_is_per_kind(load: Load) -> None:
    """Two POD requests are the cap of `pod`; the ETA requests do not count against it."""
    pod = Obligation(obligation_id="obl_2", load_id="481207", kind="pod", attempts=2, opened_at=NOW)
    config = ValidationConfig(max_attempts={"eta_request": 2, "pod": 2})
    ctx = context(load, open_obligations=(eta_obligation(attempts=1), pod), config=config)
    ask_pod = SendSms(contact_id=DRIVER, text="Please send the POD.", requests="pod")

    violation = dropped(AttemptCap(), ask_pod, ctx)
    assert violation.message.startswith("pod was already requested 2 times (cap 2)")
    assert check(AttemptCap(), eta_sms(), ctx) is None


@pytest.mark.unit
def test_attempt_cap_counts_an_email_that_requests_the_kind(load: Load) -> None:
    """An email that asks for the ETA is a request too, like the executor counts it."""
    ask = SendEmail(to_contact_ids=(DISPATCHER,), body="ETA?", purpose=Purpose.ETA_REQUEST)
    ctx = context(load, open_obligations=(eta_obligation(attempts=1),))

    assert check(AttemptCap(), ask, ctx) is None
    dropped(AttemptCap(), eta_sms(), ctx, (ask,))
    dropped(AttemptCap(), ask, context(load, open_obligations=(eta_obligation(attempts=2),)))


@pytest.mark.unit
def test_attempt_cap_leaves_a_kind_without_a_cap_alone(load: Load) -> None:
    pod = Obligation(obligation_id="obl_2", load_id="481207", kind="pod", attempts=9, opened_at=NOW)
    ask_pod = SendSms(contact_id=DRIVER, text="Please send the POD.", requests="pod")

    assert check(AttemptCap(), ask_pod, context(load, open_obligations=(pod,))) is None


# --- known_obligation_kind ------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "action",
    [
        SendSms(contact_id=DRIVER, text="POD please", requests="pod"),
        SendEmail(to_contact_ids=(DISPATCHER,), body="POD please", requests="pod"),
        ScheduleTimer(kind="pod", due_in_minutes=30),
        DeferObligation(kind="pod", minutes=5),
        CloseObligation(kind="pod", status=CloseStatus.CANCELLED),
        OpenObligation(kind="pod"),
    ],
)
def test_known_obligation_kind_drops_a_kind_outside_the_catalog(load: Load, action: Action) -> None:
    ctx = context(load, obligation_kinds=("eta_request",))

    violation = dropped(KnownObligationKind(), action, ctx)

    assert violation.message == (
        "pod is not an obligation kind of this broker (kinds: eta_request)"
    )


@pytest.mark.unit
def test_known_obligation_kind_allows_catalog_kinds_and_other_actions(load: Load) -> None:
    ctx = context(load, obligation_kinds=("eta_request", "pod"))

    assert check(KnownObligationKind(), OpenObligation(kind="pod"), ctx) is None
    assert check(KnownObligationKind(), eta_sms(), ctx) is None
    assert check(KnownObligationKind(), sms(text="PO 55821"), ctx) is None
    assert check(KnownObligationKind(), WriteTmsNote(text="note"), ctx) is None


@pytest.mark.unit
def test_known_obligation_kind_without_a_catalog_allows_any_kind(load: Load) -> None:
    assert check(KnownObligationKind(), OpenObligation(kind="anything"), context(load)) is None


@pytest.mark.unit
def test_known_obligation_kind_with_an_empty_catalog_says_none(load: Load) -> None:
    violation = dropped(KnownObligationKind(), eta_sms(), context(load, obligation_kinds=()))

    assert violation.message.endswith("(kinds: none)")


# --- open_obligation ------------------------------------------------------------------


@pytest.mark.unit
def test_obligation_exists_drops_opening_an_obligation_that_is_open(load: Load) -> None:
    pod = Obligation(obligation_id="obl_2", load_id="481207", kind="pod", attempts=0, opened_at=NOW)

    violation = dropped(
        ObligationExists(), OpenObligation(kind="pod"), context(load, open_obligations=(pod,))
    )

    assert violation.message == "an open pod obligation already exists"
    assert check(ObligationExists(), OpenObligation(kind="pod"), context(load)) is None
    dropped(
        ObligationExists(), OpenObligation(kind="pod"), context(load), (OpenObligation(kind="pod"),)
    )


# --- no_duplicate_action --------------------------------------------------------------


@pytest.mark.unit
def test_no_duplicate_action_allows_a_different_action_of_the_same_type(load: Load) -> None:
    approved: tuple[Action, ...] = (sms(text="PO 55821"),)

    assert (
        check(NoDuplicateAction(), sms(text="Appointment 06:00-14:00"), context(load), approved)
        is None
    )


@pytest.mark.unit
def test_no_duplicate_action_drops_an_action_already_approved(load: Load) -> None:
    approved: tuple[Action, ...] = (WriteTmsNote(text="ETA 08:15"), sms(text="PO 55821"))

    violation = dropped(NoDuplicateAction(), sms(text="PO 55821"), context(load), approved)

    assert violation.severity is Severity.LOW


# --- no_contact_details_in_text -------------------------------------------------------

CONTACT_DETAILS = [
    "Send it to x@y.com",
    "Email ops.team+loads@prairie-haul.co.uk now",
    "Call me at 555-123-4567",
    "Call (555) 123-4567.",
    "New number +1 555 123 4567",
    "Text 5551234567",
    "Dial 1-555-123-4567 tonight",
    "UK office: +44 20 7946 0958",
]

SAFE_TEXTS = [
    "PO 55821 is required at the gate.",
    "Load 481207, MC 778120, 38,000 lb.",
    "Appointment 2026-10-14 09:30, window 06:00-14:00.",
    "ETA 10/14/2026 at 7:45 AM, about +15 min late.",
    "Trailer at 34F, seal 7731942.",
    "Thanks @brk_0087, noted.",
]


def outbound_with(text: str) -> tuple[Action, ...]:
    return (
        sms(text=text),
        email(DISPATCHER, body=text),
        email(DISPATCHER, subject=text),
        PostBrokerSlack(text=text),
    )


@pytest.mark.unit
@pytest.mark.parametrize("text", SAFE_TEXTS)
def test_no_contact_details_allows_ordinary_load_details(load: Load, text: str) -> None:
    for action in outbound_with(text):
        assert check(NoContactDetailsInText(), action, context(load)) is None


@pytest.mark.unit
@pytest.mark.parametrize("text", CONTACT_DETAILS)
def test_no_contact_details_drops_outbound_text_with_an_address_or_number(
    load: Load, text: str
) -> None:
    for action in outbound_with(text):
        violation = dropped(NoContactDetailsInText(), action, context(load))
        assert violation.severity is Severity.HIGH


@pytest.mark.unit
def test_no_contact_details_names_every_kind_it_found(load: Load) -> None:
    action = sms(text="Write to x@y.com or call 555-123-4567")

    message = dropped(NoContactDetailsInText(), action, context(load)).message

    assert "email address" in message
    assert "phone number" in message


@pytest.mark.unit
@pytest.mark.parametrize(
    "action",
    [
        WriteTmsNote(text="Driver's new number: 555-123-4567"),
        CreateHeroTask(title="Call the driver", description="He wrote from 555-123-4567"),
        PostInternalSlack(text="Dispatcher replied from x@y.com"),
    ],
)
def test_no_contact_details_ignores_internal_text(load: Load, action: Action) -> None:
    assert check(NoContactDetailsInText(), action, context(load)) is None


# --- stage_moves_forward --------------------------------------------------------------


def at_stage(load: Load, stage: Stage) -> ValidationContext:
    return context(load.model_copy(update={"stage": stage}))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("current", "target"),
    [
        (Stage.DISPATCHED, Stage.TO_PICKUP),
        (Stage.TO_PICKUP, Stage.AT_PICKUP),
        (Stage.TO_PICKUP, Stage.TO_DELIVERY),  # a missed arrival
        (Stage.AT_PICKUP, Stage.TO_PICKUP),  # next pickup of a multi-stop load
        (Stage.AT_DELIVERY, Stage.TO_DELIVERY),  # next delivery of a multi-stop load
        (Stage.AT_DELIVERY, Stage.DELIVERED),
    ],
)
def test_stage_moves_forward_allows_forward_moves_and_repeated_stops(
    load: Load, current: Stage, target: Stage
) -> None:
    assert check(StageMovesForward(), UpdateStage(stage=target), at_stage(load, current)) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("current", "target", "reason"),
    [
        (Stage.TO_PICKUP, Stage.TO_PICKUP, "already"),
        (Stage.TO_PICKUP, Stage.DISPATCHED, "dispatched"),
        (Stage.DELIVERED, Stage.AT_DELIVERY, "delivered"),
        (Stage.TO_DELIVERY, Stage.TO_PICKUP, "back"),
        (Stage.AT_DELIVERY, Stage.TO_PICKUP, "back"),
    ],
)
def test_stage_moves_forward_drops_other_moves(
    load: Load, current: Stage, target: Stage, reason: str
) -> None:
    violation = dropped(StageMovesForward(), UpdateStage(stage=target), at_stage(load, current))

    assert violation.severity is Severity.LOW
    assert reason in violation.message


@pytest.mark.unit
def test_stage_moves_forward_starts_from_the_stage_set_earlier_in_the_plan(load: Load) -> None:
    ctx = at_stage(load, Stage.TO_PICKUP)
    approved: tuple[Action, ...] = (UpdateStage(stage=Stage.AT_PICKUP), WriteTmsNote(text="n"))

    assert check(StageMovesForward(), UpdateStage(stage=Stage.TO_DELIVERY), ctx, approved) is None
    dropped(StageMovesForward(), UpdateStage(stage=Stage.AT_PICKUP), ctx, approved)


@pytest.mark.unit
def test_stage_moves_forward_ignores_other_actions(load: Load) -> None:
    assert (
        check(StageMovesForward(), WriteTmsNote(text="note"), at_stage(load, Stage.DELIVERED))
        is None
    )


# --- default_rules --------------------------------------------------------------------


@pytest.mark.unit
def test_default_rules_hold_every_rule_once_security_first() -> None:
    names = [rule.name for rule in default_rules()]

    assert names[:2] == ["recipient_is_contact", "no_contact_details_in_text"]
    assert sorted(names) == sorted(
        {
            "recipient_is_contact",
            "channel_allowed",
            "sms_rate_limit",
            "obligation_exists",
            "deferral_policy",
            "attempt_cap",
            "known_obligation_kind",
            "no_duplicate_action",
            "no_contact_details_in_text",
            "stage_moves_forward",
        }
    )
