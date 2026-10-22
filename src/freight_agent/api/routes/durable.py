"""The UI's fixed-upstream bridge to the live broker workspace."""

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/durable", tags=["durable"])


@router.get("/workspace/{path:path}")
@router.post("/workspace/{path:path}")
@router.put("/workspace/{path:path}")
async def workspace(path: str, request: Request) -> JSONResponse:
    import re

    if not re.fullmatch(r"brokers(?:/[a-zA-Z0-9_-]+)*", path):
        raise HTTPException(422, "Caminho inválido.")
    raw = await request.body()
    payload = await request.json() if raw else None
    return await forward(request, request.method, f"/workspace/{path}", payload)


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
