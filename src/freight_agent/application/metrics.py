"""Operational metrics: pure functions over domain objects.

The source of truth for monitoring is our own data (runs, events, ledger, timers,
outbox), not the tracing backend, so health keeps working when tracing is off or down.
Nothing here does I/O: the API reads the repositories and passes the objects in, and
`now` is always an argument, so every number is reproducible in tests.

Durations are measured on UTC instants: Python subtracts two times of the same zone by
wall clock, which is an hour off across a DST change.
"""

import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from freight_agent.domain.events import Event, EventType
from freight_agent.domain.obligations import Obligation, ObligationKind
from freight_agent.domain.outbox import OutboxItem, OutboxStatus
from freight_agent.domain.runs import RunRecord, RunStatus
from freight_agent.domain.timers import Timer, TimerStatus
from freight_agent.domain.validation import Severity

QUEUE_SLA = timedelta(minutes=5)
LOAD_BUDGET_USD = 1.0
BUDGET_WARN_FRACTION = 0.8
# The longest SOP wait (30 minutes between follow-ups) plus a margin: an open obligation
# idle longer than this, with no timer to wake it, has fallen out of its procedure.
STALL_THRESHOLD = timedelta(minutes=45)
OUTBOX_STUCK_AFTER = timedelta(minutes=5)


class _Metric(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _seconds(start: datetime, end: datetime) -> float:
    return (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds()


def _ratio(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def percentile(values: Sequence[float], q: float) -> float | None:
    """The `q` quantile (0..1) with linear interpolation between ranks; None when empty."""
    if not 0.0 <= q <= 1.0:
        raise ValueError("the percentile fraction must be between 0 and 1")
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


# --- queue ---------------------------------------------------------------------


class QueueMetrics(_Metric):
    queued: int
    oldest_event_id: str | None
    oldest_age_s: float | None
    sla_s: float
    over_sla: bool
    dead_letters: int


def queue_metrics(
    queued: Sequence[Event],
    dead_letters: Sequence[Event],
    *,
    now: datetime,
    sla: timedelta = QUEUE_SLA,
) -> QueueMetrics:
    """Age of the oldest queued event against the SLA.

    Age counts from the event's `occurred_at`, so a slow integration layer shows up too:
    the SLA is about how long a driver waits for the agent, not how long a row sat queued.
    """
    oldest = min(queued, key=lambda e: e.occurred_at.astimezone(UTC), default=None)
    age = None if oldest is None else _seconds(oldest.occurred_at, now)
    return QueueMetrics(
        queued=len(queued),
        oldest_event_id=None if oldest is None else oldest.event_id,
        oldest_age_s=age,
        sla_s=sla.total_seconds(),
        over_sla=age is not None and age > sla.total_seconds(),
        dead_letters=len(dead_letters),
    )


# --- runs ----------------------------------------------------------------------


class ModelErrors(_Metric):
    model: str
    calls: int  # successful calls (usage records) plus failed runs that name the model
    errors: int
    error_rate: float


class RunMetrics(_Metric):
    total: int
    finished: int
    duration_p50_s: float | None
    duration_p95_s: float | None
    status_counts: dict[RunStatus, int]
    status_rates: dict[RunStatus, float]
    planned: int  # runs that produced a plan, so reached the validator
    blocked_runs: int  # planned runs with at least one violation
    high_severity_runs: int
    block_rate: float  # blocked_runs / planned
    models: tuple[ModelErrors, ...]


def _names_model(error: str, model: str) -> bool:
    """True when the error text names the model as a whole id ("gpt-4o" is not in
    "gpt-4o-mini"). Fallback chain errors read "<model>: <reason>; ..."."""
    return re.search(rf"(?<![\w./-]){re.escape(model)}(?![\w./-])", error) is not None


def _model_errors(
    runs: Sequence[RunRecord], known_models: Iterable[str]
) -> tuple[ModelErrors, ...]:
    """Usage records are successful calls; a failed call leaves no usage, only the run's
    error text. So an error counts against every known model it names, once per run.
    Errors that name no model (a bug, a bad event) are not model errors: they show in the
    failed status rate instead."""
    successes = Counter(u.model for run in runs for u in run.usage)
    models = sorted(set(successes) | set(known_models))
    errors = Counter(
        model
        for run in runs
        if run.error is not None
        for model in models
        if _names_model(run.error, model)
    )
    return tuple(
        ModelErrors(
            model=model,
            calls=successes[model] + errors[model],
            errors=errors[model],
            error_rate=_ratio(errors[model], successes[model] + errors[model]),
        )
        for model in models
    )


def run_metrics(runs: Sequence[RunRecord], *, known_models: Iterable[str] = ()) -> RunMetrics:
    """Duration percentiles, status mix, validator blocks and model error rates.

    `known_models` adds configured models that may have no successful call yet, so a
    model that only ever fails still shows up with a 100% error rate.
    """
    durations = [
        _seconds(run.started_at, run.finished_at) for run in runs if run.finished_at is not None
    ]
    counts = Counter(run.status for run in runs)
    planned = [run for run in runs if run.plan is not None]
    blocked = [run for run in planned if run.violations]
    high = [run for run in runs if any(v.severity is Severity.HIGH for v in run.violations)]
    return RunMetrics(
        total=len(runs),
        finished=len(durations),
        duration_p50_s=percentile(durations, 0.5),
        duration_p95_s=percentile(durations, 0.95),
        status_counts={status: counts[status] for status in RunStatus},
        status_rates={status: _ratio(counts[status], len(runs)) for status in RunStatus},
        planned=len(planned),
        blocked_runs=len(blocked),
        high_severity_runs=len(high),
        block_rate=_ratio(len(blocked), len(planned)),
        models=_model_errors(runs, known_models),
    )


# --- cost ----------------------------------------------------------------------


class LoadCost(_Metric):
    load_id: str
    runs: int
    cost_usd: float
    budget_used: float  # cost / budget; 1.0 is the whole budget
    over_warning: bool
    over_budget: bool


class CostMetrics(_Metric):
    budget_usd: float
    total_cost_usd: float
    avg_cost_per_run_usd: float
    loads: tuple[LoadCost, ...]  # most expensive first


def cost_metrics(
    runs: Sequence[RunRecord],
    *,
    budget_usd: float = LOAD_BUDGET_USD,
    warn_fraction: float = BUDGET_WARN_FRACTION,
) -> CostMetrics:
    """Model spend per load against the per-load budget. The average is over every run,
    skipped ones included, since that is what one more event costs on average."""
    spent: defaultdict[str, float] = defaultdict(float)
    run_count: Counter[str] = Counter()
    for run in runs:
        spent[run.load_id] += run.cost_usd
        run_count[run.load_id] += 1
    loads = [
        LoadCost(
            load_id=load_id,
            runs=run_count[load_id],
            cost_usd=cost,
            budget_used=cost / budget_usd,
            over_warning=cost > budget_usd * warn_fraction,
            over_budget=cost > budget_usd,
        )
        for load_id, cost in spent.items()
    ]
    total = sum(run.cost_usd for run in runs)
    return CostMetrics(
        budget_usd=budget_usd,
        total_cost_usd=total,
        avg_cost_per_run_usd=total / len(runs) if runs else 0.0,
        loads=tuple(sorted(loads, key=lambda load: (-load.cost_usd, load.load_id))),
    )


# --- timers --------------------------------------------------------------------


class TimerLagMetrics(_Metric):
    fired: int  # TIMER_DUE events seen
    measured: int  # of those, the ones whose timer is known
    lag_p50_s: float | None
    lag_p95_s: float | None
    lag_max_s: float | None


def timer_lag_metrics(timers: Sequence[Timer], events: Sequence[Event]) -> TimerLagMetrics:
    """How late follow-ups fire: each TIMER_DUE event's `occurred_at` against its timer's
    `due_at`. The scheduler must stamp the event with the time it fired for this to show
    anything; an event without a known timer is counted but not measured."""
    due_at = {timer.timer_id: timer.due_at for timer in timers}
    fired = [event for event in events if event.type is EventType.TIMER_DUE]
    lags = [
        _seconds(due_at[timer_id], event.occurred_at)
        for event in fired
        if (timer_id := event.data.get("timer_id")) in due_at
    ]
    return TimerLagMetrics(
        fired=len(fired),
        measured=len(lags),
        lag_p50_s=percentile(lags, 0.5),
        lag_p95_s=percentile(lags, 0.95),
        lag_max_s=max(lags, default=None),
    )


# --- stalled obligations -------------------------------------------------------


class StalledObligation(_Metric):
    obligation_id: str
    load_id: str
    kind: ObligationKind
    idle_since: datetime
    idle_s: float


def stalled_obligations(
    obligations: Sequence[Obligation],
    timers: Sequence[Timer],
    *,
    now: datetime,
    threshold: timedelta = STALL_THRESHOLD,
) -> tuple[StalledObligation, ...]:
    """Open obligations idle past the threshold with no pending timer to wake them.

    Idle counts from the last attempt, or from the opening when there was none. Such an
    obligation will never move on its own: a lost timer or a run that died mid-procedure.
    """
    waiting = {timer.obligation_id for timer in timers if timer.status is TimerStatus.PENDING}
    stalled: list[StalledObligation] = []
    for obligation in obligations:
        if not obligation.is_open or obligation.obligation_id in waiting:
            continue
        since = obligation.last_action_at or obligation.opened_at
        idle = _seconds(since, now)
        if idle > threshold.total_seconds():
            stalled.append(
                StalledObligation(
                    obligation_id=obligation.obligation_id,
                    load_id=obligation.load_id,
                    kind=obligation.kind,
                    idle_since=since,
                    idle_s=idle,
                )
            )
    return tuple(stalled)


# --- outbox --------------------------------------------------------------------


class OutboxMetrics(_Metric):
    pending: int
    failed: int
    stuck: int  # pending for longer than the threshold: delivery is not running
    oldest_pending_age_s: float | None
    failed_ids: tuple[str, ...]
    stuck_ids: tuple[str, ...]


def outbox_metrics(
    items: Sequence[OutboxItem], *, now: datetime, stuck_after: timedelta = OUTBOX_STUCK_AFTER
) -> OutboxMetrics:
    pending = [item for item in items if item.status is OutboxStatus.PENDING]
    failed = [item for item in items if item.status is OutboxStatus.FAILED]
    ages = {item.outbox_id: _seconds(item.created_at, now) for item in pending}
    stuck = [oid for oid, age in ages.items() if age > stuck_after.total_seconds()]
    return OutboxMetrics(
        pending=len(pending),
        failed=len(failed),
        stuck=len(stuck),
        oldest_pending_age_s=max(ages.values(), default=None),
        failed_ids=tuple(item.outbox_id for item in failed),
        stuck_ids=tuple(stuck),
    )


# --- health --------------------------------------------------------------------


class HealthLevel(StrEnum):
    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"


_RANK = {HealthLevel.OK: 0, HealthLevel.WARN: 1, HealthLevel.CRITICAL: 2}


class HealthReason(_Metric):
    check: str
    level: HealthLevel
    message: str


class HealthSummary(_Metric):
    level: HealthLevel
    reasons: tuple[HealthReason, ...]  # most severe first


class HealthThresholds(_Metric):
    """Every limit is exclusive: a value equal to it is still healthy."""

    failed_rate_warn: float = 0.05
    failed_rate_critical: float = 0.20
    escalation_rate_warn: float = 0.30
    run_p95_warn_s: float = 60.0
    model_error_rate_warn: float = 0.20
    model_error_rate_critical: float = 0.50
    timer_lag_warn_s: float = 60.0
    timer_lag_critical_s: float = 300.0


def _queue_reasons(queue: QueueMetrics) -> list[HealthReason]:
    reasons: list[HealthReason] = []
    if queue.over_sla:
        reasons.append(
            HealthReason(
                check="queue_sla",
                level=HealthLevel.CRITICAL,
                message=f"oldest queued event {queue.oldest_event_id} waited "
                f"{queue.oldest_age_s:.0f}s, over the {queue.sla_s:.0f}s SLA",
            )
        )
    if queue.dead_letters:
        reasons.append(
            HealthReason(
                check="dead_letters",
                level=HealthLevel.WARN,
                message=f"{queue.dead_letters} event(s) in the dead-letter queue",
            )
        )
    return reasons


def _graded(value: float, warn: float, critical: float) -> HealthLevel:
    if value > critical:
        return HealthLevel.CRITICAL
    if value > warn:
        return HealthLevel.WARN
    return HealthLevel.OK


def _run_reasons(runs: RunMetrics, t: HealthThresholds) -> list[HealthReason]:
    reasons: list[HealthReason] = []
    failed = runs.status_rates[RunStatus.FAILED]
    level = _graded(failed, t.failed_rate_warn, t.failed_rate_critical)
    if level is not HealthLevel.OK:
        reasons.append(
            HealthReason(check="failed_runs", level=level, message=f"{failed:.0%} of runs failed")
        )
    escalated = runs.status_rates[RunStatus.ESCALATED]
    if escalated > t.escalation_rate_warn:
        reasons.append(
            HealthReason(
                check="escalation_rate",
                level=HealthLevel.WARN,
                message=f"{escalated:.0%} of runs escalated to a Hero",
            )
        )
    if runs.duration_p95_s is not None and runs.duration_p95_s > t.run_p95_warn_s:
        reasons.append(
            HealthReason(
                check="run_duration",
                level=HealthLevel.WARN,
                message=f"run duration p95 is {runs.duration_p95_s:.0f}s",
            )
        )
    if runs.high_severity_runs:
        reasons.append(
            HealthReason(
                check="validator_high",
                level=HealthLevel.WARN,
                message=f"{runs.high_severity_runs} run(s) blocked by a high-severity rule",
            )
        )
    for model in runs.models:
        level = _graded(model.error_rate, t.model_error_rate_warn, t.model_error_rate_critical)
        if level is not HealthLevel.OK:
            reasons.append(
                HealthReason(
                    check="model_errors",
                    level=level,
                    message=f"{model.model} failed {model.errors} of {model.calls} calls",
                )
            )
    return reasons


def _cost_reasons(cost: CostMetrics) -> list[HealthReason]:
    reasons: list[HealthReason] = []
    over = [load.load_id for load in cost.loads if load.over_budget]
    near = [load.load_id for load in cost.loads if load.over_warning and not load.over_budget]
    if over:
        reasons.append(
            HealthReason(
                check="load_budget",
                level=HealthLevel.CRITICAL,
                message=f"over the ${cost.budget_usd:.2f} budget: {', '.join(over)}",
            )
        )
    if near:
        reasons.append(
            HealthReason(
                check="load_budget_warning",
                level=HealthLevel.WARN,
                message=f"near the ${cost.budget_usd:.2f} budget: {', '.join(near)}",
            )
        )
    return reasons


def _timer_reasons(timers: TimerLagMetrics, t: HealthThresholds) -> list[HealthReason]:
    if timers.lag_max_s is None:
        return []
    level = _graded(timers.lag_max_s, t.timer_lag_warn_s, t.timer_lag_critical_s)
    if level is HealthLevel.OK:
        return []
    message = f"a follow-up timer fired {timers.lag_max_s:.0f}s late"
    return [HealthReason(check="timer_lag", level=level, message=message)]


def _stalled_reasons(stalled: Sequence[StalledObligation]) -> list[HealthReason]:
    if not stalled:
        return []
    ids = ", ".join(s.obligation_id for s in stalled)
    message = f"{len(stalled)} open obligation(s) idle with no timer: {ids}"
    return [HealthReason(check="stalled_obligations", level=HealthLevel.WARN, message=message)]


def _outbox_reasons(outbox: OutboxMetrics) -> list[HealthReason]:
    reasons: list[HealthReason] = []
    if outbox.stuck:
        reasons.append(
            HealthReason(
                check="outbox_stuck",
                level=HealthLevel.CRITICAL,
                message=f"{outbox.stuck} outbox item(s) pending for too long: delivery is "
                "not keeping up",
            )
        )
    if outbox.failed:
        reasons.append(
            HealthReason(
                check="outbox_failed",
                level=HealthLevel.WARN,
                message=f"{outbox.failed} outbox item(s) failed: {', '.join(outbox.failed_ids)}",
            )
        )
    return reasons


def health_summary(
    *,
    queue: QueueMetrics | None = None,
    runs: RunMetrics | None = None,
    cost: CostMetrics | None = None,
    timers: TimerLagMetrics | None = None,
    stalled: Sequence[StalledObligation] = (),
    outbox: OutboxMetrics | None = None,
    thresholds: HealthThresholds | None = None,
) -> HealthSummary:
    """One OK / WARN / CRITICAL verdict with its reasons, for the health endpoint and page.

    CRITICAL means the agent is failing its job now (queue over SLA, many failed runs or
    model errors, late timers, delivery stalled, a load over budget). WARN means items a
    person should look at (dead letters, failed deliveries, stalled obligations, many
    escalations). A section left out is not judged.
    """
    t = thresholds or HealthThresholds()
    reasons: list[HealthReason] = []
    if queue is not None:
        reasons += _queue_reasons(queue)
    if runs is not None:
        reasons += _run_reasons(runs, t)
    if cost is not None:
        reasons += _cost_reasons(cost)
    if timers is not None:
        reasons += _timer_reasons(timers, t)
    reasons += _stalled_reasons(stalled)
    if outbox is not None:
        reasons += _outbox_reasons(outbox)
    ordered = sorted(reasons, key=lambda r: -_RANK[r.level])  # stable: check order within a level
    level = ordered[0].level if ordered else HealthLevel.OK
    return HealthSummary(level=level, reasons=tuple(ordered))
