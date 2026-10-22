"""Fresh, repeatable demo inputs. Timers and decisions still belong to the real worker."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from freight_agent.application.contracts import LocationPing
from freight_agent.domain.errors import DomainError
from freight_agent.domain.load import DelayRisk, Load
from freight_agent.orchestration.policy import DemoPolicy, freeze_policy
from freight_agent.orchestration.service import TemporalIngress


async def start_extreme_demo(
    service: TemporalIngress, evals_dir: Path, load_id: str, policy: DemoPolicy | None = None
) -> dict:
    metadata = json.loads((evals_dir / "fixtures/load_aurora_extreme.json").read_text())
    metadata["load_id"] = load_id
    now = datetime.now(UTC)
    local = now.astimezone(ZoneInfo("America/Sao_Paulo"))
    for stop, offset in zip(metadata["stops"], (-8, 1), strict=True):
        stop["appointment"]["time"] = (local + timedelta(hours=offset)).isoformat()
    policy = policy or DemoPolicy()
    async with service.deps.uow_factory() as uow:
        existing = await uow.loads.get(load_id)
    frozen = existing.sop_snapshot if existing else None
    if frozen and frozen["policy"] != policy.model_dump():
        raise DomainError("Este ID já usa outro SOP. Inicie uma nova execução.")
    # Retry the accepted request with its original catalog even after a deployment
    # changes the base SOP. Registration still fences concurrent different requests.
    if frozen is None:
        frozen = await freeze_policy(service.deps.knowledge, policy)
    load = Load.from_metadata(metadata).model_copy(update={"sop_snapshot": frozen})
    key = await service.register(load)
    # Retrying the same start request cannot create a second risk event/workflow.
    await service.ping(
        key,
        LocationPing(
            ping_id=f"demo-risk:{load_id}",
            load_id=load_id,
            lat=-23.68,
            lon=-46.88,
            delay_risk=DelayRisk.HIGH,
            occurred_at=now,
        ),
    )
    return {"load_id": load_id, "broker_id": key.broker_id, "workflow_id": key.workflow_id}
