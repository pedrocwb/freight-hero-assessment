"""Broker workspaces: editable layered SOPs and immutable execution snapshots."""

import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from freight_agent.adapters.sop.files import FileSopRepository
from freight_agent.domain.broker import Broker
from freight_agent.domain.errors import DomainError
from freight_agent.domain.sop import SopLayer, SopSection, effective_sections

BROKERS = {
    "northline": ("Northline Logistics", "Cliente A · enunciado"),
    "keystone": ("Keystone Freight", "Cliente B · enunciado"),
    "aurora": ("Aurora Transportes", "Teste extremo · 10 SMS"),
    "acme": ("Acme Logistics", "Documentos e procedimentos adicionais"),
}

ETA_BODY = """When delay risk becomes high, ask the driver for the ETA at the next stop by
SMS with requests="eta_request". If a request is already open, keep its timer and
do not send another request immediately.

On eta_follow_up_due, consult the obligation ledger. While attempts is below
{{eta_request.max_attempts}}, send exactly one new ETA request. After each accepted
SMS, including the last one, wait {{eta_request.follow_up_minutes}} minutes.
Delivery confirmation starts the timer; never send several requests at once.

After {{eta_request.max_attempts}} unanswered requests and the final wait, escalate
in this order: {{eta_request.on_exhausted}}. Include load, destination and missing ETA.
Use no other escalation channel. Close eta_request as exhausted.
An ETA satisfies the obligation. A question or acknowledgement does not: answer
questions from the load metadata while preserving the ETA request and its timer.
"""


class BrokerProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(default=1, ge=1)
    broker: Broker
    sections: tuple[SopSection, ...]

    def checked(self, broker_id: str, original: "BrokerProfile | None" = None) -> "BrokerProfile":
        if broker_id not in BROKERS or self.broker.broker_id != broker_id:
            raise DomainError("Broker inválido para esta configuração.")
        if original and tuple(s for s in self.sections if s.layer == SopLayer.STANDARD) != tuple(
            s for s in original.sections if s.layer == SopLayer.STANDARD
        ):
            raise DomainError(
                "O SOP padrão é herdado. Crie uma substituição ou extensão do cliente."
            )
        if len(self.sections) > 100:
            raise DomainError("Limite de 100 seções por broker.")
        known = {intent.intent_id for intent in self.broker.intents}
        for section in self.sections:
            if section.layer != SopLayer.STANDARD and section.client_id != broker_id:
                raise DomainError("Uma seção não pode pertencer a outro broker.")
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", section.section_id):
                raise DomainError("Identificador de seção inválido.")
            if not section.body.strip() or len(section.body) > 20000 or not section.title.strip():
                raise DomainError("Preencha título e texto do SOP (até 20.000 caracteres).")
            if not set(section.intents) <= known:
                raise DomainError("O SOP referencia um evento que não existe neste broker.")
            if section.layer == SopLayer.LOAD_TYPE and not section.load_type:
                raise DomainError("Informe o tipo de carga da extensão.")
        for types in [(), *((s.load_type,) for s in self.sections if s.load_type)]:
            effective_sections(self.sections, client_id=broker_id, load_types=types)
        return self


async def default_profile(root: Path, broker_id: str) -> BrokerProfile:
    if broker_id not in BROKERS:
        raise DomainError("Broker não encontrado.")
    files = FileSopRepository(root)
    broker = files.broker(broker_id).revised(broker_id=broker_id, name=BROKERS[broker_id][0])
    if broker_id == "keystone":
        broker = broker.revised(
            obligations=tuple(
                o.model_copy(update={"max_attempts": 1, "on_exhausted": ("broker_slack",)})
                if o.kind == "eta_request"
                else o
                for o in broker.obligations
            )
        )
    sections = await files.sections_for_client(broker_id)
    # The original procedure is preserved in meaning; parameters have a single source
    # of truth in the broker catalog, also enforced by deterministic action validation.
    sections = [
        s.model_copy(update={"body": ETA_BODY})
        if s.layer == SopLayer.CLIENT and s.section_id == "delay_risk_high"
        else s
        for s in sections
    ]
    return BrokerProfile(broker=broker, sections=tuple(sections)).checked(broker_id)


def freeze_profile(profile: BrokerProfile) -> dict:
    values: dict[str, str] = {}
    targets = {
        "hero_task": "create an urgent Hero task to call the driver",
        "dispatcher_email": "email the dispatcher in the main thread",
        "broker_slack": "post an alert in the broker Slack channel (never contact the dispatcher)",
    }
    for obligation in profile.broker.obligations:
        values.update(
            {
                f"{obligation.kind}.max_attempts": str(obligation.max_attempts),
                f"{obligation.kind}.follow_up_minutes": str(obligation.follow_up_minutes),
                f"{obligation.kind}.on_exhausted": "; then ".join(
                    targets[t] for t in obligation.on_exhausted
                ),
            }
        )
    sections = []
    for section in profile.sections:
        body = section.body
        for token, value in values.items():
            body = body.replace("{{" + token + "}}", value)
        if re.search(r"\{\{[^}]+\}\}", body):
            raise DomainError("Há uma variável desconhecida no texto do SOP.")
        sections.append(section.model_copy(update={"body": body}).model_dump(mode="json"))
    broker = profile.broker
    eta = broker.obligation("eta_request")
    if eta:
        # A custom ten-attempt SOP must fit the rate window, with room for a PO reply.
        limits = broker.limits.model_copy(
            update={
                "validation": broker.limits.validation.model_copy(
                    update={
                        "sms_max_per_window": max(
                            broker.limits.validation.sms_max_per_window, eta.max_attempts + 1
                        )
                    }
                )
            }
        )
        broker = broker.revised(limits=limits)
    data = {
        "broker": broker.model_dump(mode="json"),
        "sections": sections,
        "profile_revision": profile.revision,
    }
    data["version"] = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]
    return data
