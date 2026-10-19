"""Outbound ports, one per integration service (interface segregation).

Every call carries an idempotency key; implementations must ignore a repeated key.
The agent addresses people by contact id; resolving phone numbers and emails is
the message service's job.
"""

from dataclasses import dataclass
from typing import Protocol

from freight_agent.domain.load import Stage


class DeliveryError(Exception):
    """The integration service rejected or failed the call. The outbox will retry."""


class SmsGateway(Protocol):
    async def send(
        self, *, load_id: str, contact_id: str, text: str, idempotency_key: str
    ) -> None: ...


class EmailGateway(Protocol):
    async def send(
        self,
        *,
        load_id: str,
        to_contact_ids: tuple[str, ...],
        subject: str | None,
        body: str,
        idempotency_key: str,
    ) -> None:
        """Send in the load's main email thread."""
        ...


class BrokerSlackGateway(Protocol):
    async def post(
        self, *, broker_id: str, load_id: str, text: str, idempotency_key: str
    ) -> None: ...


class TmsClient(Protocol):
    async def write_note(self, *, load_id: str, text: str, idempotency_key: str) -> None: ...


class InternalSystem(Protocol):
    async def create_hero_task(
        self, *, load_id: str, title: str, description: str, urgent: bool, idempotency_key: str
    ) -> None: ...

    async def update_stage(self, *, load_id: str, stage: Stage, idempotency_key: str) -> None: ...

    async def post_internal_slack(
        self, *, load_id: str, text: str, idempotency_key: str
    ) -> None: ...


@dataclass(frozen=True)
class Gateways:
    sms: SmsGateway
    email: EmailGateway
    broker_slack: BrokerSlackGateway
    tms: TmsClient
    internal: InternalSystem
