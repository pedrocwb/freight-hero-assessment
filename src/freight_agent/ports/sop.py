"""SOP source port."""

from typing import Protocol

from freight_agent.domain.sop import SopSection


class SopRepository(Protocol):
    async def sections_for_client(self, client_id: str) -> list[SopSection]:
        """Standard sections plus every section of this client, including its load-type layers."""
        ...

    def version(self) -> str:
        """A content hash of the SOP files, recorded with every run."""
        ...
