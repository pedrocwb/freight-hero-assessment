from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from freight_agent.application.metrics import (
    CostMetrics,
    HealthLevel,
    HealthThresholds,
    LoadCost,
    ModelErrors,
    OutboxMetrics,
    QueueMetrics,
    RunMetrics,
    StalledObligation,
    TimerLagMetrics,
    cost_metrics,
    health_summary,
    outbox_metrics,
    percentile,
    queue_metrics,
    run_metrics,
    stalled_obligations,
    timer_lag_metrics,
)
from freight_agent.domain.actions import Plan, SendSms
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.load import Channel
from freight_agent.domain.obligations import Obligation, ObligationKind, ObligationStatus
from freight_agent.domain.outbox import OutboxItem, OutboxKind, OutboxStatus
from freight_agent.domain.runs import RunRecord, RunStatus, Usage
from freight_agent.domain.timers import Timer, TimerStatus
from freight_agent.domain.validation import Severity, Violation

LA = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 10, 13, 7, 0, tzinfo=LA)
PLAN = Plan(actions=(SendSms(contact_id="drv_1", text="ETA?"),))


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _run(
    n: int,
    *,
    status: RunStatus = RunStatus.ACTED,
    load_id: str = "481207",
    duration_s: float | None = 10.0,
    usage: tuple[Usage, ...] = (),
    violations: tuple[Violation, ...] = (),
    plan: Plan | None = None,
    error: str | None = None,
) -> RunRecord:
    started = _at(n)
    return RunRecord(
        run_id=f"run_{n}",
        event_id=f"m{n}",
        load_id=load_id,
        started_at=started,
        finished_at=None if duration_s is None else started + timedelta(seconds=duration_s),
        status=status,
        usage=usage,
        violations=violations,
        plan=plan,
        error=error,
    )


def _usage(model: str, cost: float = 0.0, role: str = "gate") -> Usage:
    return Usage(role=role, model=model, cost_usd=cost)


def _event(event_id: str, at: datetime) -> Event:
    return Event(
        event_id=event_id,
        load_id="481207",
        type=EventType.INBOUND_MESSAGE,
        occurred_at=at,
        channel=Channel.SMS,
    )


def _timer_event(timer_id: str, at: datetime) -> Event:
    return Event(
        event_id=f"timer:{timer_id}",
        load_id="481207",
        type=EventType.TIMER_DUE,
        occurred_at=at,
        channel=Channel.SYSTEM,
        data={"timer_id": timer_id, "obligation_id": "obl_1", "kind": "eta_request"},
    )


def _timer(
    timer_id: str,
    due: datetime,
    *,
    obligation_id: str = "obl_1",
    status: TimerStatus = TimerStatus.PENDING,
) -> Timer:
    return Timer(
        timer_id=timer_id,
        load_id="481207",
        obligation_id=obligation_id,
        due_at=due,
        created_at=T0,
        status=status,
    )


def _obligation(
    obligation_id: str = "obl_1",
    *,
    last_action_at: datetime | None = T0,
    status: ObligationStatus = ObligationStatus.OPEN,
) -> Obligation:
    return Obligation(
        obligation_id=obligation_id,
        load_id="481207",
        kind=ObligationKind.ETA_REQUEST,
        opened_at=T0,
        last_action_at=last_action_at,
        status=status,
    )


def _outbox(
    n: int, *, created: datetime, status: OutboxStatus = OutboxStatus.PENDING
) -> OutboxItem:
    return OutboxItem(
        outbox_id=f"out_{n}",
        idempotency_key=f"m1:{n}",
        run_id="run_1",
        load_id="481207",
        kind=OutboxKind.SMS,
        created_at=created,
        status=status,
    )


# --- percentile ----------------------------------------------------------------


@pytest.mark.unit
def test_percentile_of_nothing_is_none() -> None:
    assert percentile([], 0.5) is None


@pytest.mark.unit
def test_percentile_of_one_value_is_that_value() -> None:
    assert percentile([7.0], 0.5) == 7.0
    assert percentile([7.0], 0.95) == 7.0


@pytest.mark.unit
def test_percentile_interpolates_between_ranks() -> None:
    values = [4.0, 1.0, 3.0, 2.0]  # unsorted on purpose
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 1.0) == 4.0
    assert percentile(values, 0.5) == pytest.approx(2.5)
    assert percentile(list(map(float, range(1, 101))), 0.95) == pytest.approx(95.05)


@pytest.mark.unit
@pytest.mark.parametrize("q", [-0.1, 1.5])
def test_percentile_rejects_a_fraction_outside_zero_to_one(q: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        percentile([1.0], q)


# --- queue ---------------------------------------------------------------------


@pytest.mark.unit
def test_empty_queue_has_no_age_and_meets_the_sla() -> None:
    metrics = queue_metrics([], [], now=T0)
    assert metrics == QueueMetrics(
        queued=0,
        oldest_event_id=None,
        oldest_age_s=None,
        sla_s=300.0,
        over_sla=False,
        dead_letters=0,
    )


@pytest.mark.unit
def test_queue_age_is_the_oldest_event_against_the_sla() -> None:
    queued = [_event("m2", _at(-2)), _event("m1", _at(-6)), _event("m3", _at(-1))]
    metrics = queue_metrics(queued, [_event("dead", _at(-60))], now=T0)
    assert metrics.queued == 3
    assert metrics.oldest_event_id == "m1"
    assert metrics.oldest_age_s == pytest.approx(360.0)
    assert metrics.over_sla is True
    assert metrics.dead_letters == 1


@pytest.mark.unit
def test_queue_within_the_sla_is_not_over_it() -> None:
    metrics = queue_metrics([_event("m1", _at(-5))], [], now=T0)
    assert metrics.oldest_age_s == pytest.approx(300.0)
    assert metrics.over_sla is False  # exactly at the SLA is still within it


# --- runs ----------------------------------------------------------------------


@pytest.mark.unit
def test_no_runs_gives_empty_metrics_without_dividing_by_zero() -> None:
    metrics = run_metrics([])
    assert metrics.total == 0
    assert metrics.finished == 0
    assert metrics.duration_p50_s is None
    assert metrics.duration_p95_s is None
    assert set(metrics.status_counts) == set(RunStatus)
    assert all(count == 0 for count in metrics.status_counts.values())
    assert all(rate == 0.0 for rate in metrics.status_rates.values())
    assert metrics.block_rate == 0.0
    assert metrics.models == ()


@pytest.mark.unit
def test_one_run_is_its_own_p50_and_p95() -> None:
    metrics = run_metrics([_run(1, duration_s=12.0)])
    assert metrics.duration_p50_s == pytest.approx(12.0)
    assert metrics.duration_p95_s == pytest.approx(12.0)
    assert metrics.status_rates[RunStatus.ACTED] == 1.0


@pytest.mark.unit
def test_durations_skip_unfinished_runs_and_take_percentiles() -> None:
    runs = [_run(n, duration_s=float(n)) for n in range(1, 21)] + [_run(99, duration_s=None)]
    metrics = run_metrics(runs)
    assert metrics.total == 21
    assert metrics.finished == 20
    assert metrics.duration_p50_s == pytest.approx(10.5)
    assert metrics.duration_p95_s == pytest.approx(19.05)


@pytest.mark.unit
def test_duration_is_measured_on_instants_across_a_dst_change() -> None:
    # 01:59 PDT to 01:01 PST on 2026-11-01 is two real minutes, not minus 58.
    run = RunRecord(
        run_id="run_dst",
        event_id="m_dst",
        load_id="481207",
        started_at=datetime(2026, 11, 1, 1, 59, tzinfo=LA),
        finished_at=datetime(2026, 11, 1, 1, 1, fold=1, tzinfo=LA),
        status=RunStatus.NOOP,
    )
    assert run_metrics([run]).duration_p50_s == pytest.approx(120.0)


@pytest.mark.unit
def test_status_rates_count_every_status() -> None:
    statuses = [
        RunStatus.NOOP,
        RunStatus.NOOP,
        RunStatus.ACTED,
        RunStatus.ESCALATED,
        RunStatus.SKIPPED,
        RunStatus.FAILED,
        RunStatus.ACTED,
        RunStatus.ACTED,
    ]
    metrics = run_metrics([_run(n, status=s) for n, s in enumerate(statuses)])
    assert metrics.status_counts == {
        RunStatus.SKIPPED: 1,
        RunStatus.NOOP: 2,
        RunStatus.ACTED: 3,
        RunStatus.ESCALATED: 1,
        RunStatus.FAILED: 1,
    }
    assert metrics.status_rates[RunStatus.ACTED] == pytest.approx(3 / 8)
    assert metrics.status_rates[RunStatus.NOOP] == pytest.approx(2 / 8)
    assert sum(metrics.status_rates.values()) == pytest.approx(1.0)


@pytest.mark.unit
def test_validator_blocks_count_runs_with_violations_and_high_ones() -> None:
    low = Violation(rule="rate_limit", action_index=0, message="too many SMS")
    high = Violation(
        rule="no_contact_info", action_index=1, message="phone", severity=Severity.HIGH
    )
    runs = [
        _run(1, plan=PLAN),
        _run(2, plan=PLAN, violations=(low,)),
        _run(3, plan=PLAN, violations=(low, high), status=RunStatus.ESCALATED),
        _run(4, plan=PLAN),
        _run(5, status=RunStatus.SKIPPED),  # never reached the validator
    ]
    metrics = run_metrics(runs)
    assert metrics.planned == 4
    assert metrics.blocked_runs == 2
    assert metrics.high_severity_runs == 1
    assert metrics.block_rate == pytest.approx(0.5)


@pytest.mark.unit
def test_model_error_rate_combines_usage_and_errors_that_name_the_model() -> None:
    runs = [
        _run(1, usage=(_usage("model-a"), _usage("model-a", role="decide"))),
        _run(2, usage=(_usage("model-a"),)),
        _run(
            3,
            status=RunStatus.ESCALATED,
            usage=(_usage("model-a"),),
            error="every model in the fallback chain failed: model-b: timeout; model-c: 429",
        ),
        _run(4, status=RunStatus.FAILED, error="KeyError: 'load'"),  # not a model error
    ]
    metrics = run_metrics(runs, known_models=["model-b", "model-c", "model-d"])
    assert metrics.models == (
        ModelErrors(model="model-a", calls=4, errors=0, error_rate=0.0),
        ModelErrors(model="model-b", calls=1, errors=1, error_rate=1.0),
        ModelErrors(model="model-c", calls=1, errors=1, error_rate=1.0),
        ModelErrors(model="model-d", calls=0, errors=0, error_rate=0.0),
    )


@pytest.mark.unit
def test_a_model_error_counts_once_per_run_even_when_named_twice() -> None:
    runs = [
        _run(1, usage=(_usage("m"),)),
        _run(2, status=RunStatus.ESCALATED, error="m: timeout; retry m: timeout"),
    ]
    (model,) = run_metrics(runs).models
    assert model == ModelErrors(model="m", calls=2, errors=1, error_rate=0.5)


@pytest.mark.unit
def test_a_model_name_inside_a_longer_model_name_is_not_matched() -> None:
    runs = [
        _run(1, usage=(_usage("openai/gpt-4o"), _usage("openai/gpt-4o-mini"))),
        _run(2, status=RunStatus.ESCALATED, error="openai/gpt-4o-mini: timeout"),
    ]
    by_model = {m.model: m for m in run_metrics(runs).models}
    assert by_model["openai/gpt-4o"].errors == 0
    assert by_model["openai/gpt-4o-mini"].errors == 1


# --- cost ----------------------------------------------------------------------


@pytest.mark.unit
def test_no_runs_cost_nothing() -> None:
    assert cost_metrics([]) == CostMetrics(
        budget_usd=1.0, total_cost_usd=0.0, avg_cost_per_run_usd=0.0, loads=()
    )


@pytest.mark.unit
def test_cost_per_load_against_the_budget() -> None:
    runs = [
        _run(1, load_id="cheap", usage=(_usage("m", 0.10), _usage("m", 0.05))),
        _run(2, load_id="warm", usage=(_usage("m", 0.85),)),
        _run(3, load_id="over", usage=(_usage("m", 0.70),)),
        _run(4, load_id="over", usage=(_usage("m", 0.40),)),
        _run(5, load_id="cheap", status=RunStatus.SKIPPED),
    ]
    metrics = cost_metrics(runs)
    assert metrics.total_cost_usd == pytest.approx(2.10)
    assert metrics.avg_cost_per_run_usd == pytest.approx(2.10 / 5)
    assert [load.load_id for load in metrics.loads] == ["over", "warm", "cheap"]
    over, warm, cheap = metrics.loads
    assert over.runs == 2
    assert over.cost_usd == pytest.approx(1.10)
    assert over.budget_used == pytest.approx(1.10)
    assert (over.over_warning, over.over_budget) == (True, True)
    assert (warm.over_warning, warm.over_budget) == (True, False)
    assert isinstance(cheap, LoadCost)
    assert cheap.runs == 2
    assert cheap.cost_usd == pytest.approx(0.15)
    assert (cheap.over_warning, cheap.over_budget) == (False, False)


@pytest.mark.unit
def test_cost_flags_are_strictly_over_the_thresholds() -> None:
    runs = [
        _run(1, load_id="at_warning", usage=(_usage("m", 0.8),)),
        _run(2, load_id="at_budget", usage=(_usage("m", 2.0),)),
    ]
    metrics = cost_metrics(runs, budget_usd=2.0, warn_fraction=0.4)
    by_load = {load.load_id: load for load in metrics.loads}
    assert by_load["at_warning"].over_warning is False
    assert by_load["at_budget"].over_warning is True
    assert by_load["at_budget"].over_budget is False


# --- timers --------------------------------------------------------------------


@pytest.mark.unit
def test_timer_lag_with_nothing_fired() -> None:
    assert timer_lag_metrics([_timer("t1", T0)], []) == TimerLagMetrics(
        fired=0, measured=0, lag_p50_s=None, lag_p95_s=None, lag_max_s=None
    )


@pytest.mark.unit
def test_timer_lag_compares_the_fired_event_with_the_due_time() -> None:
    timers = [_timer("t1", _at(30)), _timer("t2", _at(60)), _timer("t3", _at(90))]
    events = [
        _timer_event("t1", _at(30)),
        _timer_event("t2", _at(61)),
        _timer_event("t3", _at(92)),
        _timer_event("gone", _at(95)),  # its timer is not in the list: not measured
        _event("m1", _at(31)),  # not a timer event
    ]
    metrics = timer_lag_metrics(timers, events)
    assert metrics.fired == 4
    assert metrics.measured == 3
    assert metrics.lag_p50_s == pytest.approx(60.0)
    assert metrics.lag_max_s == pytest.approx(120.0)
    assert metrics.lag_p95_s == pytest.approx(114.0)


@pytest.mark.unit
def test_a_timer_event_without_a_timer_id_is_not_measured() -> None:
    event = _timer_event("t1", _at(30)).model_copy(update={"data": {}})
    assert timer_lag_metrics([_timer("t1", _at(30))], [event]).measured == 0


# --- stalled obligations -------------------------------------------------------


@pytest.mark.unit
def test_open_obligation_idle_past_the_threshold_without_a_timer_is_stalled() -> None:
    stalled = stalled_obligations([_obligation()], [], now=_at(50))
    assert stalled == (
        StalledObligation(
            obligation_id="obl_1",
            load_id="481207",
            kind=ObligationKind.ETA_REQUEST,
            idle_since=T0,
            idle_s=3000.0,
        ),
    )


@pytest.mark.unit
def test_an_obligation_is_not_stalled_while_a_timer_is_pending() -> None:
    timers = [_timer("t1", _at(30))]
    assert stalled_obligations([_obligation()], timers, now=_at(50)) == ()


@pytest.mark.unit
def test_timers_that_are_not_pending_do_not_keep_an_obligation_alive() -> None:
    timers = [
        _timer("t1", _at(30), status=TimerStatus.FIRED),
        _timer("t2", _at(30), status=TimerStatus.PENDING, obligation_id="obl_other"),
    ]
    assert len(stalled_obligations([_obligation()], timers, now=_at(50))) == 1


@pytest.mark.unit
def test_recent_closed_or_never_acted_obligations() -> None:
    obligations = [
        _obligation("recent", last_action_at=_at(20)),
        _obligation("closed", status=ObligationStatus.SATISFIED),
        _obligation("never_acted", last_action_at=None),  # idle since it was opened
    ]
    stalled = stalled_obligations(obligations, [], now=_at(60), threshold=timedelta(minutes=45))
    assert [s.obligation_id for s in stalled] == ["never_acted"]
    assert stalled[0].idle_since == T0


@pytest.mark.unit
def test_idle_exactly_at_the_threshold_is_not_stalled() -> None:
    assert stalled_obligations([_obligation()], [], now=_at(45)) == ()


# --- outbox --------------------------------------------------------------------


@pytest.mark.unit
def test_empty_outbox() -> None:
    assert outbox_metrics([], now=T0) == OutboxMetrics(
        pending=0, failed=0, stuck=0, oldest_pending_age_s=None, failed_ids=(), stuck_ids=()
    )


@pytest.mark.unit
def test_outbox_counts_failed_and_pending_items_older_than_the_threshold() -> None:
    items = [
        _outbox(1, created=_at(-10)),
        _outbox(2, created=_at(-1)),
        _outbox(3, created=_at(-30), status=OutboxStatus.FAILED),
        _outbox(4, created=_at(-30), status=OutboxStatus.SENT),
        _outbox(5, created=_at(-5)),  # exactly at the threshold: not stuck yet
    ]
    metrics = outbox_metrics(items, now=T0)
    assert metrics.pending == 3
    assert metrics.failed == 1
    assert metrics.failed_ids == ("out_3",)
    assert metrics.stuck == 1
    assert metrics.stuck_ids == ("out_1",)
    assert metrics.oldest_pending_age_s == pytest.approx(600.0)


# --- health --------------------------------------------------------------------


def _healthy_runs() -> RunMetrics:
    return run_metrics([_run(n, usage=(_usage("m"),)) for n in range(10)])


@pytest.mark.unit
def test_nothing_measured_is_ok() -> None:
    summary = health_summary()
    assert summary.level is HealthLevel.OK
    assert summary.reasons == ()


@pytest.mark.unit
def test_healthy_metrics_are_ok() -> None:
    summary = health_summary(
        queue=queue_metrics([_event("m1", _at(-1))], [], now=T0),
        runs=_healthy_runs(),
        cost=cost_metrics([_run(1, usage=(_usage("m", 0.1),))]),
        timers=timer_lag_metrics([_timer("t1", T0)], [_timer_event("t1", T0)]),
        stalled=(),
        outbox=outbox_metrics([_outbox(1, created=_at(-1))], now=T0),
    )
    assert summary.level is HealthLevel.OK
    assert summary.reasons == ()


@pytest.mark.unit
def test_queue_over_the_sla_is_critical_and_dead_letters_warn() -> None:
    summary = health_summary(
        queue=queue_metrics([_event("m1", _at(-6))], [_event("d", _at(-60))], now=T0)
    )
    assert summary.level is HealthLevel.CRITICAL
    assert {(r.check, r.level) for r in summary.reasons} == {
        ("queue_sla", HealthLevel.CRITICAL),
        ("dead_letters", HealthLevel.WARN),
    }
    assert "m1" in next(r.message for r in summary.reasons if r.check == "queue_sla")


@pytest.mark.unit
def test_failed_runs_warn_then_go_critical() -> None:
    def failed(share: int) -> RunMetrics:
        return run_metrics(
            [_run(n, status=RunStatus.FAILED if n < share else RunStatus.ACTED) for n in range(10)]
        )

    assert health_summary(runs=failed(1)).level is HealthLevel.WARN
    assert health_summary(runs=failed(3)).level is HealthLevel.CRITICAL
    reasons = health_summary(runs=failed(3)).reasons
    assert [r.check for r in reasons] == ["failed_runs"]


@pytest.mark.unit
def test_escalations_slow_runs_and_high_violations_warn() -> None:
    high = Violation(rule="r", action_index=0, message="m", severity=Severity.HIGH)
    runs = run_metrics(
        [
            _run(1, status=RunStatus.ESCALATED, duration_s=90.0, plan=PLAN, violations=(high,)),
            _run(2, status=RunStatus.ESCALATED, duration_s=90.0),
            _run(3, duration_s=90.0),
        ]
    )
    summary = health_summary(runs=runs)
    assert summary.level is HealthLevel.WARN
    assert {r.check for r in summary.reasons} == {
        "escalation_rate",
        "run_duration",
        "validator_high",
    }


@pytest.mark.unit
def test_model_errors_warn_then_go_critical() -> None:
    def runs(errors: int) -> RunMetrics:
        ok = [_run(n, usage=(_usage("m"),)) for n in range(10 - errors)]
        bad = [_run(100 + n, status=RunStatus.ESCALATED, error="m: timeout") for n in range(errors)]
        return run_metrics(ok + bad)

    lenient = HealthThresholds(escalation_rate_warn=1.0)
    warn = health_summary(runs=runs(3), thresholds=lenient)
    assert warn.level is HealthLevel.WARN
    assert [r.check for r in warn.reasons] == ["model_errors"]
    assert "m" in warn.reasons[0].message
    assert health_summary(runs=runs(6), thresholds=lenient).level is HealthLevel.CRITICAL


@pytest.mark.unit
def test_cost_over_eighty_percent_warns_and_over_budget_is_critical() -> None:
    warm = cost_metrics([_run(1, load_id="warm", usage=(_usage("m", 0.9),))])
    assert health_summary(cost=warm).level is HealthLevel.WARN
    over = cost_metrics(
        [
            _run(1, load_id="warm", usage=(_usage("m", 0.9),)),
            _run(2, load_id="over", usage=(_usage("m", 1.5),)),
        ]
    )
    summary = health_summary(cost=over)
    assert summary.level is HealthLevel.CRITICAL
    assert {(r.check, r.level) for r in summary.reasons} == {
        ("load_budget", HealthLevel.CRITICAL),
        ("load_budget_warning", HealthLevel.WARN),
    }


@pytest.mark.unit
def test_timer_lag_warns_then_goes_critical() -> None:
    def lag(minutes: float) -> TimerLagMetrics:
        return timer_lag_metrics([_timer("t1", T0)], [_timer_event("t1", _at(minutes))])

    assert health_summary(timers=lag(0.5)).level is HealthLevel.OK
    assert health_summary(timers=lag(2)).level is HealthLevel.WARN
    assert health_summary(timers=lag(10)).level is HealthLevel.CRITICAL
    assert health_summary(timers=timer_lag_metrics([], [])).level is HealthLevel.OK


@pytest.mark.unit
def test_stalled_obligations_warn() -> None:
    stalled = stalled_obligations([_obligation()], [], now=_at(60))
    summary = health_summary(stalled=stalled)
    assert summary.level is HealthLevel.WARN
    assert summary.reasons[0].check == "stalled_obligations"
    assert "obl_1" in summary.reasons[0].message


@pytest.mark.unit
def test_failed_outbox_items_warn_and_stuck_ones_are_critical() -> None:
    failed = outbox_metrics([_outbox(1, created=_at(-1), status=OutboxStatus.FAILED)], now=T0)
    assert health_summary(outbox=failed).level is HealthLevel.WARN
    stuck = outbox_metrics([_outbox(1, created=_at(-20))], now=T0)
    summary = health_summary(outbox=stuck)
    assert summary.level is HealthLevel.CRITICAL
    assert summary.reasons[0].check == "outbox_stuck"


@pytest.mark.unit
def test_the_summary_takes_the_worst_level_and_lists_critical_reasons_first() -> None:
    summary = health_summary(
        queue=queue_metrics([], [_event("d", T0)], now=T0),
        outbox=outbox_metrics([_outbox(1, created=_at(-20))], now=T0),
    )
    assert summary.level is HealthLevel.CRITICAL
    assert [r.level for r in summary.reasons] == [HealthLevel.CRITICAL, HealthLevel.WARN]
