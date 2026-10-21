"""The parts of the Postgres migration runner that need no database."""

from pathlib import Path

import pytest

from freight_agent.adapters.persistence.postgres.migrate import (
    MIGRATIONS_DIR,
    MigrationError,
    apply_migrations,
    discover,
)

pytestmark = pytest.mark.unit


def _write(directory: Path, *names: str) -> Path:
    for name in names:
        (directory / name).write_text("SELECT 1;\n")
    return directory


def test_migrations_are_ordered_by_number_not_by_name(tmp_path: Path) -> None:
    _write(tmp_path, "0010_later.sql", "0002_sooner.sql", "README.md")

    found = discover(tmp_path)

    assert [(m.version, m.filename) for m in found] == [
        (2, "0002_sooner.sql"),
        (10, "0010_later.sql"),
    ]


@pytest.mark.parametrize("name", ["1_short.sql", "0001-dash.sql", "0001_Upper.sql", "0001.sql"])
def test_a_misnamed_migration_is_refused(tmp_path: Path, name: str) -> None:
    _write(tmp_path, name)
    with pytest.raises(MigrationError, match=name):
        discover(tmp_path)


def test_two_migrations_with_one_number_are_refused(tmp_path: Path) -> None:
    _write(tmp_path, "0001_one.sql", "0001_other.sql")
    with pytest.raises(MigrationError, match="0001"):
        discover(tmp_path)


def test_the_checksum_follows_the_file_content(tmp_path: Path) -> None:
    _write(tmp_path, "0001_one.sql")
    before = discover(tmp_path)[0].checksum
    (tmp_path / "0001_one.sql").write_text("SELECT 2;\n")
    assert discover(tmp_path)[0].checksum != before


def test_the_repo_migrations_start_at_one_and_enable_pgvector() -> None:
    found = discover(MIGRATIONS_DIR)
    assert found and found[0].version == 1
    assert any("CREATE EXTENSION IF NOT EXISTS vector" in m.sql for m in found)


@pytest.mark.parametrize("schema", ["Upper", "two words", "x; DROP SCHEMA public", "", "9lives"])
async def test_a_schema_name_that_is_not_a_plain_identifier_is_refused(schema: str) -> None:
    with pytest.raises(ValueError, match="schema"):
        await apply_migrations("postgresql://never.connects.invalid/db", schema=schema)
