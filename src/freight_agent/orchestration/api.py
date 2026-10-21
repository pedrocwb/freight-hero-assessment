"""Real-clock ingress for durable loads. The existing UI remains a virtual-clock simulator."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from freight_agent.application.contracts import InboundMessage, LocationPing
from freight_agent.domain.errors import DomainError
from freight_agent.domain.load import Load
from freight_agent.orchestration.contracts import LoadKey
from freight_agent.orchestration.worker import services
from freight_agent.settings import Settings


class Registration(BaseModel):
    metadata: dict


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with services(settings) as service:
            app.state.ingress = service
            yield

    app = FastAPI(title="Freight durable ingress", lifespan=lifespan)

    @app.exception_handler(DomainError)
    async def invalid(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "mode": "temporal", "gateways": "recording"}

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
