"""The health report: `application.metrics` over every load of the simulation.

The metrics are pure functions of our own data, so health works with tracing off. `now`
is the simulation clock: queue age, stalled obligations and stuck deliveries are judged
in virtual time, which is the time the simulated agent lives in.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime

from freight_agent.api.schemas import HealthReport
from freight_agent.application.contracts import LoadSnapshot
from freight_agent.application.metrics import (
    cost_metrics,
    health_summary,
    outbox_metrics,
    queue_metrics,
    run_metrics,
    stalled_obligations,
    timer_lag_metrics,
)
from freight_agent.domain.events import Event


def health_report(
    snapshots: Sequence[LoadSnapshot],
    *,
    oldest_queued: Event | None,
    queue_size: int,
    dead_letters: Sequence[Event],
    now: datetime,
    settings: Mapping[str, str],
) -> HealthReport:
    """`oldest_queued` and `queue_size` stand in for the whole queue: the queue port gives
    the longest-waiting event, which is all the SLA check reads, and the count."""
    queued = [oldest_queued] if oldest_queued is not None else []
    queue = queue_metrics(queued, dead_letters, now=now).model_copy(update={"queued": queue_size})
    runs = [run for snapshot in snapshots for run in snapshot.runs]
    timers = [timer for snapshot in snapshots for timer in snapshot.timers]
    events = [event for snapshot in snapshots for event in snapshot.events]
    obligations = [o for snapshot in snapshots for o in snapshot.obligations]
    outbox = [item for snapshot in snapshots for item in snapshot.outbox]

    run_stats = run_metrics(runs)
    cost = cost_metrics(runs)
    timer_lag = timer_lag_metrics(timers, events)
    stalled = stalled_obligations(obligations, timers, now=now)
    delivery = outbox_metrics(outbox, now=now)
    summary = health_summary(
        queue=queue, runs=run_stats, cost=cost, timers=timer_lag, stalled=stalled, outbox=delivery
    )
    return HealthReport(
        status=summary.level,
        reasons=summary.reasons,
        queue=queue,
        runs=run_stats,
        cost=cost,
        outbox=delivery,
        timers=timer_lag,
        stalled=stalled,
        settings=dict(settings),
    )
