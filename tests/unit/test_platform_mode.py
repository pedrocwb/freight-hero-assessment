"""Which model the interactive platform uses when LLM_MODE is not set."""

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
def test_the_live_model_when_its_key_is_set_and_no_mode_is_chosen() -> None:
    assert _interactive_mode(_settings(openrouter_api_key="sk-or-test")) == "live"


@pytest.mark.unit
def test_replay_without_a_key() -> None:
    assert _interactive_mode(_settings()) == "replay"


@pytest.mark.unit
def test_an_explicit_mode_wins() -> None:
    settings = _settings(openrouter_api_key="sk-or-test", llm_mode="scripted")

    assert _interactive_mode(settings) == "scripted"
