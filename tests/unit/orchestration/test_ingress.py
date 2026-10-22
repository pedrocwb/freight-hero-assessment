"""Committed input survives unavailable Temporal; retry does not overwrite load state."""

from contextlib import asynccontextmanager
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from temporalio.client import Client
from tests.unit.application.test_runtime_harness import DRIVER, World, at

from freight_agent.application.contracts import InboundMessage
from freight_agent.application.runtime import RuntimeConfig
from freight_agent.domain.errors import DomainError
from freight_agent.domain.load import Channel, Load, Stage
from freight_agent.orchestration import api
from freight_agent.orchestration.contracts import LoadKey
from freight_agent.orchestration.service import TemporalIngress
from freight_agent.settings import Settings

pytestmark = pytest.mark.unit


class ClientStub:
    def __init__(self):
        self.down = False
        self.calls = []

    async def start_workflow(self, *args, **kwargs):
        if self.down:
            raise ConnectionError("Temporal unavailable")
        self.calls.append((args, kwargs))


def message(load_id: str) -> InboundMessage:
    return InboundMessage(
        message_id="provider/1",
        load_id=load_id,
        channel=Channel.SMS,
        sender_contact_id=DRIVER,
        body="What's the PO?",
        occurred_at=at(7, 12),
    )


async def test_ingress_gap_is_reconciled_from_database(load: Load):
    world = await World(load, config=RuntimeConfig(delivery_followups=True)).start()
    client = ClientStub()
    service = TemporalIngress(world.deps, cast(Client, client), "freight-loads")
    key = LoadKey(load.broker_id, load.load_id)
    client.down = True
    result = await service.message(key, message(load.load_id))
    assert result.accepted
    assert not client.calls
    async with world.deps.uow_factory() as uow:
        assert await uow.queue.size() == 1
    with pytest.raises(RuntimeError, match="deferred"):
        await service.reconcile_once()
    client.down = False
    assert await service.reconcile_once() == 1
    assert client.calls[-1][1]["id"] == key.workflow_id
    assert client.calls[-1][1]["start_signal"] == "wake"
    assert (await service.message(key, message(load.load_id))).duplicate
    async with world.deps.uow_factory() as uow:
        assert await uow.queue.size() == 1
    email = message(load.load_id).model_copy(update={"channel": Channel.EMAIL})
    other_channel = await service.message(key, email)
    assert other_channel.accepted and not other_channel.duplicate
    assert other_channel.event_id != result.event_id


async def test_registration_is_idempotent_and_checks_broker(load: Load):
    world = await World(load).start()
    service = TemporalIngress(world.deps, cast(Client, ClientStub()), "freight-loads")
    async with world.deps.uow_factory() as uow:
        await uow.loads.save(load.model_copy(update={"stage": Stage.DELIVERED}))
        await uow.commit()
    await service.register(load)
    assert (await world.snapshot()).load.stage is Stage.DELIVERED
    with pytest.raises(DomainError, match="another broker"):
        await service.register(load.model_copy(update={"broker_id": "different"}))
    with pytest.raises(DomainError, match="mismatch"):
        await service.message(LoadKey("different", load.load_id), message(load.load_id))


async def test_restart_recovers_timer_only_load(load: Load):
    world = await World(load, config=RuntimeConfig(delivery_followups=True)).start()
    await world.high_risk_at_seven()
    client = ClientStub()
    service = TemporalIngress(world.deps, cast(Client, client), "freight-loads")
    assert await service.reconcile_once() == 0
    client.down = True
    with pytest.raises(RuntimeError, match="deferred"):
        await service.reconcile_once(recover_timers=True)
    client.down = False
    assert await service.reconcile_once(recover_timers=True) == 1


def test_durable_api_accepts_and_exposes_committed_input(load: Load, load_metadata, monkeypatch):
    world = World(load, config=RuntimeConfig(delivery_followups=True))
    stub = ClientStub()
    service = TemporalIngress(world.deps, cast(Client, stub), "freight-loads")

    @asynccontextmanager
    async def wired(settings):
        yield service

    monkeypatch.setattr(api, "services", wired)
    # The profile repository is a separate startup dependency, unused by these routes.
    # Keep this unit test independent of a developer's .env and any Postgres server.
    profile_store = Mock(spec=api.PostgresUnitOfWorkFactory, close=AsyncMock())
    monkeypatch.setattr(api, "PostgresUnitOfWorkFactory", Mock(return_value=profile_store))
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database_url=SecretStr("postgresql://unused/test"),
        openrouter_api_key=None,
        langsmith_api_key=None,
    )
    with TestClient(api.create_app(settings)) as client:
        assert client.get("/health").json()["mode"] == "temporal"
        registration = client.post("/loads", json={"metadata": load_metadata})
        assert registration.status_code == 201, registration.text
        path = f"/brokers/{load.broker_id}/loads/{load.load_id}"
        accepted = client.post(
            f"{path}/messages", json=message(load.load_id).model_dump(mode="json")
        )
        assert accepted.status_code == 202 and accepted.json()["accepted"]
        state = client.get(path)
        assert state.status_code == 200 and len(state.json()["events"]) == 1
        assert client.get(f"/brokers/wrong/loads/{load.load_id}").status_code == 409
        assert client.post("/loads", json={"metadata": {}}).status_code == 422
    profile_store.assert_not_called()
    profile_store.close.assert_awaited_once()
