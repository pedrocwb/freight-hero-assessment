"""Postgres `BrokerRepository` (tables `brokers` and `broker_versions`, migration 0003).

Owner: card W3-BRK. C0 stub: no broker is stored yet, so `get` and `get_version` find
none and every run follows the standard catalog (`application.context`), as before wave
3; the other methods raise until the card lands.
"""

import builtins

from freight_agent.adapters.persistence.postgres.connection import Session
from freight_agent.domain.broker import Broker

_CARD = "card BRK"


class PostgresBrokerRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    async def get(self, broker_id: str) -> Broker | None:
        return None  # C0: no broker table yet; the run context falls back to the standard

    async def get_version(self, broker_id: str, version: int) -> Broker | None:
        return None

    async def save(self, broker: Broker) -> None:
        raise NotImplementedError(_CARD)

    async def versions(self, broker_id: str) -> builtins.list[Broker]:
        raise NotImplementedError(_CARD)

    async def list(self) -> builtins.list[Broker]:
        raise NotImplementedError(_CARD)
