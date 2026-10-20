"""In-memory `BrokerRepository` over the memory store's state.

Owner: card W3-BRK (C0 wrote a working version). Rows live in `_State.brokers` (the
current version per broker) and `_State.broker_versions` (every saved version), so the
store's transactions cover them like every other row.
"""

import builtins
from typing import TYPE_CHECKING

from freight_agent.domain.broker import Broker

if TYPE_CHECKING:
    from freight_agent.adapters.persistence.memory.store import MemoryStore


class MemoryBrokerRepository:
    def __init__(self, store: "MemoryStore") -> None:
        self._s = store

    async def get(self, broker_id: str) -> Broker | None:
        return self._s.state.brokers.get(broker_id)

    async def get_version(self, broker_id: str, version: int) -> Broker | None:
        return self._s.state.broker_versions.get((broker_id, version))

    async def save(self, broker: Broker) -> None:
        state = self._s.state
        state.brokers[broker.broker_id] = broker
        state.broker_versions[(broker.broker_id, broker.config_version)] = broker

    async def versions(self, broker_id: str) -> builtins.list[Broker]:
        found = [b for (owner, _), b in self._s.state.broker_versions.items() if owner == broker_id]
        return sorted(found, key=lambda b: b.config_version)

    async def list(self) -> builtins.list[Broker]:
        brokers = self._s.state.brokers
        return [brokers[broker_id] for broker_id in sorted(brokers)]
