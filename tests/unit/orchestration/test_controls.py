"""Per-execution SOP isolation and durable, idempotent operator stop requests."""

from pathlib import Path
from typing import cast

import pytest
from temporalio.client import Client
from tests.unit.application.test_runtime_harness import World
from tests.unit.orchestration.test_ingress import ClientStub, message

from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.application.runtime import RuntimeConfig
from freight_agent.domain.errors import DomainError
from freight_agent.domain.load import Load
from freight_agent.orchestration.activities import LoadActivities
from freight_agent.orchestration.contracts import LoadKey
from freight_agent.orchestration.demo import start_extreme_demo
from freight_agent.orchestration.policy import DEFAULT_BODY, DemoPolicy, freeze_policy, pinned_index
from freight_agent.orchestration.service import TemporalIngress

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


async def test_policy_is_frozen_and_both_text_and_limits_are_versioned(load: Load):
    knowledge = MemorySopIndex.from_files(ROOT / "sops")
    policy = DemoPolicy(
        max_attempts=2, interval_minutes=3, body=DEFAULT_BODY + "\nUse MARCADOR AZUL."
    )
    frozen = await freeze_policy(knowledge, policy)
    other = await freeze_policy(knowledge, DemoPolicy())
    assert frozen["version"] != other["version"]
    assert frozen == await freeze_policy(knowledge, policy)
    index = pinned_index(frozen)
    section = await index.read("aurora", (), "delay_risk_high")
    assert section and "MARCADOR AZUL" in section.body
    assert "{{" not in section.body
    broker = await index.broker("aurora")
    definition = broker.obligation("eta_request")
    assert definition and definition.max_attempts == 2 and definition.follow_up_minutes == 3
    assert broker.limits.validation.sms_max_per_window == 10
    assert await index.read("aurora", (), "eta_received")
    assert not await index.visible("other-broker", ())

    world = await World(load, knowledge=knowledge).start()
    service = TemporalIngress(world.deps, cast(Client, ClientStub()), "policy-test")
    await start_extreme_demo(service, ROOT / "evals", "aurora-policy", policy)
    await start_extreme_demo(service, ROOT / "evals", "aurora-policy", policy)
    with pytest.raises(DomainError, match="outro SOP"):
        await start_extreme_demo(service, ROOT / "evals", "aurora-policy", DemoPolicy())
    saved = await service.runtime.snapshot("aurora-policy")
    assert saved.load.sop_snapshot == frozen
    assert len(saved.events) == 1


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
