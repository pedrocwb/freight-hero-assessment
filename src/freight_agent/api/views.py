"""Read models for the platform's panels, built from load snapshots. Pure functions."""

from freight_agent.api.schemas import LoadSummary
from freight_agent.application.contracts import LoadSnapshot
from freight_agent.domain.timers import TimerStatus


def load_summary(snapshot: LoadSnapshot) -> LoadSummary:
    load = snapshot.load
    return LoadSummary(
        load_id=load.load_id,
        broker_id=load.broker_id,
        broker_name=load.broker_name,
        stage=load.stage,
        delay_risk=load.delay_risk,
        open_obligations=sum(1 for o in snapshot.obligations if o.is_open),
        pending_timers=sum(1 for t in snapshot.timers if t.status is TimerStatus.PENDING),
        cost_usd=sum(run.cost_usd for run in snapshot.runs),
    )
