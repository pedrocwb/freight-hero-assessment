import pytest

from freight_agent.domain.load import DelayRisk
from freight_agent.domain.ping_filter import PingFilterConfig, RiskTracker, observe

LOW, MED, HIGH = DelayRisk.LOW, DelayRisk.MEDIUM, DelayRisk.HIGH
CFG = PingFilterConfig(upgrade_confirmations=1, downgrade_confirmations=2)


@pytest.mark.unit
def test_same_band_emits_nothing() -> None:
    tracker, transition = observe(RiskTracker(band=LOW), LOW, CFG)

    assert transition is None
    assert tracker.band is LOW


@pytest.mark.unit
def test_upgrade_is_confirmed_by_a_single_ping() -> None:
    tracker, transition = observe(RiskTracker(band=LOW), HIGH, CFG)

    assert transition is not None
    assert (transition.previous, transition.current) == (LOW, HIGH)
    assert tracker.band is HIGH


@pytest.mark.unit
def test_downgrade_needs_consecutive_confirmations() -> None:
    tracker = RiskTracker(band=HIGH)

    tracker, first = observe(tracker, LOW, CFG)
    assert first is None
    assert tracker.band is HIGH

    tracker, second = observe(tracker, LOW, CFG)
    assert second is not None
    assert (second.previous, second.current) == (HIGH, LOW)
    assert tracker.band is LOW


@pytest.mark.unit
def test_a_ping_back_in_the_current_band_resets_a_pending_downgrade() -> None:
    tracker = RiskTracker(band=HIGH)
    tracker, _ = observe(tracker, LOW, CFG)
    tracker, _ = observe(tracker, HIGH, CFG)
    tracker, transition = observe(tracker, LOW, CFG)

    assert transition is None
    assert tracker.band is HIGH


@pytest.mark.unit
def test_flapping_between_bands_never_emits_a_downgrade() -> None:
    tracker = RiskTracker(band=HIGH)
    for risk in [LOW, MED, LOW, MED, LOW]:
        tracker, transition = observe(tracker, risk, CFG)
        assert transition is None
    assert tracker.band is HIGH


@pytest.mark.unit
def test_requires_positive_confirmation_counts() -> None:
    with pytest.raises(ValueError):
        PingFilterConfig(upgrade_confirmations=0, downgrade_confirmations=2)


# --- redeliveries ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_remembered_ping_id_is_seen() -> None:
    tracker = RiskTracker(band=LOW).remember("p1", keep=64)

    assert tracker.has_seen("p1")
    assert not tracker.has_seen("p2")
    assert not RiskTracker(band=LOW).has_seen("p1")


@pytest.mark.unit
def test_remember_keeps_the_newest_ping_ids_up_to_the_bound() -> None:
    tracker = RiskTracker(band=LOW)
    for ping_id in ("p1", "p2", "p3", "p4"):
        tracker = tracker.remember(ping_id, keep=3)

    assert tracker.recent_ping_ids == ("p2", "p3", "p4")
    assert not tracker.has_seen("p1")


@pytest.mark.unit
def test_remember_with_no_room_keeps_nothing() -> None:
    assert RiskTracker(band=LOW).remember("p1", keep=0).recent_ping_ids == ()


@pytest.mark.unit
@pytest.mark.parametrize(("start", "risk"), [(LOW, LOW), (LOW, HIGH), (HIGH, LOW)])
def test_observe_keeps_the_remembered_ping_ids(start: DelayRisk, risk: DelayRisk) -> None:
    """Counting and dedupe are separate: a transition resets the count, not the memory."""
    tracker = RiskTracker(band=start).remember("p1", keep=64)

    tracker, _ = observe(tracker, risk, CFG)

    assert tracker.recent_ping_ids == ("p1",)
