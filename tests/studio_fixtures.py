"""Offline scenario A inputs for the Studio graph tests."""

from pathlib import Path
from typing import Any

from freight_agent.domain.actions import (
    Action,
    CloseObligation,
    CloseStatus,
    CreateHeroTask,
    Plan,
    Purpose,
    ScheduleTimer,
    SendEmail,
    SendSms,
)
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.obligations import ObligationKind
from freight_agent.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
DRIVER = "drv_20931"
DISPATCHER = "dsp_4410"

ASK_ETA = SendSms(
    contact_id=DRIVER,
    text="Hi, this is Northline Logistics. What time will you arrive at the pickup?",
    purpose=Purpose.ETA_REQUEST,
)
ASK_AGAIN = SendSms(
    contact_id=DRIVER,
    text="Following up: what time will you arrive at the pickup?",
    purpose=Purpose.ETA_REQUEST,
)
PO_REPLY = SendSms(
    contact_id=DRIVER,
    text="The PO number is 55821. You need it at the gate.",
    purpose=Purpose.REPLY,
)
FOLLOW_UP_IN_30 = ScheduleTimer(kind=ObligationKind.ETA_REQUEST, due_in_minutes=30)
CALL_DRIVER = CreateHeroTask(
    title="Call the driver for the ETA",
    description="Two ETA texts got no answer; the dispatcher was emailed.",
    urgent=True,
)
EMAIL_DISPATCHER = SendEmail(
    to_contact_ids=(DISPATCHER,),
    subject="Load 481207: driver ETA needed",
    body="Your driver does not answer. Please send the ETA.",
    purpose=Purpose.ESCALATION,
)
EXHAUSTED = CloseObligation(kind=ObligationKind.ETA_REQUEST, status=CloseStatus.EXHAUSTED)
QUESTION = GateDecision(intent=Intent.QUESTION, act=True, reason="The driver asks for the PO.")


def local(hour: int, minute: int, second: int = 0) -> str:
    """An ISO time on the scenario day, 2026-10-13, as the API writes it (PDT)."""
    return f"2026-10-13T{hour:02d}:{minute:02d}:{second:02d}-07:00"


def plan(*actions: Action) -> Plan:
    """The agent's rounds for these actions: one round stages them, the next finishes."""
    return Plan(actions=actions, rationale="queued for the Studio tests")


def studio_settings(**overrides: Any) -> Settings:
    """The repository's SOPs, evals and models; no `.env`, no key, no tracing. The gate
    runs on its chat model (`llm`), where these tests script its answers on the fake."""
    values: dict[str, Any] = {
        "gate_engine": "llm",
        "langsmith_tracing": False,
        "openrouter_api_key": None,
        "langsmith_api_key": None,
        "sops_dir": ROOT / "sops",
        "evals_dir": ROOT / "evals",
        "models_config": ROOT / "config" / "models.yaml",
        "prompts_dir": ROOT / "prompts",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]
