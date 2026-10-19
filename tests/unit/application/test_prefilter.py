from datetime import datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from freight_agent.adapters.persistence.memory.store import MemoryStore
from freight_agent.application.context import RunContext, load_run_context
from freight_agent.application.prefilter import (
    EscalationReason,
    PrefilterResult,
    SkipReason,
    prefilter,
)
from freight_agent.domain.events import Attachment, Event, EventType
from freight_agent.domain.load import Channel, Load
from freight_agent.domain.obligations import (
    DeferralPolicy,
    Obligation,
    ObligationKind,
    ObligationStatus,
)
from freight_agent.domain.timers import Timer, TimerStatus
from freight_agent.ports.repositories import UnitOfWork

LA = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 10, 13, 7, 0, tzinfo=LA)
PDF = Attachment(
    attachment_id="att_1", filename="bol.pdf", content_type="application/pdf", ref="s3://bol"
)


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _message(
    *,
    body: str = "What's the PO number?",
    sender: str | None = "drv_20931",
    attachments: tuple[Attachment, ...] = (),
) -> Event:
    return Event(
        event_id="m1",
        load_id="481207",
        type=EventType.INBOUND_MESSAGE,
        occurred_at=_at(12),
        channel=Channel.SMS,
        sender_contact_id=sender,
        body=body,
        attachments=attachments,
    )


def _system_event(event_type: EventType, **data: str) -> Event:
    return Event(
        event_id=f"{event_type}:1",
        load_id="481207",
        type=event_type,
        occurred_at=_at(0),
        channel=Channel.SYSTEM,
        data=dict(data),
    )


def _timer_due(timer_id: str = "timer_1") -> Event:
    return _system_event(
        EventType.TIMER_DUE, timer_id=timer_id, obligation_id="obl_1", kind="eta_request"
    )


def _obligation(active_timer_id: str | None = "timer_1") -> Obligation:
    obligation = Obligation.open(
        obligation_id="obl_1",
        load_id="481207",
        kind=ObligationKind.ETA_REQUEST,
        at=_at(1),
        first_attempt=True,
    )
    return obligation.attach_timer(active_timer_id) if active_timer_id else obligation


def _timer(timer_id: str = "timer_1", status: TimerStatus = TimerStatus.FIRED) -> Timer:
    return Timer(
        timer_id=timer_id,
        load_id="481207",
        obligation_id="obl_1",
        due_at=_at(31),
        created_at=_at(1),
        status=status,
    )


def _ctx(
    load: Load,
    event: Event,
    *,
    obligations: tuple[Obligation, ...] = (),
    event_timer: Timer | None = None,
    already_processed: bool = False,
) -> RunContext:
    return RunContext(
        event=event,
        load=load,
        now=_at(31),
        obligations=obligations,
        pending_timers=(),
        recent_events=(event,),
        recent_outbound=(),
        event_timer=event_timer,
        already_processed=already_processed,
    )


def _skip(reason: SkipReason, result: PrefilterResult | None) -> PrefilterResult:
    assert result is not None
    assert result.skip is reason
    assert result.escalate is None
    assert result.detail
    return result


# --- what passes through -------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "event",
    [
        _message(),
        _message(body="", attachments=(PDF,)),
        _system_event(EventType.RISK_CHANGED, previous="low", current="high"),
        _system_event(EventType.ATTACHMENT_RECEIVED),
        _system_event(EventType.HERO_UPDATE),
    ],
    ids=["question", "photo_only", "risk", "attachment", "hero_update"],
)
def test_events_that_need_the_gate_pass_through(load: Load, event: Event) -> None:
    assert prefilter(_ctx(load, event)) is None


@pytest.mark.unit
def test_the_active_timer_of_an_open_obligation_passes_through(load: Load) -> None:
    ctx = _ctx(load, _timer_due(), obligations=(_obligation(),), event_timer=_timer())

    assert prefilter(ctx) is None


# --- already processed ---------------------------------------------------------------------


@pytest.mark.unit
def test_an_event_with_a_run_is_skipped(load: Load) -> None:
    result = _skip(
        SkipReason.ALREADY_PROCESSED, prefilter(_ctx(load, _message(), already_processed=True))
    )

    assert "m1" in result.detail


@pytest.mark.unit
@pytest.mark.parametrize(
    "event",
    [_timer_due("timer_gone"), _message(body="  "), _message(sender=None)],
    ids=["stale_timer", "empty", "unknown_sender"],
)
def test_already_processed_is_checked_first(load: Load, event: Event) -> None:
    _skip(SkipReason.ALREADY_PROCESSED, prefilter(_ctx(load, event, already_processed=True)))


# --- stale timer ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_missing_timer_is_stale(load: Load) -> None:
    result = _skip(SkipReason.STALE_TIMER, prefilter(_ctx(load, _timer_due("timer_gone"))))

    assert "timer_gone" in result.detail
    assert "not found" in result.detail


@pytest.mark.unit
@pytest.mark.parametrize("status", [TimerStatus.SUPERSEDED, TimerStatus.CANCELLED])
def test_a_superseded_or_cancelled_timer_is_stale(load: Load, status: TimerStatus) -> None:
    ctx = _ctx(load, _timer_due(), obligations=(_obligation(),), event_timer=_timer(status=status))

    result = _skip(SkipReason.STALE_TIMER, prefilter(ctx))

    assert str(status) in result.detail


@pytest.mark.unit
def test_a_timer_whose_obligation_is_unknown_is_stale(load: Load) -> None:
    result = _skip(
        SkipReason.STALE_TIMER, prefilter(_ctx(load, _timer_due(), event_timer=_timer()))
    )

    assert "obl_1" in result.detail


@pytest.mark.unit
def test_a_timer_of_a_closed_obligation_is_stale(load: Load) -> None:
    satisfied = _obligation().close(ObligationStatus.SATISFIED)
    ctx = _ctx(load, _timer_due(), obligations=(satisfied,), event_timer=_timer())

    result = _skip(SkipReason.STALE_TIMER, prefilter(ctx))

    assert "satisfied" in result.detail


@pytest.mark.unit
def test_the_deferral_race_a_fired_timer_whose_obligation_moved_on_is_stale(load: Load) -> None:
    """The timer fired, but a deferral replaced it before the run: only the new one may act."""
    ctx = _ctx(
        load,
        _timer_due("timer_1"),
        obligations=(_obligation(active_timer_id="timer_2"),),
        event_timer=_timer("timer_1", status=TimerStatus.FIRED),
    )

    result = _skip(SkipReason.STALE_TIMER, prefilter(ctx))

    assert "timer_2" in result.detail


@pytest.mark.unit
def test_a_timer_of_an_obligation_with_no_active_timer_is_stale(load: Load) -> None:
    ctx = _ctx(
        load,
        _timer_due(),
        obligations=(_obligation(active_timer_id=None),),
        event_timer=_timer(),
    )

    result = _skip(SkipReason.STALE_TIMER, prefilter(ctx))

    assert "no active timer" in result.detail


@pytest.mark.unit
async def test_the_deferral_race_through_the_store(store: MemoryStore, load: Load) -> None:
    """The 07:31 timer fired, but the driver's "5 min" (sent 07:30, run first) deferred the
    obligation onto a new timer. The old timer must not chase the driver."""
    fired = _timer("timer_1", status=TimerStatus.FIRED)
    obligation = _obligation("timer_1").defer(until=_at(35), now=_at(30), policy=DeferralPolicy())
    replacement = Timer(
        timer_id="timer_2",
        load_id="481207",
        obligation_id="obl_1",
        due_at=_at(37),
        created_at=_at(31),
    )
    event = _timer_due("timer_1")
    async with store.unit_of_work() as uow:
        await uow.loads.save(load)
        await uow.events.append(event)
        await uow.timers.save(fired)
        await uow.timers.save(replacement)
        await uow.obligations.save(obligation.attach_timer("timer_2"))
        await uow.commit()

    async with store.unit_of_work() as uow:
        ctx = await load_run_context(
            cast(UnitOfWork, uow), event, now=_at(31), recent_limit=30, outbound_window_minutes=60
        )

    assert ctx.event_timer == fired
    _skip(SkipReason.STALE_TIMER, prefilter(ctx))


@pytest.mark.unit
def test_stale_timer_is_checked_before_message_rules(load: Load) -> None:
    """Message rules never apply to a timer event, even one with an empty body and no sender."""
    event = _timer_due("timer_gone")
    assert event.body == "" and event.sender_contact_id is None

    _skip(SkipReason.STALE_TIMER, prefilter(_ctx(load, event)))


# --- empty message -------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("body", ["", "   ", "\n\t "])
def test_an_empty_message_without_attachments_is_skipped(load: Load, body: str) -> None:
    _skip(SkipReason.EMPTY_MESSAGE, prefilter(_ctx(load, _message(body=body))))


@pytest.mark.unit
def test_an_empty_message_is_skipped_before_the_sender_is_checked(load: Load) -> None:
    _skip(SkipReason.EMPTY_MESSAGE, prefilter(_ctx(load, _message(body=" ", sender="stranger"))))


# --- unknown sender ------------------------------------------------------------------------


def _escalate(reason: EscalationReason, result: PrefilterResult | None) -> PrefilterResult:
    assert result is not None
    assert result.escalate is reason
    assert result.skip is None
    assert result.detail
    return result


@pytest.mark.unit
@pytest.mark.parametrize(
    ("sender", "fragment"),
    [(None, "could not be matched"), ("dsp_other", "dsp_other")],
    ids=["unresolved", "not_on_this_load"],
)
def test_a_message_from_an_unknown_sender_escalates(
    load: Load, sender: str | None, fragment: str
) -> None:
    result = _escalate(
        EscalationReason.UNKNOWN_SENDER, prefilter(_ctx(load, _message(sender=sender)))
    )

    assert fragment in result.detail


@pytest.mark.unit
def test_the_sender_must_be_a_contact_of_this_load(load: Load) -> None:
    """A driver of another load is a stranger here: contacts are per load."""
    without_driver = load.model_copy(
        update={"contacts": tuple(c for c in load.contacts if c.id != "drv_20931")}
    )

    result = _escalate(
        EscalationReason.UNKNOWN_SENDER,
        prefilter(_ctx(without_driver, _message(sender="drv_20931"))),
    )

    assert "481207" in result.detail
