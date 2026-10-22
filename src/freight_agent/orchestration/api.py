"""Real-clock broker workspace and integration ingress for durable loads."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from freight_agent.adapters.persistence.postgres.profiles import PostgresProfiles
from freight_agent.adapters.persistence.postgres.store import PostgresUnitOfWorkFactory
from freight_agent.application.contracts import InboundMessage, LocationPing
from freight_agent.domain.errors import DomainError
from freight_agent.domain.load import Channel, Load
from freight_agent.orchestration.contracts import LoadKey
from freight_agent.orchestration.profiles import (
    BROKERS,
    BrokerProfile,
    default_profile,
    freeze_profile,
)
from freight_agent.orchestration.worker import services
from freight_agent.orchestration.workspace import (
    inject_step,
    scenario_info,
    scenarios,
    start_scenario,
)
from freight_agent.settings import Settings


class Registration(BaseModel):
    metadata: dict


class StartScenario(BaseModel):
    scenario_id: str
    revision: int


class OperatorMessage(BaseModel):
    message_id: str
    body: str
    sender_contact_id: str
    channel: Channel


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with services(settings) as service:
            app.state.ingress = service
            store = PostgresUnitOfWorkFactory(settings.require_database_url().get_secret_value())
            app.state.profiles = PostgresProfiles(store)
            try:
                yield
            finally:
                await store.close()

    app = FastAPI(title="Freight durable ingress", lifespan=lifespan)

    @app.exception_handler(DomainError)
    async def invalid(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "mode": "temporal",
            "gateways": "recording",
            "model_ready": settings.model_ready,
        }

    async def profile_for(request: Request, broker_id: str) -> BrokerProfile:
        default = await default_profile(settings.sops_dir, broker_id)
        saved = await request.app.state.profiles.get_or_create(
            broker_id, default.model_dump(mode="json")
        )
        return BrokerProfile.model_validate(saved)

    @app.get("/workspace/brokers")
    async def brokers() -> list[dict]:
        return [
            {"id": key, "name": name, "description": description}
            for key, (name, description) in BROKERS.items()
        ]

    @app.get("/workspace/brokers/{broker_id}/profile")
    async def profile(broker_id: str, request: Request):
        return await profile_for(request, broker_id)

    @app.put("/workspace/brokers/{broker_id}/profile")
    async def save_profile(broker_id: str, body: BrokerProfile, request: Request):
        original = await profile_for(request, broker_id)
        body.checked(broker_id, original)
        freeze_profile(body)  # reject invalid placeholders before persisting
        return await request.app.state.profiles.save(
            broker_id, body.revision, body.model_dump(mode="json")
        )

    @app.get("/workspace/brokers/{broker_id}/scenarios")
    async def scenario_catalog(broker_id: str, request: Request):
        await profile_for(request, broker_id)
        return [
            scenario_info(settings.evals_dir, s) for s in scenarios(settings.evals_dir, broker_id)
        ]

    @app.get("/workspace/brokers/{broker_id}/loads")
    async def live_loads(broker_id: str, request: Request):
        await profile_for(request, broker_id)
        loads = await request.app.state.profiles.loads(broker_id)
        return [
            {
                "load_id": load["load_id"],
                "stage": load["stage"],
                "stopped": bool(load.get("automation_stopped_at")),
                "stopping": bool(load.get("automation_stop_requested_at")),
                "scenario": (load.get("sop_snapshot") or {})
                .get("scenario", {})
                .get("title", "Operação ao vivo"),
                "version": (load.get("sop_snapshot") or {}).get("version", ""),
                "started_at": (load.get("sop_snapshot") or {}).get("started_at"),
            }
            for load in loads
        ]

    @app.post("/workspace/brokers/{broker_id}/loads/{load_id}/start", status_code=202)
    async def start_live(broker_id: str, load_id: str, body: StartScenario, request: Request):
        import re

        if not re.fullmatch(r"[a-z0-9-]{1,80}", load_id):
            raise HTTPException(422, "ID de carga inválido.")
        if not settings.model_ready:
            raise HTTPException(503, "Configure a IA antes de iniciar a operação.")
        return await start_scenario(
            request.app.state.ingress,
            settings.evals_dir,
            await profile_for(request, broker_id),
            body.scenario_id,
            load_id,
            body.revision,
        )

    @app.get("/workspace/brokers/{broker_id}/loads/{load_id}")
    async def live_snapshot(broker_id: str, load_id: str, request: Request):
        state = await snapshot(broker_id, load_id, request)
        async with request.app.state.ingress.deps.uow_factory() as uow:
            pending = await uow.queue.head(load_id)
        return {**state.model_dump(mode="json"), "processing": pending is not None}

    @app.post("/workspace/brokers/{broker_id}/loads/{load_id}/stop", status_code=202)
    async def live_stop(broker_id: str, load_id: str, request: Request):
        return await stop(broker_id, load_id, request)

    @app.post("/workspace/brokers/{broker_id}/loads/{load_id}/steps/{index}", status_code=202)
    async def step(broker_id: str, load_id: str, index: int, request: Request):
        return await inject_step(
            request.app.state.ingress, settings.evals_dir, LoadKey(broker_id, load_id), index
        )

    @app.post("/workspace/brokers/{broker_id}/loads/{load_id}/messages", status_code=202)
    async def operator_message(
        broker_id: str, load_id: str, body: OperatorMessage, request: Request
    ):
        from datetime import UTC, datetime

        service = request.app.state.ingress
        key = LoadKey(broker_id, load_id)
        await service._check(key, running=True)
        load = (await service.runtime.snapshot(load_id)).load
        contact = load.contact(body.sender_contact_id)
        if (
            not contact
            or body.channel not in contact.channels
            or not body.body.strip()
            or len(body.body) > 2000
        ):
            raise HTTPException(
                422, "Confira contato, canal e texto da mensagem (até 2.000 caracteres)."
            )
        return await service.message(
            key,
            InboundMessage(
                message_id=body.message_id,
                load_id=load_id,
                channel=body.channel,
                sender_contact_id=body.sender_contact_id,
                body=body.body,
                occurred_at=datetime.now(UTC),
            ),
        )

    @app.post("/workspace/brokers/{broker_id}/loads/{load_id}/pings", status_code=202)
    async def live_ping(broker_id: str, load_id: str, body: LocationPing, request: Request):
        return await ping(broker_id, load_id, body, request)

    @app.post("/brokers/{broker_id}/loads/{load_id}/stop", status_code=202)
    async def stop(broker_id: str, load_id: str, request: Request) -> dict:
        return await request.app.state.ingress.stop(LoadKey(broker_id, load_id))

    @app.post("/loads", status_code=201)
    async def register(body: Registration, request: Request) -> dict:
        try:
            load = Load.from_metadata(body.metadata)
        except (ValueError, KeyError, TypeError, DomainError) as exc:
            raise HTTPException(422, "invalid load metadata") from exc
        key = await request.app.state.ingress.register(load)
        return {"workflow_id": key.workflow_id, "load_id": load.load_id}

    @app.post("/brokers/{broker_id}/loads/{load_id}/messages", status_code=202)
    async def message(broker_id: str, load_id: str, body: InboundMessage, request: Request):
        return await request.app.state.ingress.message(LoadKey(broker_id, load_id), body)

    @app.post("/brokers/{broker_id}/loads/{load_id}/pings", status_code=202)
    async def ping(broker_id: str, load_id: str, body: LocationPing, request: Request):
        return await request.app.state.ingress.ping(LoadKey(broker_id, load_id), body)

    @app.get("/brokers/{broker_id}/loads/{load_id}")
    async def snapshot(broker_id: str, load_id: str, request: Request):
        service = request.app.state.ingress
        await service._check(LoadKey(broker_id, load_id))
        return await service.runtime.snapshot(load_id)

    return app
