"""Read immutable SOP snapshots, including loads created before broker workspaces."""

from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.domain.broker import Broker
from freight_agent.domain.sop import SopSection


def pinned_index(snapshot: dict) -> MemorySopIndex:
    return MemorySopIndex(
        [SopSection.model_validate(s) for s in snapshot["sections"]],
        brokers=[Broker.model_validate(snapshot["broker"])],
        version=snapshot["version"],
    )
