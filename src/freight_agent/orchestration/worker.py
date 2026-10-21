"""Process composition: real clock, shared Postgres, existing LangGraph and gateways."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace

from temporalio.client import Client
from temporalio.worker import Worker

from freight_agent.bootstrap import (
    build_platform_memory,
    build_platform_store,
    build_runtime_deps,
)
from freight_agent.orchestration.activities import LoadActivities
from freight_agent.orchestration.service import TemporalIngress
from freight_agent.orchestration.workflow import LoadWorkflow
from freight_agent.settings import Settings


@asynccontextmanager
async def services(settings: Settings, *, lazy: bool = True) -> AsyncIterator[TemporalIngress]:
    if settings.persistence != "postgres":
        raise ValueError("Temporal requires PERSISTENCE=postgres and its own DATABASE_URL")
    store = build_platform_store(settings)
    memory = build_platform_memory(settings)
    try:
        deps = build_runtime_deps(settings, persistence=store.uow_factory, memory=memory.memory)
        deps = replace(deps, config=replace(deps.config, delivery_followups=True))
        client = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace, lazy=lazy
        )
        yield TemporalIngress(deps, client, settings.temporal_task_queue)
    finally:
        await memory.close()
        await store.close()


async def serve_worker(settings: Settings) -> None:
    async with services(settings, lazy=False) as service:
        activities = LoadActivities(service.deps)
        worker = Worker(
            service.client,
            task_queue=settings.temporal_task_queue,
            workflows=[LoadWorkflow],
            activities=activities.registered(),
            max_concurrent_activities=20,
        )
        await worker.run()


async def serve_relay(settings: Settings) -> None:
    async with services(settings) as service:
        await service.runtime.queue_missed_extractions()
        # One maintenance process per DB. Facility extraction never changes load state;
        # load events, documents, deliveries and timers belong to Temporal Workers.
        import asyncio
        import logging

        async def maintenance() -> None:
            while True:
                try:
                    await service.runtime.process_background()
                except Exception:
                    logging.getLogger(__name__).exception("background pipeline failed")
                await asyncio.sleep(settings.temporal_reconcile_seconds)

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(service.relay(settings.temporal_reconcile_seconds))
            tasks.create_task(maintenance())
