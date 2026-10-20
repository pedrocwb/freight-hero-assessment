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


@pytest.mark.unit
def test_db_migrate_sends_ddl_to_the_direct_url_and_lists_what_it_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pooled URL serves the app; migrations go to the direct endpoint when set."""
    seen: list[str | None] = []

    def migrate(conninfo: str | None = None) -> list[str]:
        seen.append(conninfo)
        return ["0003_brokers.sql", "0004_knowledge.sql"]

    monkeypatch.setattr("freight_agent.adapters.persistence.postgres.migrate.migrate", migrate)
    monkeypatch.setenv("DATABASE_URL", "postgresql://agent:pooled@pooler.example/agent")
    monkeypatch.setenv("DATABASE_URL_DIRECT", "postgresql://agent:direct@db.example/agent")

    result = runner.invoke(cli.app, ["db", "migrate"])

    assert result.exit_code == 0, result.output
    assert seen == ["postgresql://agent:direct@db.example/agent"]
    assert "applied 2 migration(s): 0003_brokers.sql, 0004_knowledge.sql" in result.output
    assert "direct" not in result.output  # no URL, so no password, in the output


@pytest.mark.unit
def test_db_migrate_reports_an_up_to_date_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "freight_agent.adapters.persistence.postgres.migrate.migrate",
        lambda conninfo=None: [],
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://agent:pooled@localhost:5432/agent")

    result = runner.invoke(cli.app, ["db", "migrate"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "applied 0 migration(s)"


@pytest.mark.unit
def test_the_platform_commands_are_registered() -> None:
    """What the README, the Dockerfile (`ENTRYPOINT ["agent"]`) and compose call."""
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0, result.output
    for command in ("serve", "db", "eval", "sop"):
        assert command in result.output
