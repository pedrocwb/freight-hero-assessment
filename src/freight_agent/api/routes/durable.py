"""The UI's bridge to real-clock loads. Never touches the simulator's state or clock."""

from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from freight_agent.orchestration.policy import DemoPolicy

router = APIRouter(prefix="/durable", tags=["durable"])
DemoId = Annotated[str, Path(pattern=r"^aurora-[a-z0-9-]{1,64}$")]


@router.api_route("/workspace/{path:path}", methods=["GET", "POST", "PUT"])
async def workspace(path: str, request: Request) -> JSONResponse:
    import re

    if not re.fullmatch(r"brokers(?:/[a-zA-Z0-9_-]+)*", path):
        raise HTTPException(422, "Caminho inválido.")
    raw = await request.body()
    payload = await request.json() if raw else None
    return await forward(request, request.method, f"/workspace/{path}", payload)


class DriverMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message_id: str = Field(pattern=r"^[a-zA-Z0-9-]{1,80}$")
    body: str = Field(min_length=1, max_length=2000)


async def forward(
    request: Request, method: str, path: str, payload: dict | None = None
) -> JSONResponse:
    url = request.app.state.durable_api_url.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.request(method, url, json=payload)
        return JSONResponse(response.json(), status_code=response.status_code)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            503, "A operação ao vivo está indisponível. Inicie os serviços do Temporal."
        ) from exc


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    return await forward(request, "GET", "/health")


@router.post("/demos/aurora/{load_id}")
async def start(load_id: DemoId, request: Request, body: DemoPolicy | None = None) -> JSONResponse:
    return await forward(
        request,
        "POST",
        f"/demos/aurora/{quote(load_id, safe='')}",
        body.model_dump() if body else None,
    )


@router.get("/sop")
async def sop(request: Request) -> JSONResponse:
    return await forward(request, "GET", "/demos/aurora-policy")


@router.post("/loads/{load_id}/stop")
async def stop(load_id: DemoId, request: Request) -> JSONResponse:
    return await forward(request, "POST", f"/brokers/aurora/loads/{quote(load_id, safe='')}/stop")


@router.get("/loads/{load_id}")
async def snapshot(load_id: DemoId, request: Request) -> JSONResponse:
    return await forward(request, "GET", f"/brokers/aurora/loads/{quote(load_id, safe='')}")


@router.post("/loads/{load_id}/messages")
async def driver_message(load_id: DemoId, body: DriverMessage, request: Request) -> JSONResponse:
    return await forward(
        request,
        "POST",
        f"/brokers/aurora/loads/{quote(load_id, safe='')}/messages",
        {
            "message_id": body.message_id,
            "load_id": load_id,
            "channel": "sms",
            "sender_contact_id": "drv_aurora",
            "body": body.body,
            "occurred_at": datetime.now(UTC).isoformat(),
        },
    )
