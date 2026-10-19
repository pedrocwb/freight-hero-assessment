from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from freight_agent.domain.obligations import Obligation, ObligationKind, ObligationStatus
from freight_agent.domain.timers import Timer, TimerStatus, follow_up_due_at, is_stale

LA = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 10, 13, 7, 1, tzinfo=LA)
GRACE = timedelta(minutes=2)


def _obligation(active_timer_id: str | None = "timer_1") -> Obligation:
    obligation = Obligation.open(
        obligation_id="obl_1",
        load_id="481207",
        kind=ObligationKind.ETA_REQUEST,
        at=T0,
        first_attempt=True,
    )
    return obligation if active_timer_id is None else obligation.attach_timer(active_timer_id)


def _timer(status: TimerStatus = TimerStatus.PENDING, timer_id: str = "timer_1") -> Timer:
    return Timer(
        timer_id=timer_id,
        load_id="481207",
        obligation_id="obl_1",
        due_at=T0 + timedelta(minutes=30),
        created_at=T0,
        status=status,
        reason="schedule_timer",
    )


@pytest.mark.unit
def test_mark_returns_a_new_timer_with_the_status() -> None:
    timer = _timer()

    fired = timer.mark(TimerStatus.FIRED)

    assert fired.status is TimerStatus.FIRED
    assert timer.status is TimerStatus.PENDING
    assert fired.model_copy(update={"status": TimerStatus.PENDING}) == timer


@pytest.mark.unit
def test_the_active_timer_of_an_open_obligation_is_fresh() -> None:
    assert not is_stale(_timer(), _obligation())


@pytest.mark.unit
def test_a_fired_timer_that_is_still_active_may_act() -> None:
    # The scheduler marks a timer fired when it queues the timer_due event;
    # that event's run must still get through the prefilter.
    assert not is_stale(_timer(TimerStatus.FIRED), _obligation())


@pytest.mark.unit
@pytest.mark.parametrize("status", [TimerStatus.SUPERSEDED, TimerStatus.CANCELLED])
def test_a_superseded_or_cancelled_timer_is_stale_even_if_still_active(
    status: TimerStatus,
) -> None:
    assert is_stale(_timer(status), _obligation())


@pytest.mark.unit
def test_a_timer_without_its_obligation_is_stale() -> None:
    assert is_stale(_timer(), None)


@pytest.mark.unit
@pytest.mark.parametrize(
    "status",
    [ObligationStatus.SATISFIED, ObligationStatus.EXHAUSTED, ObligationStatus.CANCELLED],
)
def test_a_timer_of_a_closed_obligation_is_stale(status: ObligationStatus) -> None:
    assert is_stale(_timer(), _obligation().close(status))


@pytest.mark.unit
@pytest.mark.parametrize("active_timer_id", ["timer_2", None], ids=["replaced", "no_active"])
def test_a_timer_that_is_not_the_active_one_is_stale(active_timer_id: str | None) -> None:
    assert is_stale(_timer(), _obligation(active_timer_id))


@pytest.mark.unit
def test_without_a_deferral_the_follow_up_is_due_when_requested() -> None:
    requested = T0 + timedelta(minutes=30)

    assert follow_up_due_at(requested=requested, deferred_until=None, grace=GRACE) == requested


@pytest.mark.unit
@pytest.mark.parametrize(
    "deferred_until",
    [T0 + timedelta(minutes=10), T0 + timedelta(minutes=28)],
    ids=["well_before", "grace_ends_exactly_at_requested"],
)
def test_a_deferral_that_ends_before_the_requested_time_changes_nothing(
    deferred_until: datetime,
) -> None:
    requested = T0 + timedelta(minutes=30)

    due = follow_up_due_at(requested=requested, deferred_until=deferred_until, grace=GRACE)

    assert due == requested


@pytest.mark.unit
def test_a_deferral_past_the_requested_time_waits_for_the_promise_plus_grace() -> None:
    requested = T0 + timedelta(minutes=30)
    deferred_until = T0 + timedelta(minutes=35)

    due = follow_up_due_at(requested=requested, deferred_until=deferred_until, grace=GRACE)

    assert due == T0 + timedelta(minutes=37)
    assert due.tzinfo is LA


@pytest.mark.unit
def test_the_follow_up_is_never_earlier_than_requested_in_the_repeated_hour() -> None:
    # 01:40 PST (second pass of the hour) is 09:40 UTC; 01:48 PDT is 08:48 UTC.
    requested = datetime(2026, 11, 1, 1, 40, fold=1, tzinfo=LA)
    deferred_until = datetime(2026, 11, 1, 1, 48, tzinfo=LA)

    due = follow_up_due_at(requested=requested, deferred_until=deferred_until, grace=GRACE)

    assert due.astimezone(UTC) == requested.astimezone(UTC)


@pytest.mark.unit
def test_the_grace_is_real_time_when_the_clocks_go_back() -> None:
    requested = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
    deferred_until = datetime(2026, 11, 1, 1, 59, tzinfo=LA)  # PDT, 08:59 UTC

    due = follow_up_due_at(requested=requested, deferred_until=deferred_until, grace=GRACE)

    assert due.astimezone(UTC) == datetime(2026, 11, 1, 9, 1, tzinfo=UTC)
    assert due.tzinfo is LA
