"""Brokers as data: create, read, change, list versions, roll out (`/api/brokers`).

Owner: card W3-BRK. Bodies: `api.schemas.BrokerIn`, `BrokerView`, `BrokerVersionView`,
`RolloutIn`. The brokers live in the session's store, the one its runs read
(`SimulationService.uow_factory`), so a change applies to the next run.

- `POST /api/brokers` creates a broker as a copy of the standard catalog plus the
  fields given, at `config_version` 1. 409 when the id exists or is `standard`.
- `GET /api/brokers` lists the standard catalog first, then every saved broker by id.
- `GET /api/brokers/{broker_id}` is the current version; `standard` is the standard
  catalog (code) unless a broker of that id was saved.
- `PUT /api/brokers/{broker_id}` saves the changed fields as a new `config_version`,
  live for every load (`rollout = {percent: 100, previous_version: <old>}`); the old
  version stays in `versions`. A change that changes nothing saves nothing, so
  repeating a PUT is harmless. The standard catalog is code: 409.
- `GET /api/brokers/{broker_id}/versions` lists every saved version, oldest first.
- `POST /api/brokers/{broker_id}/rollout` sets the share of loads on the current
  version (a canary; 0 is a rollback) and the earlier version the others run on. It
  re-saves the current version rather than adding one: the configuration is the same,
  only the split changes. 409 when that earlier version was never saved, is not
  earlier, or there is none and the percent is below 100.

On a create or a change, each obligation whose follow-up intent the catalog lacks gets
it as a system intent (its timer emits it, `domain.broker.with_follow_up_intents`).
A catalog that cannot work (`domain.broker.catalog_problems`) is refused with 422 and
every problem named; nothing is saved.
"""

from collections.abc import Callable
from typing import Any, Final

from fastapi import APIRouter
from pydantic import ValidationError

from freight_agent.api.deps import Service
from freight_agent.api.errors import BadRequest, Conflict, NotFound, Unprocessable
from freight_agent.api.schemas import BrokerIn, BrokerVersionView, BrokerView, RolloutIn
from freight_agent.domain.broker import (
    STANDARD_BROKER_ID,
    Broker,
    Rollout,
    new_broker,
    standard_broker,
    with_follow_up_intents,
)
from freight_agent.ports.brokers import BrokerRepository

router = APIRouter(prefix="/brokers", tags=["brokers"])

# The fields of `BrokerIn` a create or a change copies onto the broker when given.
_CATALOG_FIELDS: Final = (
    "name",
    "triage",
    "intents",
    "obligations",
    "document_types",
    "documents",
    "limits",
)


def _view(broker: Broker) -> BrokerView:
    return BrokerView.model_validate(broker.model_dump())


def _checked(build: Callable[[], Broker]) -> Broker:
    """`build()`, or a 422 that names every problem of the catalog it built."""
    try:
        return build()
    except ValidationError as exc:
        raise Unprocessable(_problems(exc)) from exc


def _problems(exc: ValidationError) -> str:
    """The catalog's problems as one line; a field error with its location."""
    parts: list[str] = []
    for error in exc.errors():
        message = str(error["msg"]).removeprefix("Value error, ")
        where = ".".join(str(part) for part in error["loc"])
        parts.append(f"{where}: {message}" if where else message)
    return "; ".join(parts)


def _revise(broker: Broker, body: BrokerIn) -> Broker:
    """`broker` with the fields `body` gives, the follow-up intents of its obligations
    included, validated."""
    given = {name: getattr(body, name) for name in _CATALOG_FIELDS}
    changes: dict[str, Any] = {name: value for name, value in given.items() if value is not None}
    intents = changes.get("intents", broker.intents)
    obligations = changes.get("obligations", broker.obligations)
    changes["intents"] = with_follow_up_intents(intents, obligations)
    return _checked(lambda: broker.revised(**changes))


async def _current(brokers: BrokerRepository, broker_id: str) -> Broker:
    broker = await brokers.get(broker_id)
    if broker is not None:
        return broker
    if broker_id == STANDARD_BROKER_ID:
        return standard_broker()
    raise NotFound(f"no broker {broker_id!r}: create it with POST /api/brokers")


async def _saved(brokers: BrokerRepository, broker_id: str) -> Broker:
    broker = await brokers.get(broker_id)
    if broker is None:
        raise NotFound(f"broker {broker_id!r} was never saved")
    return broker


@router.post("", status_code=201)
async def create_broker(service: Service, body: BrokerIn) -> BrokerView:
    """A new broker: the standard catalog under its id, with the fields given."""
    if body.broker_id is None or body.name is None:
        raise Unprocessable("broker_id and name are required to create a broker")
    broker_id, name = body.broker_id, body.name
    if broker_id == STANDARD_BROKER_ID:
        raise Conflict("standard is the standard catalog: pick another broker id")
    broker = _revise(_checked(lambda: new_broker(broker_id, name)), body)
    async with service.uow_factory() as uow:
        if await uow.brokers.get(broker_id) is not None:
            raise Conflict(f"broker {broker_id!r} exists: change it with PUT")
        await uow.brokers.save(broker)
        await uow.commit()
    return _view(broker)


@router.get("")
async def list_brokers(service: Service) -> list[BrokerView]:
    """The standard catalog, then every saved broker by id."""
    async with service.uow_factory() as uow:
        saved = await uow.brokers.list()
    standard = next((b for b in saved if b.broker_id == STANDARD_BROKER_ID), standard_broker())
    return [_view(standard), *(_view(b) for b in saved if b.broker_id != STANDARD_BROKER_ID)]


@router.get("/{broker_id}")
async def get_broker(service: Service, broker_id: str) -> BrokerView:
    async with service.uow_factory() as uow:
        return _view(await _current(uow.brokers, broker_id))


@router.put("/{broker_id}")
async def change_broker(service: Service, broker_id: str, body: BrokerIn) -> BrokerView:
    """Save the changed fields as a new version, live for every load."""
    if body.broker_id is not None and body.broker_id != broker_id:
        raise BadRequest(
            f"the body names broker {body.broker_id!r} but the path {broker_id!r}: a broker"
            " id never changes"
        )
    if broker_id == STANDARD_BROKER_ID:
        raise Conflict("the standard catalog is code: create a broker to change it")
    async with service.uow_factory() as uow:
        current = await _saved(uow.brokers, broker_id)
        candidate = _revise(current, body)
        if candidate == current:
            return _view(current)  # nothing changes: no new version
        version = current.config_version
        changed = _checked(
            lambda: candidate.revised(
                config_version=version + 1,
                rollout=Rollout(percent=100, previous_version=version),
            )
        )
        await uow.brokers.save(changed)
        await uow.commit()
    return _view(changed)


@router.get("/{broker_id}/versions")
async def broker_versions(service: Service, broker_id: str) -> list[BrokerVersionView]:
    """Every saved version, oldest first; `current` marks the one runs start from."""
    async with service.uow_factory() as uow:
        current = await _current(uow.brokers, broker_id)
        versions = await uow.brokers.versions(broker_id) or [current]
    return [
        BrokerVersionView.of(b, current=b.config_version == current.config_version)
        for b in versions
    ]


@router.post("/{broker_id}/rollout")
async def roll_out(service: Service, broker_id: str, body: RolloutIn) -> BrokerView:
    """Split the loads between the current version and an earlier one."""
    async with service.uow_factory() as uow:
        current = await _saved(uow.brokers, broker_id)
        version = current.config_version
        previous = body.previous_version
        if previous is None:  # left out: the rollout's own
            previous = current.rollout.previous_version
        if previous is None and body.percent < 100:
            raise Conflict(
                f"broker {broker_id!r} has no earlier version than {version} for the other"
                f" {100 - body.percent}% of the loads"
            )
        if previous is not None:
            if await uow.brokers.get_version(broker_id, previous) is None:
                raise Conflict(f"broker {broker_id!r} has no version {previous}")
            if previous >= version:
                raise Conflict(
                    f"previous_version must be earlier than the current version {version},"
                    f" not {previous}"
                )
        rolled = _checked(
            lambda: current.revised(
                rollout=Rollout(percent=body.percent, previous_version=previous)
            )
        )
        await uow.brokers.save(rolled)
        await uow.commit()
    return _view(rolled)
