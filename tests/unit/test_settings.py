import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from freight_agent.settings import MissingSetting, Settings

ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.example"
VARIABLES = tuple(name.upper() for name in Settings.model_fields)
SECRETS = {
    "OPENROUTER_API_KEY": "sk-or-v1-settings-test-secret",
    "DATABASE_URL": "postgresql://agent:pooled-password@db.example/agent",
    "DATABASE_URL_DIRECT": "postgresql://agent:direct-password@db.example/agent",
    "LANGSMITH_API_KEY": "lsv2_pt_settings-test-secret",
    "TELEGRAM_BOT_TOKEN": "123456:telegram-test-secret",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No variable from the developer's shell, and no `.env` from the repository root."""
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def _settings(env_file: Path | None = None, **values: Any) -> Settings:
    """`_env_file` is a pydantic-settings init option that type checkers cannot see."""
    options: dict[str, Any] = {"_env_file": env_file, **values}
    return Settings(**options)


def _with_every_secret(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    return _settings()


# --- sources -------------------------------------------------------------------


@pytest.mark.unit
def test_the_defaults_run_offline_on_memory_and_replay() -> None:
    settings = _settings()

    assert settings.persistence == "memory"
    assert settings.llm_mode == "replay"
    assert settings.models_config == Path("config/models.yaml")
    assert settings.sops_dir == Path("sops")
    assert settings.evals_dir == Path("evals")
    assert settings.cassettes_dir == Path("evals/cassettes")
    assert settings.web_dist == Path("web/dist")
    assert settings.blobs_dir == Path("blobs")
    assert settings.openrouter_api_key is None
    assert settings.database_url is None
    assert settings.langsmith_tracing is False
    assert settings.langsmith_project == "freight-agent"


@pytest.mark.unit
def test_settings_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRETS["OPENROUTER_API_KEY"])
    monkeypatch.setenv("PERSISTENCE", "postgres")
    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_PROJECT", "freight-agent-evals")
    monkeypatch.setenv("SOPS_DIR", "/srv/sops")
    monkeypatch.setenv("CASSETTES_DIR", "/srv/cassettes")
    monkeypatch.setenv("BLOBS_DIR", "/srv/blobs")

    settings = _settings()

    assert settings.require_openrouter_key().get_secret_value() == SECRETS["OPENROUTER_API_KEY"]
    assert settings.persistence == "postgres"
    assert settings.llm_mode == "live"
    assert settings.langsmith_tracing is True
    assert settings.langsmith_project == "freight-agent-evals"
    assert settings.sops_dir == Path("/srv/sops")
    assert settings.cassettes_dir == Path("/srv/cassettes")
    assert settings.blobs_dir == Path("/srv/blobs")


@pytest.mark.unit
def test_settings_read_the_dotenv_file_of_the_working_directory(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        f"OPENROUTER_API_KEY={SECRETS['OPENROUTER_API_KEY']}\n"
        f"DATABASE_URL={SECRETS['DATABASE_URL']}\n"
        "LLM_MODE=scripted\n"
        "MODELS_CONFIG=custom/models.yaml\n",
        encoding="utf-8",
    )

    settings = Settings()  # the default env_file, `.env` in the working directory (tmp_path)

    assert settings.require_openrouter_key().get_secret_value() == SECRETS["OPENROUTER_API_KEY"]
    assert settings.require_database_url().get_secret_value() == SECRETS["DATABASE_URL"]
    assert settings.llm_mode == "scripted"
    assert settings.models_config == Path("custom/models.yaml")


@pytest.mark.unit
def test_the_environment_wins_over_the_dotenv_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("LLM_MODE=scripted\nPERSISTENCE=postgres\n", encoding="utf-8")
    monkeypatch.setenv("LLM_MODE", "record")

    settings = _settings(env_file)

    assert settings.llm_mode == "record"
    assert settings.persistence == "postgres"


@pytest.mark.unit
def test_an_empty_value_means_the_default(tmp_path: Path) -> None:
    """`.env.example` lists every variable with no value; a copy of it must load."""
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_MODE=\nPERSISTENCE=\nOPENROUTER_API_KEY=\nSOPS_DIR=\n", "utf-8")

    settings = _settings(env_file)

    assert settings.llm_mode == "replay"
    assert settings.persistence == "memory"
    assert settings.openrouter_api_key is None
    assert settings.sops_dir == Path("sops")


@pytest.mark.unit
def test_a_copy_of_the_shipped_env_example_loads_with_the_offline_defaults() -> None:
    settings = _settings(ENV_EXAMPLE)

    assert settings.llm_mode == "replay"
    assert settings.persistence == "memory"
    assert settings.openrouter_api_key is None
    assert settings.langsmith_tracing is False


@pytest.mark.unit
def test_the_env_example_lists_exactly_the_settings() -> None:
    listed = re.findall(r"^([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text("utf-8"), re.MULTILINE)

    assert sorted(listed) == sorted(VARIABLES)


@pytest.mark.unit
def test_variables_for_other_tools_in_the_dotenv_file_are_ignored(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=compose-only\nLLM_MODE=live\n", "utf-8")

    assert _settings(env_file).llm_mode == "live"


# --- validation ----------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("LLM_MODE", "offline"),
        ("PERSISTENCE", "sqlite"),
        ("LANGSMITH_TRACING", "perhaps"),
    ],
)
def test_an_invalid_value_is_rejected_naming_the_setting(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError, match=variable.lower()):
        _settings()


@pytest.mark.unit
def test_a_validation_error_never_shows_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LLM_MODE", "offline")

    with pytest.raises(ValidationError) as raised:
        _settings()

    for secret in SECRETS.values():
        assert secret not in str(raised.value)


@pytest.mark.unit
def test_settings_cannot_change_once_loaded() -> None:
    settings = _settings()

    with pytest.raises(ValidationError):
        settings.llm_mode = "live"  # pyright: ignore[reportAttributeAccessIssue]


# --- secrets -------------------------------------------------------------------


@pytest.mark.unit
def test_secrets_are_secret_strings() -> None:
    for name in SECRETS:
        field = Settings.model_fields[name.lower()]
        assert field.annotation == SecretStr | None, name


@pytest.mark.unit
def test_no_secret_shows_in_repr_str_or_a_json_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _with_every_secret(monkeypatch)

    for shown in (repr(settings), str(settings), settings.model_dump_json()):
        for secret in SECRETS.values():
            assert secret not in shown


@pytest.mark.unit
def test_describe_shows_secrets_only_as_set_or_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRETS["OPENROUTER_API_KEY"])
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")  # blank counts as not set

    summary = _settings().describe()

    assert summary["OPENROUTER_API_KEY"] == "set"
    assert summary["TELEGRAM_BOT_TOKEN"] == "unset"
    assert summary["DATABASE_URL"] == "unset"
    assert summary["LANGSMITH_API_KEY"] == "unset"


@pytest.mark.unit
def test_describe_lists_every_setting_and_shows_the_plain_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _with_every_secret(monkeypatch)

    summary = settings.describe()

    assert tuple(summary) == VARIABLES
    assert summary["LLM_MODE"] == "replay"
    assert summary["PERSISTENCE"] == "memory"
    assert summary["LANGSMITH_TRACING"] == "false"
    assert summary["LANGSMITH_PROJECT"] == "freight-agent"
    assert summary["CASSETTES_DIR"] == str(Path("evals/cassettes"))
    shown = "\n".join(summary.values())
    for secret in SECRETS.values():
        assert secret not in shown


@pytest.mark.unit
def test_a_missing_key_fails_only_when_asked_for_and_names_the_variable() -> None:
    settings = _settings()  # loading without any secret is fine

    with pytest.raises(MissingSetting, match="OPENROUTER_API_KEY") as raised:
        settings.require_openrouter_key()

    assert raised.value.variable == "OPENROUTER_API_KEY"
    with pytest.raises(MissingSetting, match="DATABASE_URL"):
        settings.require_database_url()


@pytest.mark.unit
def test_a_blank_key_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "   ")

    with pytest.raises(MissingSetting, match="OPENROUTER_API_KEY"):
        _settings().require_openrouter_key()
