"""The API's routers, one per section of `docs/api.md`, all under `/api`."""

from fastapi import APIRouter

from freight_agent.api.routes import (
    broker_evals,
    brokers,
    documents,
    evals,
    hero,
    ingress,
    knowledge,
    loads,
    ops,
    sim,
)

ROUTERS: tuple[APIRouter, ...] = (
    sim.router,
    loads.router,
    ingress.router,
    hero.router,
    knowledge.router,
    evals.router,
    ops.router,
    brokers.router,
    documents.router,
    broker_evals.router,
)
