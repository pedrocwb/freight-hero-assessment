"""`agent serve`: wiring only (no server)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from freight_agent import cli
from freight_agent.settings import Settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.mark.unit
def test_serve_passes_host_and_port_to_the_api_server(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr("freight_agent.api.__main__.main", seen.append)

    result = runner.invoke(cli.app, ["serve", "--host", "0.0.0.0", "--port", "9000"])

    assert result.exit_code == 0, result.output
    assert seen == [["--host", "0.0.0.0", "--port", "9000"]]


@pytest.mark.unit
def test_the_platform_commands_are_registered() -> None:
    """What the README, the Dockerfile (`ENTRYPOINT ["agent"]`) and compose call."""
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0, result.output
    for command in ("serve", "eval", "sop"):
        assert command in result.output
