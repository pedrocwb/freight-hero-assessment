"""Real-time scenario setup and operator inputs. Only Temporal executes the agent."""

from datetime import datetime
from pathlib import Path

from freight_agent.application.contracts import InboundMessage, LocationPing
from freight_agent.domain.errors import DomainError
from freight_agent.domain.events import Attachment
from freight_agent.domain.load import Load
from freight_agent.evalkit.runner import scenario_metadata, seed_facts
from freight_agent.evalkit.spec import Scenario, load_scenario
from freight_agent.orchestration.contracts import LoadKey
from freight_agent.orchestration.live_inputs import attachment_bytes, live_message
from freight_agent.orchestration.profiles import BrokerProfile, freeze_profile
from freight_agent.orchestration.service import TemporalIngress


def scenarios(root: Path, broker_id: str) -> list[Scenario]:
    found = [load_scenario(p) for p in sorted((root / "scenarios").rglob("*.yaml"))]
    if broker_id == "aurora":
        found.append(
            Scenario.model_validate(
                {
                    "id": "aurora_extreme",
                    "title": "Dez pedidos de ETA sem resposta",
                    "suite": "demo",
                    "description": "Dez SMS, um por minuto, e escalada para um hero.",
                    "load": "fixtures/load_aurora_extreme.json",
                    "date": "2026-10-21",
                    "start": "09:00",
                    "steps": [
                        {
                            "at": "09:00",
                            "note": "Risco alto: iniciar os pedidos de ETA.",
                            "ping": {"delay_risk": "high"},
                        }
                    ],
                }
            )
        )
    own = [s for s in found if scenario_metadata(root, s)["broker"]["id"] == broker_id]
    priority = {
        "a_client_a_high_risk": 0,
        "d1_keystone_high_risk": 0,
        "d2_keystone_reefer_departure": 1,
    }
    return sorted(own, key=lambda s: (priority.get(s.id, 2), s.suite, s.id))


def scenario_info(root: Path, scenario: Scenario) -> dict:
    metadata = scenario_metadata(root, scenario)
    return {
        "id": scenario.id,
        "title": scenario.title,
        "description": scenario.description,
        "suite": scenario.suite,
        "load_types": metadata.get("load_types", []),
        "stage": metadata["stage"],
        "steps": [s.model_dump(mode="json") for s in scenario.steps],
    }


def rebase_load(root: Path, scenario: Scenario, load_id: str, now: datetime) -> Load:
    metadata = scenario_metadata(root, scenario)
    metadata["load_id"] = load_id
    load = Load.from_metadata(metadata)
    # Preserve relative appointment windows while moving the fixture into live time.
    from freight_agent.evalkit.runner import local_time, scenario_timezone

    anchor = local_time(scenario.date, scenario.steps[0].at, scenario_timezone(scenario))
    delta = now - anchor
    stops = tuple(
        s.model_copy(
            update={
                "appointment": s.appointment.model_copy(
                    update={
                        name: value + delta
                        for name in ("at", "start", "end")
                        if (value := getattr(s.appointment, name)) is not None
                    }
                )
            }
        )
        for s in load.stops
    )
    return load.model_copy(update={"stops": stops})


async def start_scenario(
    service: TemporalIngress,
    root: Path,
    profile: BrokerProfile,
    scenario_id: str,
    load_id: str,
    revision: int,
) -> dict:
    key = LoadKey(profile.broker.broker_id, load_id)
    async with service.deps.uow_factory() as uow:
        existing = await uow.loads.get(load_id)
    if existing:
        frozen = existing.sop_snapshot or {}
        if (
            existing.broker_id != key.broker_id
            or frozen.get("scenario", {}).get("id") != scenario_id
            or frozen.get("profile_revision") != revision
        ):
            raise DomainError("Este ID já pertence a outra execução.")
        load = existing
        scenario = Scenario.model_validate(frozen["scenario"])
    else:
        if profile.revision != revision:
            raise DomainError("O SOP mudou. Recarregue o broker antes de iniciar.")
        scenario = next((s for s in scenarios(root, key.broker_id) if s.id == scenario_id), None)
        if scenario is None:
            raise DomainError("Cenário não encontrado neste broker.")
        now = service.deps.clock.now()
        frozen = freeze_profile(profile)
        frozen.update(scenario=scenario.model_dump(mode="json"), started_at=now.isoformat())
        load = rebase_load(root, scenario, load_id, now).model_copy(update={"sop_snapshot": frozen})
        # Fixture memory belongs to this isolated test facility, avoiding interference
        # with another live load of the same broker.
        if scenario.suite == "memory":
            load = load.model_copy(
                update={
                    "stops": tuple(
                        s.model_copy(update={"address": f"{s.address} · teste {load_id}"})
                        for s in load.stops
                    )
                }
            )
    if not existing and scenario.memories and service.deps.memory:
        for client, facts in seed_facts(scenario, load).items():
            await service.deps.memory.apply(client, upserts=facts)
    await service.register(load)
    # Always bootstrap with a location ping; quiet scenarios retain their initial risk.
    ping = scenario.steps[0].ping
    await service.ping(
        key,
        LocationPing(
            ping_id="initial-location",
            load_id=load_id,
            lat=ping.lat if ping else 37.9577,
            lon=ping.lon if ping else -121.2908,
            delay_risk=ping.delay_risk if ping else load.delay_risk,
            occurred_at=datetime.fromisoformat(frozen["started_at"]),
        ),
    )
    return {"load_id": load_id, "broker_id": key.broker_id, "workflow_id": key.workflow_id}


async def inject_step(service: TemporalIngress, root: Path, key: LoadKey, index: int):
    await service._check(key, running=True)
    snapshot = await service.runtime.snapshot(key.load_id)
    frozen = snapshot.load.sop_snapshot or {}
    if "scenario" not in frozen:
        raise DomainError("Esta execução não tem um roteiro de cenário.")
    scenario = Scenario.model_validate(frozen["scenario"])
    if index < 0 or index >= len(scenario.steps):
        raise DomainError("Etapa inválida.")
    step = scenario.steps[index]
    now = service.deps.clock.now()
    if step.ping:
        return await service.ping(
            key,
            LocationPing(
                ping_id="initial-location" if index == 0 else f"step-{index}",
                load_id=key.load_id,
                lat=step.ping.lat,
                lon=step.ping.lon,
                delay_risk=step.ping.delay_risk,
                occurred_at=now,
            ),
        )
    if not step.message:
        raise DomainError("Esta etapa aguarda o timer real; não é possível adiantar o relógio.")
    attachments = []
    for i, spec in enumerate(step.message.attachments):
        path = (root / spec.fixture).resolve()
        if not path.is_relative_to(root.resolve() / "fixtures"):
            raise DomainError("Anexo fora da pasta de fixtures.")
        ref = await service.deps.blobs.put(
            attachment_bytes(root, spec.fixture, snapshot.load, now),
            spec.content_type,
            name=spec.filename,
        )
        attachments.append(
            Attachment(
                attachment_id=f"step-{index}-{i}",
                filename=spec.filename,
                content_type=spec.content_type,
                ref=ref,
            )
        )
    return await service.message(
        key,
        InboundMessage(
            message_id=step.message.message_id or f"step-{index}",
            load_id=key.load_id,
            channel=step.message.channel,
            sender_contact_id=step.message.sender,
            body=live_message(step.message.body, scenario, step, now),
            occurred_at=now,
            attachments=tuple(attachments),
        ),
    )
