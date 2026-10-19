"""`agent serve` and `agent db migrate`: wiring only (no server, no database)."""

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
def test_db_migrate_without_a_database_url_says_which_variable_to_set() -> None:
    result = runner.invoke(cli.app, ["db", "migrate"])

    assert result.exit_code == 2
    assert "DATABASE_URL" in result.output
