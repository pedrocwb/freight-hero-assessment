"""Per-execution SOP isolation and durable, idempotent operator stop requests."""

from typing import cast

import pytest
from temporalio.client import Client
from tests.unit.application.test_runtime_harness import World
from tests.unit.orchestration.test_ingress import ClientStub, message

from freight_agent.application.runtime import RuntimeConfig
from freight_agent.domain.errors import DomainError
from freight_agent.domain.load import Load
from freight_agent.orchestration.activities import LoadActivities
from freight_agent.orchestration.contracts import LoadKey
from freight_agent.orchestration.service import TemporalIngress

pytestmark = pytest.mark.unit


async def test_stop_survives_temporal_outage_and_cannot_be_restarted(load: Load):
    world = await World(load, config=RuntimeConfig(delivery_followups=True)).start()
    client = ClientStub()
    service = TemporalIngress(world.deps, cast(Client, client), "stop-test")
    key = LoadKey(load.broker_id, load.load_id)
    client.down = True
    assert await service.stop(key) == {"status": "stopping"}
    assert await service.stop(key) == {"status": "stopping"}
    snapshot = await world.snapshot()
    assert snapshot.load.automation_stop_requested_at and not snapshot.load.automation_stopped_at
    assert len(snapshot.events) == 1
    client.down = False
    assert await service.reconcile_once() == 1
    activities = LoadActivities(world.deps)
    assert (await activities.snapshot(key)).stop_requested
    await activities.stop(key)
    assert (await activities.snapshot(key)).finished
    assert await service.stop(key) == {"status": "stopped"}
    calls = len(client.calls)
    await service.notify(key)
    assert await service.reconcile_once(recover_timers=True) == 0
    assert len(client.calls) == calls
    with pytest.raises(DomainError, match="parada"):
        await service.message(key, message(load.load_id))
    assert (await world.snapshot()).load.automation_stopped_at
