"""The platform's model is always the live one. Without `OPENROUTER_API_KEY` the platform
still starts, on a model whose every call fails saying the key is missing, so each run
that needs the model escalates to a Hero with that reason and no event is lost."""

from pathlib import Path
from typing import Any

import pytest

from freight_agent.adapters.llm.resilient import ResilientModel
from freight_agent.adapters.llm.router import RoleRouter
from freight_agent.adapters.llm.unavailable import UnavailableModel
from freight_agent.api.schemas import MessageIn
from freight_agent.api.simulation import SimulationService
from freight_agent.application.escalation import MODEL_FAILURE_TITLE
from freight_agent.domain.load import Channel
from freight_agent.domain.outbox import OutboxKind
from freight_agent.domain.runs import RunStatus
from freight_agent.ports.llm import ModelRole
from freight_agent.settings import Settings

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
KEY = "sk-or-v1-platform-test-secret"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No variable from the developer's shell, and no `.env` from the repository root."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)


def _settings(**values: Any) -> Settings:
    """The shipped models and SOPs, no `.env`, no tracing."""
    paths = {
        "models_config": ROOT / "config" / "models.yaml",
        "sops_dir": ROOT / "sops",
        "prompts_dir": ROOT / "prompts",
    }
    return Settings(**{"_env_file": None, **paths, **values})


async def test_with_the_key_the_platform_runs_on_the_live_router() -> None:
    """Built only: nothing is sent until a run calls the model."""
    service = SimulationService(_settings(openrouter_api_key=KEY))

    model = service.session.inner_model
    assert isinstance(model, RoleRouter)
    assert model.roles == frozenset(ModelRole)
    assert all(isinstance(model.model_for(role), ResilientModel) for role in ModelRole)
    assert (await service.state()).model_ready is True


async def test_without_the_key_the_platform_starts_and_says_the_model_is_not_ready() -> None:
    service = SimulationService(_settings())

    model = service.session.inner_model
    assert isinstance(model, UnavailableModel)
    assert "OPENROUTER_API_KEY is not set" in model.reason
    assert (await service.state()).model_ready is False


async def test_without_the_key_a_run_escalates_saying_the_key_is_missing(
    load_metadata: dict[str, Any],
) -> None:
    service = SimulationService(_settings())
    summary = await service.create_load(load_metadata)
    message = MessageIn(
        load_id=summary.load_id,
        channel=Channel.SMS,
        sender_contact_id="drv_20931",
        body="Running about 2 hours late, stuck in traffic on I-5.",
    )

    await service.ingest_message(message, latency_s=60)

    (run,) = await service.runs(summary.load_id)
    assert run.status is RunStatus.ESCALATED
    assert run.error is not None
    assert "OPENROUTER_API_KEY is not set" in run.error
    assert service.session.model.calls == 1  # the gate's call, which failed
    (task,) = (await service.snapshot(summary.load_id)).outbox
    assert task.kind is OutboxKind.HERO_TASK
    assert task.payload["title"] == MODEL_FAILURE_TITLE
    assert task.payload["urgent"] is True
    assert "OPENROUTER_API_KEY is not set" in task.payload["description"]
