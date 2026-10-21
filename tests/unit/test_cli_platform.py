"""`agent serve` and `agent db migrate`: wiring only (no server, no database)."""

from pathlib import Path

import pytest
from psycopg import OperationalError
from typer.testing import CliRunner

from freight_agent import cli
from freight_agent.adapters.persistence.postgres.migrate import MigrationError
from freight_agent.settings import Settings

runner = CliRunner()
MODELS_YAML = Path(__file__).resolve().parents[2] / "config" / "models.yaml"


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
    for command in ("serve", "eval", "sop", "db"):
        assert command in result.output


@pytest.mark.unit
def test_db_migrate_applies_the_migrations_to_the_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    stores: list[tuple[str, int]] = []

    def migrate(url: str) -> list[str]:
        seen.append(url)
        return ["0001_load_memory.sql"]

    async def setup_store(url: str, dims: int) -> None:
        stores.append((url, dims))

    monkeypatch.setattr("freight_agent.adapters.persistence.postgres.migrate.migrate", migrate)
    monkeypatch.setattr("freight_agent.adapters.memory.postgres.setup_store", setup_store)
    monkeypatch.setenv("DATABASE_URL", "postgresql://agent:agent@db:5432/agent")
    monkeypatch.setenv("MODELS_CONFIG", str(MODELS_YAML))

    result = runner.invoke(cli.app, ["db", "migrate"])

    assert result.exit_code == 0, result.output
    assert seen == ["postgresql://agent:agent@db:5432/agent"]
    # Then the facility memory's store, at the width of the memory embeddings model.
    assert stores == [("postgresql://agent:agent@db:5432/agent", 1536)]
    assert "applied 1 migration(s): 0001_load_memory.sql" in result.output
    assert "facility memory store is set up (1536-dimension vectors)" in result.output


@pytest.mark.unit
def test_db_migrate_without_a_database_url_says_which_variable_to_set() -> None:
    result = runner.invoke(cli.app, ["db", "migrate"])

    assert result.exit_code == 2
    assert "DATABASE_URL is not set" in result.output


@pytest.mark.unit
def test_db_migrate_on_an_unreachable_database_names_no_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def migrate(url: str) -> list[str]:
        raise OperationalError('connection to server at "db", port 5432 failed\nmore detail')

    monkeypatch.setattr("freight_agent.adapters.persistence.postgres.migrate.migrate", migrate)
    monkeypatch.setenv("DATABASE_URL", "postgresql://agent:s3cret@db:5432/agent")

    result = runner.invoke(cli.app, ["db", "migrate"])

    assert result.exit_code == 2
    assert "cannot reach the database: connection to server" in result.output
    assert "s3cret" not in result.output and "more detail" not in result.output


@pytest.mark.unit
def test_db_migrate_refuses_a_bad_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    def migrate(url: str) -> list[str]:
        raise MigrationError("0001_load_memory.sql changed after it was applied")

    monkeypatch.setattr("freight_agent.adapters.persistence.postgres.migrate.migrate", migrate)
    monkeypatch.setenv("DATABASE_URL", "postgresql://agent:agent@db:5432/agent")

    result = runner.invoke(cli.app, ["db", "migrate"])

    assert result.exit_code == 2
    assert "error: 0001_load_memory.sql changed" in result.output
