from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from freight_agent.domain.errors import InvariantViolation
from freight_agent.domain.obligations import (
    CLOSED_STATUSES,
    DeferralPolicy,
    Obligation,
    ObligationKind,
    ObligationStatus,
)

LA = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 10, 13, 7, 1, tzinfo=LA)
POLICY = DeferralPolicy(max_minutes=15, max_per_obligation=1, grace_minutes=2)
CLOSING = sorted(CLOSED_STATUSES)

# 2026-11-01 in Los Angeles: at 02:00 PDT the clocks go back to 01:00 PST, so the
# wall times from 01:00 to 01:59 happen twice (fold=0 is PDT, fold=1 is PST).
BEFORE_FALL_BACK = datetime(2026, 11, 1, 1, 55, tzinfo=LA)  # 08:55 UTC


def _open(*, first_attempt: bool = True, at: datetime = T0) -> Obligation:
    return Obligation.open(
        obligation_id="obl_1",
        load_id="481207",
        kind=ObligationKind.ETA_REQUEST,
        at=at,
        first_attempt=first_attempt,
        sop_version="sop_v1",
    )


@pytest.mark.unit
def test_open_with_a_first_attempt_counts_it() -> None:
    obligation = _open(first_attempt=True)

    assert obligation.is_open
    assert obligation.status is ObligationStatus.OPEN
    assert obligation.attempts == 1
    assert obligation.opened_at == T0
    assert obligation.last_action_at == T0
    assert obligation.sop_version == "sop_v1"
    assert (obligation.deferred_until, obligation.deferrals_used) == (None, 0)
    assert obligation.active_timer_id is None


@pytest.mark.unit
def test_open_without_a_first_attempt_has_nothing_to_count_yet() -> None:
    obligation = _open(first_attempt=False)

    assert obligation.is_open
    assert obligation.attempts == 0
    assert obligation.opened_at == T0
    assert obligation.last_action_at is None


@pytest.mark.unit
def test_record_attempt_counts_one_more_and_stamps_the_time() -> None:
    original = _open()
    later = T0 + timedelta(minutes=30)

    updated = original.record_attempt(later)

    assert (updated.attempts, updated.last_action_at) == (2, later)
    assert (original.attempts, original.last_action_at) == (1, T0)


@pytest.mark.unit
@pytest.mark.parametrize("minutes", [1, 5, 15])
def test_defer_within_the_policy_records_the_promise(minutes: int) -> None:
    until = T0 + timedelta(minutes=minutes)

    deferred = _open().defer(until=until, now=T0, policy=POLICY)

    assert deferred.deferred_until == until
    assert deferred.deferrals_used == 1
    assert deferred.is_open


@pytest.mark.unit
def test_defer_over_the_count_limit_is_refused() -> None:
    once = _open().defer(until=T0 + timedelta(minutes=5), now=T0, policy=POLICY)

    with pytest.raises(InvariantViolation, match="deferral limit"):
        once.defer(until=T0 + timedelta(minutes=10), now=T0, policy=POLICY)


@pytest.mark.unit
def test_a_policy_with_no_deferrals_refuses_the_first_one() -> None:
    policy = DeferralPolicy(max_per_obligation=0)

    with pytest.raises(InvariantViolation, match="deferral limit"):
        _open().defer(until=T0 + timedelta(minutes=5), now=T0, policy=policy)


@pytest.mark.unit
@pytest.mark.parametrize("offset", [timedelta(0), timedelta(minutes=-1)], ids=["now", "past"])
def test_defer_must_end_in_the_future(offset: timedelta) -> None:
    with pytest.raises(InvariantViolation, match="future"):
        _open().defer(until=T0 + offset, now=T0, policy=POLICY)


@pytest.mark.unit
@pytest.mark.parametrize("over", [timedelta(seconds=1), timedelta(minutes=1)])
def test_defer_longer_than_max_minutes_is_refused(over: timedelta) -> None:
    until = T0 + timedelta(minutes=POLICY.max_minutes) + over

    with pytest.raises(InvariantViolation, match="longer than 15 minutes"):
        _open().defer(until=until, now=T0, policy=POLICY)


@pytest.mark.unit
def test_defer_measures_real_time_when_the_clocks_go_back() -> None:
    # Wall-clock 01:55 PDT + 15 min reads 02:10 PST: 75 real minutes later.
    until = BEFORE_FALL_BACK + timedelta(minutes=15)
    assert until.astimezone(UTC) - BEFORE_FALL_BACK.astimezone(UTC) == timedelta(minutes=75)

    with pytest.raises(InvariantViolation, match="longer than"):
        _open().defer(until=until, now=BEFORE_FALL_BACK, policy=POLICY)


@pytest.mark.unit
def test_defer_into_the_repeated_hour_counts_the_real_wait() -> None:
    now = datetime(2026, 11, 1, 1, 50, tzinfo=LA)  # PDT, 08:50 UTC
    until = datetime(2026, 11, 1, 1, 0, fold=1, tzinfo=LA)  # PST, 09:00 UTC

    deferred = _open().defer(until=until, now=now, policy=POLICY)

    assert deferred.deferred_until == until


@pytest.mark.unit
def test_attach_timer_sets_and_replaces_the_active_timer() -> None:
    first = _open().attach_timer("timer_1")
    second = first.attach_timer("timer_2")

    assert first.active_timer_id == "timer_1"
    assert second.active_timer_id == "timer_2"


@pytest.mark.unit
@pytest.mark.parametrize("status", CLOSING)
def test_close_with_a_closing_status(status: ObligationStatus) -> None:
    obligation = (
        _open()
        .attach_timer("timer_1")
        .defer(until=T0 + timedelta(minutes=5), now=T0, policy=POLICY)
    )

    closed = obligation.close(status)

    assert closed.status is status
    assert not closed.is_open
    assert closed.active_timer_id is None
    assert closed.deferred_until is None
    assert (closed.attempts, closed.deferrals_used) == (1, 1)


@pytest.mark.unit
def test_close_with_open_is_not_a_closing_status() -> None:
    with pytest.raises(InvariantViolation, match="not a closing status"):
        _open().close(ObligationStatus.OPEN)


_OPERATIONS: dict[str, Callable[[Obligation], Obligation]] = {
    "record_attempt": lambda o: o.record_attempt(T0 + timedelta(minutes=30)),
    "defer": lambda o: o.defer(until=T0 + timedelta(minutes=5), now=T0, policy=POLICY),
    "attach_timer": lambda o: o.attach_timer("timer_9"),
    **{f"close_{s}": (lambda o, s=s: o.close(s)) for s in ObligationStatus},
}


@pytest.mark.unit
@pytest.mark.parametrize("status", CLOSING)
@pytest.mark.parametrize("operation", list(_OPERATIONS))
def test_every_operation_on_a_closed_obligation_is_refused(
    status: ObligationStatus, operation: str
) -> None:
    closed = _open().close(status)

    with pytest.raises(InvariantViolation, match=f"on a {status} obligation"):
        _OPERATIONS[operation](closed)
