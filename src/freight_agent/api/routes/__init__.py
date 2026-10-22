"""The API's routers, one per section of `docs/api.md`, all under `/api`."""

from fastapi import APIRouter

from freight_agent.api.routes import (
    documents,
    durable,
    graph,
    ingress,
    loads,
    ops,
    scenarios,
    sim,
    sops,
)

ROUTERS: tuple[APIRouter, ...] = (
    sim.router,
    loads.router,
    ingress.router,
    sops.router,
    scenarios.router,
    graph.router,
    ops.router,
    documents.router,
    durable.router,
)
