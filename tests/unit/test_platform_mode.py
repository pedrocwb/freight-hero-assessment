"""The interactive platform never spends on a live model unless asked to."""

from pathlib import Path
from typing import Any

import pytest

from freight_agent.api.simulation import _interactive_mode  # pyright: ignore[reportPrivateUsage]
from freight_agent.settings import Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No variable from the developer's shell, and no `.env` from the repository root."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)


def _settings(**values: Any) -> Settings:
    return Settings(**{"_env_file": None, **values})


@pytest.mark.unit
def test_a_key_alone_does_not_switch_the_platform_to_the_live_model() -> None:
    assert _interactive_mode(_settings(openrouter_api_key="sk-or-test")) == "replay"


@pytest.mark.unit
def test_live_only_when_asked_for() -> None:
    settings = _settings(openrouter_api_key="sk-or-test", llm_mode="live")

    assert _interactive_mode(settings) == "live"
