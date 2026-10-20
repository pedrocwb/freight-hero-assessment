"""`python -m freight_agent.api` serves the app with uvicorn."""

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from freight_agent.api import __main__ as entry
from freight_agent.settings import Settings

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def offline_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No variable from the developer's shell and no `.env`: an untraced app without the
    model key (it starts anyway; its runs would escalate). The models file is the
    repository's, since the working directory is a temporary one."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("MODELS_CONFIG", str(ROOT / "config" / "models.yaml"))
    monkeypatch.setenv("PROMPTS_DIR", str(ROOT / "prompts"))
    monkeypatch.chdir(tmp_path)


def test_serves_on_port_8000_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: calls.append((app, kw)))

    entry.main([])

    [(app, options)] = calls
    assert isinstance(app, FastAPI)
    assert (options["host"], options["port"]) == ("127.0.0.1", 8000)


def test_host_and_port_can_be_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: calls.append(kw))

    entry.main(["--host", "0.0.0.0", "--port", "8123"])

    assert (calls[0]["host"], calls[0]["port"]) == ("0.0.0.0", 8123)
