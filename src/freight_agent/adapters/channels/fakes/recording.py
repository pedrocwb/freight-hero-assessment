"""Gateways that record every delivery instead of sending it.

Used by unit tests, the eval harness and the simulation. A repeated
idempotency key is ignored (and counted), like a real integration service.
"""

from dataclasses import dataclass, field
from typing import Any

from freight_agent.domain.load import Stage
from freight_agent.ports.gateways import DeliveryError, Gateways


@dataclass(frozen=True)
class Delivery:
    channel: (
        str  # sms | email | broker_slack | tms_note | hero_task | stage_update | internal_slack
    )
    load_id: str
    idempotency_key: str
    payload: dict[str, Any]


@dataclass
class DeliveryLog:
    deliveries: list[Delivery] = field(default_factory=list)
    seen_keys: set[str] = field(default_factory=set)
    duplicate_attempts: int = 0
    failures_remaining: dict[str, int] = field(default_factory=dict)

    def record(self, channel: str, load_id: str, key: str, payload: dict[str, Any]) -> None:
        if self.failures_remaining.get(channel, 0) > 0:
            self.failures_remaining[channel] -= 1
            raise DeliveryError(f"simulated {channel} failure")
        if key in self.seen_keys:
            self.duplicate_attempts += 1
            return
        self.seen_keys.add(key)
        self.deliveries.append(Delivery(channel, load_id, key, payload))

    def of(self, channel: str) -> list[Delivery]:
        return [d for d in self.deliveries if d.channel == channel]


class _Sms:
    def __init__(self, log: DeliveryLog) -> None:
        self._log = log

    async def send(self, *, load_id: str, contact_id: str, text: str, idempotency_key: str) -> None:
        self._log.record("sms", load_id, idempotency_key, {"contact_id": contact_id, "text": text})


class _Email:
    def __init__(self, log: DeliveryLog) -> None:
        self._log = log

    async def send(
        self,
        *,
        load_id: str,
        to_contact_ids: tuple[str, ...],
        subject: str | None,
        body: str,
        idempotency_key: str,
    ) -> None:
        payload = {"to_contact_ids": list(to_contact_ids), "subject": subject, "body": body}
        self._log.record("email", load_id, idempotency_key, payload)


class _BrokerSlack:
    def __init__(self, log: DeliveryLog) -> None:
        self._log = log

    async def post(self, *, broker_id: str, load_id: str, text: str, idempotency_key: str) -> None:
        payload = {"broker_id": broker_id, "text": text}
        self._log.record("broker_slack", load_id, idempotency_key, payload)


class _Tms:
    def __init__(self, log: DeliveryLog) -> None:
        self._log = log

    async def write_note(self, *, load_id: str, text: str, idempotency_key: str) -> None:
        self._log.record("tms_note", load_id, idempotency_key, {"text": text})


class _Internal:
    def __init__(self, log: DeliveryLog) -> None:
        self._log = log

    async def create_hero_task(
        self, *, load_id: str, title: str, description: str, urgent: bool, idempotency_key: str
    ) -> None:
        payload = {"title": title, "description": description, "urgent": urgent}
        self._log.record("hero_task", load_id, idempotency_key, payload)

    async def update_stage(self, *, load_id: str, stage: Stage, idempotency_key: str) -> None:
        self._log.record("stage_update", load_id, idempotency_key, {"stage": stage.value})

    async def post_internal_slack(self, *, load_id: str, text: str, idempotency_key: str) -> None:
        self._log.record("internal_slack", load_id, idempotency_key, {"text": text})


class RecordingGateways:
    def __init__(self) -> None:
        self.log = DeliveryLog()

    def as_gateways(self) -> Gateways:
        return Gateways(
            sms=_Sms(self.log),
            email=_Email(self.log),
            broker_slack=_BrokerSlack(self.log),
            tms=_Tms(self.log),
            internal=_Internal(self.log),
        )

    def fail_next(self, channel: str, times: int = 1) -> None:
        self.log.failures_remaining[channel] = times
