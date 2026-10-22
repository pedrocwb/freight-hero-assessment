"""Editable demo policy, frozen with its complete effective catalog for each load."""

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.domain.broker import Broker
from freight_agent.domain.sop import SopLayer, SopSection
from freight_agent.interfaces.knowledge import SopIndex

DEFAULT_BODY = """Escreva os SMS e tarefas em português brasileiro.
Ao detectar risco alto de atraso, peça a previsão de chegada ao motorista por SMS,
usando send_sms com requests="eta_request". Se já houver um pedido aberto, mantenha
seu timer e não envie outro SMS imediatamente.

Quando eta_follow_up_due ocorrer, consulte as tentativas da obrigação:
- Enquanto attempts for menor que {{max_attempts}}, envie exatamente um novo pedido.
- Quando attempts atingir {{max_attempts}}, não envie outro SMS. Abra exatamente uma
  tarefa urgente para o hero ligar ao motorista, informe carga e destino e encerre
  eta_request com close_obligation(status="exhausted").

Aguarde {{interval_minutes}} minuto(s) após cada SMS confirmado, inclusive o último,
antes de decidir o próximo passo. A confirmação de entrega agenda esse timer.
Nunca envie vários pedidos de uma vez. Não envie e-mail ou Slack neste procedimento.
Uma resposta com previsão de chegada satisfaz a obrigação e cancela os pedidos.
Outras mensagens seguem seu SOP específico e não contam como nova tentativa."""


class DemoPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    max_attempts: int = Field(default=10, ge=1, le=10, strict=True)
    interval_minutes: int = Field(default=1, ge=1, le=60, strict=True)
    body: str = Field(default=DEFAULT_BODY, min_length=20, max_length=12000)


async def freeze_policy(knowledge: SopIndex, policy: DemoPolicy) -> dict:
    broker_data = (await knowledge.broker("aurora")).model_dump(mode="json")
    broker_data["broker_id"] = "aurora"
    for definition in broker_data["obligations"]:
        if definition["kind"] == "eta_request":
            definition.update(
                max_attempts=policy.max_attempts,
                follow_up_minutes=policy.interval_minutes,
                on_exhausted=["hero_task"],
            )
    # The obligation caps ETA requests; keep room for replies to driver questions.
    validation = broker_data["limits"]["validation"]
    validation["sms_max_per_window"] = max(validation["sms_max_per_window"], policy.max_attempts)
    broker = Broker.model_validate(broker_data)
    sections = []
    rendered = policy.body.replace("{{max_attempts}}", str(policy.max_attempts)).replace(
        "{{interval_minutes}}", str(policy.interval_minutes)
    )
    for section in await knowledge.visible("aurora", ()):
        data = section.model_dump(exclude={"sources"})
        if section.section_id == "delay_risk_high":
            data.update(
                title="Aurora: procedimento desta execução",
                description="Pedidos de ETA e escalada conforme o SOP editado.",
                intents=("delay_risk_high", "eta_follow_up_due"),
                body=rendered,
            )
        sections.append(
            SopSection(
                **data, layer=SopLayer.CLIENT, client_id="aurora", source="execution-snapshot"
            ).model_dump(mode="json")
        )
    value = {
        "policy": policy.model_dump(),
        "broker": broker.model_dump(mode="json"),
        "sections": sections,
    }
    value["version"] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:12]
    return value


def pinned_index(snapshot: dict) -> MemorySopIndex:
    return MemorySopIndex(
        [SopSection.model_validate(s) for s in snapshot["sections"]],
        brokers=[Broker.model_validate(snapshot["broker"])],
        version=snapshot["version"],
    )
