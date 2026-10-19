"""Properties of the obligation ledger and follow-up timing over arbitrary operation sequences.

Times are aware datetimes in a mix of fixed-offset and DST zones, so the sequences
cross clock changes. Durations are checked on UTC instants: a wait is real time, not
the distance between two wall-clock readings.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from freight_agent.domain.errors import InvariantViolation
from freight_agent.domain.obligations import (
    CLOSED_STATUSES,
    DeferralPolicy,
    Obligation,
    ObligationKind,
    ObligationStatus,
)
from freight_agent.domain.timers import follow_up_due_at

LA = ZoneInfo("America/Los_Angeles")
NY = ZoneInfo("America/New_York")
LORD_HOWE = ZoneInfo("Australia/Lord_Howe")  # a 30-minute DST shift
ZONES = (UTC, timezone(timedelta(hours=-5)), timezone(timedelta(hours=5, minutes=30)), LA, NY)
# Wall times inside a spring-forward gap or a fall-back fold. Hypothesis aims at clock
# changes only now and then; these make most histories cross one.
CLOCK_CHANGES = (
    datetime(2026, 3, 8, 2, 30, tzinfo=LA),
    datetime(2026, 11, 1, 1, 30, tzinfo=LA),
    datetime(2026, 11, 1, 1, 30, tzinfo=NY),
    datetime(2026, 4, 5, 1, 45, tzinfo=LORD_HOWE),
    datetime(2026, 10, 4, 2, 15, tzinfo=LORD_HOWE),
)


def _seconds(low: int, high: int) -> st.SearchStrategy[timedelta]:
    return st.integers(low, high).map(lambda seconds: timedelta(seconds=seconds))


offsets = _seconds(-90 * 60, 90 * 60)
# Around the policy's longest wait (30 minutes at most), including the past and now.
waits = _seconds(-120, 40 * 60)
moments = st.one_of(
    st.datetimes(
        min_value=datetime(2000, 1, 1),
        max_value=datetime(2100, 1, 1),
        timezones=st.sampled_from(ZONES),
    ),
    st.builds(
        lambda change, offset, fold: (change + offset).replace(fold=fold),
        st.sampled_from(CLOCK_CHANGES),
        offsets,
        st.integers(0, 1),
    ),
)
policies = st.builds(
    DeferralPolicy,
    max_minutes=st.integers(0, 30),
    max_per_obligation=st.integers(0, 3),
    grace_minutes=st.integers(0, 5),
)


def _instant(moment: datetime) -> datetime:
    return moment.astimezone(UTC)


@st.composite
def nearby(
    draw: st.DrawFn, start: datetime, within: st.SearchStrategy[timedelta] = offsets
) -> datetime:
    """A time near `start`, reached by wall-clock or by real-time arithmetic."""
    offset = draw(within)
    if draw(st.booleans()):
        return start + offset
    return (_instant(start) + offset).astimezone(start.tzinfo)


@dataclass(frozen=True)
class Attempt:
    at: datetime


@dataclass(frozen=True)
class Defer:
    now: datetime
    until: datetime


@dataclass(frozen=True)
class AttachTimer:
    timer_id: str


@dataclass(frozen=True)
class Close:
    status: ObligationStatus


Operation = Attempt | Defer | AttachTimer | Close


@st.composite
def operations(draw: st.DrawFn, start: datetime) -> Operation:
    kind = draw(st.sampled_from(["attempt", "defer", "timer", "close"]))
    if kind == "attempt":
        return Attempt(at=draw(nearby(start)))
    if kind == "defer":
        now = draw(nearby(start))
        return Defer(now=now, until=draw(nearby(now, waits)))
    if kind == "timer":
        return AttachTimer(timer_id=f"timer_{draw(st.integers(1, 5))}")
    return Close(status=draw(st.sampled_from(ObligationStatus)))


@st.composite
def histories(draw: st.DrawFn) -> tuple[Obligation, DeferralPolicy, list[Operation]]:
    start = draw(moments)
    obligation = Obligation.open(
        obligation_id="obl_1",
        load_id="481207",
        kind=ObligationKind.ETA_REQUEST,
        at=start,
        first_attempt=draw(st.booleans()),
    )
    return obligation, draw(policies), draw(st.lists(operations(start), max_size=12))


def _apply(obligation: Obligation, operation: Operation, policy: DeferralPolicy) -> Obligation:
    match operation:
        case Attempt(at=at):
            return obligation.record_attempt(at)
        case Defer(now=now, until=until):
            return obligation.defer(until=until, now=now, policy=policy)
        case AttachTimer(timer_id=timer_id):
            return obligation.attach_timer(timer_id)
        case Close(status=status):
            return obligation.close(status)


def _replay(
    obligation: Obligation, policy: DeferralPolicy, history: list[Operation]
) -> list[tuple[Obligation, Operation, Obligation | None]]:
    """Each step as (before, operation, after); `after` is None when the ledger refused it."""
    steps: list[tuple[Obligation, Operation, Obligation | None]] = []
    for operation in history:
        try:
            after = _apply(obligation, operation, policy)
        except InvariantViolation:
            after = None
        steps.append((obligation, operation, after))
        obligation = after or obligation
    return steps


@pytest.mark.property
@given(histories())
def test_attempts_never_decrease(case: tuple[Obligation, DeferralPolicy, list[Operation]]) -> None:
    obligation, policy, history = case

    for before, operation, after in _replay(obligation, policy, history):
        if after is None:
            continue
        expected = before.attempts + (1 if isinstance(operation, Attempt) else 0)
        assert after.attempts == expected


@pytest.mark.property
@given(histories())
def test_a_closed_obligation_accepts_no_further_change(
    case: tuple[Obligation, DeferralPolicy, list[Operation]],
) -> None:
    obligation, policy, history = case

    for before, operation, after in _replay(obligation, policy, history):
        if not before.is_open:
            assert after is None
        elif isinstance(operation, Close):
            assert (after is not None) == (operation.status in CLOSED_STATUSES)

    closed = obligation.close(ObligationStatus.SATISFIED)
    assert all(after is None for _, _, after in _replay(closed, policy, history))


@pytest.mark.property
@given(histories())
def test_deferrals_never_exceed_the_policy(
    case: tuple[Obligation, DeferralPolicy, list[Operation]],
) -> None:
    obligation, policy, history = case
    longest = timedelta(minutes=policy.max_minutes)

    for before, operation, after in _replay(obligation, policy, history):
        if after is not None:
            assert after.deferrals_used <= policy.max_per_obligation
        if not isinstance(operation, Defer) or not before.is_open:
            continue
        wait = _instant(operation.until) - _instant(operation.now)
        allowed = before.deferrals_used < policy.max_per_obligation and (
            timedelta(0) < wait <= longest
        )
        assert (after is not None) == allowed
        if after is not None:
            assert after.deferred_until == operation.until
            assert after.deferrals_used == before.deferrals_used + 1


@pytest.mark.property
@given(now=moments, policy=policies, data=st.data())
def test_a_deferral_is_judged_on_the_real_wait(
    now: datetime, policy: DeferralPolicy, data: st.DataObject
) -> None:
    """One deferral from a fresh obligation, most often across a clock change."""
    until = data.draw(nearby(now, waits))
    obligation = Obligation.open(
        obligation_id="obl_1",
        load_id="481207",
        kind=ObligationKind.ETA_REQUEST,
        at=now,
        first_attempt=True,
    )
    wait = _instant(until) - _instant(now)
    allowed = policy.max_per_obligation >= 1 and (
        timedelta(0) < wait <= timedelta(minutes=policy.max_minutes)
    )

    try:
        obligation.defer(until=until, now=now, policy=policy)
    except InvariantViolation:
        assert not allowed
    else:
        assert allowed


@pytest.mark.property
@given(requested=moments, grace_minutes=st.integers(0, 10), data=st.data())
def test_the_follow_up_honours_the_cadence_and_the_promise(
    requested: datetime, grace_minutes: int, data: st.DataObject
) -> None:
    grace = timedelta(minutes=grace_minutes)
    deferred_until = data.draw(st.none() | nearby(requested))

    due = follow_up_due_at(requested=requested, deferred_until=deferred_until, grace=grace)

    assert _instant(due) >= _instant(requested)
    if deferred_until is None:
        assert due == requested
        return
    promise = _instant(deferred_until) + grace
    assert _instant(due) >= promise
    assert _instant(due) == max(_instant(requested), promise)
