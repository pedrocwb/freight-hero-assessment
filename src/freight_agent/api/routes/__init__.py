"""The API's routers, one per section of `docs/api.md`, all under `/api`."""

from fastapi import APIRouter

from freight_agent.api.routes import (
    brokers,
    documents,
    evals,
    hero,
    ingress,
    loads,
    ops,
    sim,
    sops,
)

ROUTERS: tuple[APIRouter, ...] = (
    sim.router,
    loads.router,
    ingress.router,
    hero.router,
    sops.router,
    evals.router,
    ops.router,
    brokers.router,
    documents.router,
)
