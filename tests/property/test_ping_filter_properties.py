"""Properties of the ping filter's hysteresis over arbitrary risk streams.

A "segment" is the run of pings since the last transition (or since the start): the
filter forgets everything it was counting when the band changes.
"""

from itertools import pairwise

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from freight_agent.domain.load import DelayRisk
from freight_agent.domain.ping_filter import (
    PingFilterConfig,
    RiskTracker,
    RiskTransition,
    observe,
)

RANK = {DelayRisk.LOW: 0, DelayRisk.MEDIUM: 1, DelayRisk.HIGH: 2}

risks = st.sampled_from(DelayRisk)
streams = st.lists(risks, max_size=40)
configs = st.builds(
    PingFilterConfig,
    upgrade_confirmations=st.integers(1, 4),
    downgrade_confirmations=st.integers(1, 4),
)

Step = tuple[DelayRisk, RiskTracker, RiskTransition | None]


def _run(band: DelayRisk, pings: list[DelayRisk], config: PingFilterConfig) -> list[Step]:
    """Each ping with the tracker and the transition it produced."""
    tracker = RiskTracker(band=band)
    steps: list[Step] = []
    for risk in pings:
        tracker, transition = observe(tracker, risk, config)
        steps.append((risk, tracker, transition))
    return steps


def _required(band: DelayRisk, risk: DelayRisk, config: PingFilterConfig) -> int:
    if RANK[risk] > RANK[band]:
        return config.upgrade_confirmations
    return config.downgrade_confirmations


def _trailing_run(segment: list[DelayRisk]) -> int:
    """How many pings at the end of the segment equal its last ping."""
    run = 0
    for risk in reversed(segment):
        if risk is not segment[-1]:
            break
        run += 1
    return run


def _segments(band: DelayRisk, steps: list[Step]) -> list[tuple[DelayRisk, list[DelayRisk]]]:
    """For each step: the band before the ping, and the segment up to and including it."""
    out: list[tuple[DelayRisk, list[DelayRisk]]] = []
    segment: list[DelayRisk] = []
    for risk, tracker, transition in steps:
        segment = [*segment, risk]
        out.append((band, segment))
        if transition is not None:
            band, segment = tracker.band, []
    return out


@pytest.mark.property
@given(risks, st.integers(0, 40), configs)
def test_a_stream_in_the_current_band_never_transitions(
    band: DelayRisk, length: int, config: PingFilterConfig
) -> None:
    for _, tracker, transition in _run(band, [band] * length, config):
        assert transition is None
        assert tracker == RiskTracker(band=band)


@pytest.mark.property
@given(risks, risks, st.integers(0, 40), configs)
def test_a_constant_stream_transitions_at_most_once(
    band: DelayRisk, risk: DelayRisk, length: int, config: PingFilterConfig
) -> None:
    steps = _run(band, [risk] * length, config)

    transitions = [t for _, _, t in steps if t is not None]
    assert len(transitions) <= 1
    if risk is not band and length >= _required(band, risk, config):
        assert transitions == [RiskTransition(previous=band, current=risk)]


@pytest.mark.property
@given(risks, streams, configs)
def test_transitions_chain_and_never_repeat_a_band(
    band: DelayRisk, pings: list[DelayRisk], config: PingFilterConfig
) -> None:
    transitions = [t for _, _, t in _run(band, pings, config) if t is not None]

    for transition in transitions:
        assert transition.previous is not transition.current
        assert transition.is_upgrade == (RANK[transition.current] > RANK[transition.previous])
    for first, second in pairwise(transitions):
        assert second.previous is first.current
        assert second.current is not first.current
    if transitions:
        assert transitions[0].previous is band


@pytest.mark.property
@given(risks, streams, configs)
def test_the_tracker_band_follows_the_transitions(
    band: DelayRisk, pings: list[DelayRisk], config: PingFilterConfig
) -> None:
    for _, tracker, transition in _run(band, pings, config):
        if transition is None:
            assert tracker.band is band
            continue
        assert tracker == RiskTracker(band=transition.current)
        band = tracker.band


@pytest.mark.property
@given(risks, streams, configs)
def test_a_transition_needs_exactly_its_confirmations_in_a_row(
    band: DelayRisk, pings: list[DelayRisk], config: PingFilterConfig
) -> None:
    steps = _run(band, pings, config)

    for (risk, _, transition), (before, segment) in zip(steps, _segments(band, steps), strict=True):
        if risk is before:
            assert transition is None
            continue
        required = _required(before, risk, config)
        # Fires on the ping that completes the run, never earlier and never later.
        assert (transition is not None) == (_trailing_run(segment) == required)
        if transition is not None:
            assert transition == RiskTransition(previous=before, current=risk)
            upgrade, downgrade = config.upgrade_confirmations, config.downgrade_confirmations
            assert required == (upgrade if transition.is_upgrade else downgrade)


@pytest.mark.property
@given(risks, streams, configs)
def test_the_pending_count_is_the_current_run_of_equal_pings(
    band: DelayRisk, pings: list[DelayRisk], config: PingFilterConfig
) -> None:
    steps = _run(band, pings, config)

    for (risk, tracker, transition), (before, segment) in zip(
        steps, _segments(band, steps), strict=True
    ):
        if transition is not None or risk is before:
            assert (tracker.pending, tracker.pending_count) == (None, 0)
        else:
            assert (tracker.pending, tracker.pending_count) == (risk, _trailing_run(segment))


@pytest.mark.property
@given(risks, risks, risks, configs)
def test_interleaving_a_different_value_resets_the_count(
    band: DelayRisk, risk: DelayRisk, other: DelayRisk, config: PingFilterConfig
) -> None:
    required = _required(band, risk, config)
    assume(risk is not band and other is not risk and required >= 2)
    # The interleaved ping must not move the band itself, or the count restarts for that reason.
    assume(other is band or _required(band, other, config) >= 2)
    almost = [risk] * (required - 1)

    steps = _run(band, [*almost, other, *almost], config)

    assert all(transition is None for _, _, transition in steps)
    assert steps[-1][1].band is band
    assert steps[-1][1].pending_count == required - 1
