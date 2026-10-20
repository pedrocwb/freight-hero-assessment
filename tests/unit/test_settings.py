"""The app's settings: the environment, `.env`, the defaults, and the secrets kept out of
every repr, dump and error."""

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from freight_agent.settings import MissingSetting, Settings

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.example"
VARIABLES = tuple(name.upper() for name in Settings.model_fields)
SECRETS = {
    "OPENROUTER_API_KEY": "sk-or-v1-settings-test-secret",
    "LANGSMITH_API_KEY": "lsv2_pt_settings-test-secret",
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


def test_the_defaults_need_no_key_and_trace_nothing() -> None:
    settings = _settings()

    assert settings.models_config == Path("config/models.yaml")
    assert settings.sops_dir == Path("sops")
    assert settings.evals_dir == Path("evals")
    assert settings.web_dist == Path("web/dist")
    assert settings.blobs_dir == Path("blobs")
    assert settings.openrouter_api_key is None
    assert settings.langsmith_api_key is None
    assert settings.model_ready is False
    assert settings.langsmith_tracing is False
    assert settings.langsmith_project == "freight-agent"


def test_the_model_mode_and_the_cassettes_are_gone() -> None:
    """The model is always the live one: no mode to choose, no cassettes to replay."""
    assert "llm_mode" not in Settings.model_fields
    assert "cassettes_dir" not in Settings.model_fields


def test_settings_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRETS["OPENROUTER_API_KEY"])
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_PROJECT", "freight-agent-evals")
    monkeypatch.setenv("SOPS_DIR", "/srv/sops")
    monkeypatch.setenv("EVALS_DIR", "/srv/evals")
    monkeypatch.setenv("BLOBS_DIR", "/srv/blobs")

    settings = _settings()

    assert settings.require_openrouter_key().get_secret_value() == SECRETS["OPENROUTER_API_KEY"]
    assert settings.model_ready is True
    assert settings.langsmith_tracing is True
    assert settings.langsmith_project == "freight-agent-evals"
    assert settings.sops_dir == Path("/srv/sops")
    assert settings.evals_dir == Path("/srv/evals")
    assert settings.blobs_dir == Path("/srv/blobs")


def test_settings_read_the_dotenv_file_of_the_working_directory(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        f"OPENROUTER_API_KEY={SECRETS['OPENROUTER_API_KEY']}\n"
        f"LANGSMITH_API_KEY={SECRETS['LANGSMITH_API_KEY']}\n"
        "LANGSMITH_TRACING=true\n"
        "MODELS_CONFIG=custom/models.yaml\n",
        encoding="utf-8",
    )

    settings = Settings()  # the default env_file, `.env` in the working directory (tmp_path)

    assert settings.require_openrouter_key().get_secret_value() == SECRETS["OPENROUTER_API_KEY"]
    assert settings.langsmith_api_key is not None
    assert settings.require_langsmith_key().get_secret_value() == SECRETS["LANGSMITH_API_KEY"]
    assert settings.langsmith_tracing is True
    assert settings.models_config == Path("custom/models.yaml")


def test_the_environment_wins_over_the_dotenv_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("SOPS_DIR=from-file\nLANGSMITH_PROJECT=from-file\n", encoding="utf-8")
    monkeypatch.setenv("SOPS_DIR", "/from/the/environment")

    settings = _settings(env_file)

    assert settings.sops_dir == Path("/from/the/environment")
    assert settings.langsmith_project == "from-file"


def test_an_empty_value_means_the_default(tmp_path: Path) -> None:
    """`.env.example` lists every variable with no value; a copy of it must load."""
    env_file = tmp_path / ".env"
    env_file.write_text("LANGSMITH_PROJECT=\nOPENROUTER_API_KEY=\nSOPS_DIR=\n", "utf-8")

    settings = _settings(env_file)

    assert settings.langsmith_project == "freight-agent"
    assert settings.openrouter_api_key is None
    assert settings.sops_dir == Path("sops")


def test_a_copy_of_the_shipped_env_example_loads_with_the_defaults() -> None:
    settings = _settings(ENV_EXAMPLE)

    assert settings == _settings()
    assert settings.openrouter_api_key is None
    assert settings.model_ready is False
    assert settings.langsmith_tracing is False
    assert settings.models_config == Path("config/models.yaml")


def test_the_env_example_lists_exactly_the_settings() -> None:
    listed = re.findall(r"^([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text("utf-8"), re.MULTILINE)

    assert sorted(listed) == sorted(VARIABLES)


def test_variables_for_other_tools_in_the_dotenv_file_are_ignored(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("COMPOSE_PROJECT_NAME=compose-only\nLANGSMITH_PROJECT=mine\n", "utf-8")

    assert _settings(env_file).langsmith_project == "mine"


def test_an_old_dotenv_with_the_model_mode_and_cassettes_still_loads(tmp_path: Path) -> None:
    """A `.env` copied before the modes went keeps working: the old lines are unknown
    variables now, and ignored."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_MODE=replay\nCASSETTES_DIR=evals/cassettes\nLANGSMITH_PROJECT=mine\n", "utf-8"
    )

    settings = _settings(env_file)

    assert settings.langsmith_project == "mine"
    assert "LLM_MODE" not in settings.describe()
    assert "CASSETTES_DIR" not in settings.describe()


# --- validation ----------------------------------------------------------------


def test_an_invalid_value_is_rejected_naming_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "perhaps")

    with pytest.raises(ValidationError, match="langsmith_tracing"):
        _settings()


def test_a_validation_error_never_shows_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LANGSMITH_TRACING", "perhaps")

    with pytest.raises(ValidationError) as raised:
        _settings()

    for secret in SECRETS.values():
        assert secret not in str(raised.value)


def test_settings_cannot_change_once_loaded() -> None:
    settings = _settings()

    with pytest.raises(ValidationError):
        settings.langsmith_project = "elsewhere"  # pyright: ignore[reportAttributeAccessIssue]


# --- secrets -------------------------------------------------------------------


def test_secrets_are_secret_strings() -> None:
    for name in SECRETS:
        field = Settings.model_fields[name.lower()]
        assert field.annotation == SecretStr | None, name


def test_no_secret_shows_in_repr_str_or_a_json_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _with_every_secret(monkeypatch)

    for shown in (repr(settings), str(settings), settings.model_dump_json()):
        for secret in SECRETS.values():
            assert secret not in shown


def test_describe_shows_secrets_only_as_set_or_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRETS["OPENROUTER_API_KEY"])
    monkeypatch.setenv("LANGSMITH_API_KEY", "   ")  # blank counts as not set

    summary = _settings().describe()

    assert summary["OPENROUTER_API_KEY"] == "set"
    assert summary["LANGSMITH_API_KEY"] == "unset"


def test_describe_lists_every_setting_and_shows_the_plain_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _with_every_secret(monkeypatch)

    summary = settings.describe()

    assert tuple(summary) == VARIABLES
    assert summary["LANGSMITH_TRACING"] == "false"
    assert summary["LANGSMITH_PROJECT"] == "freight-agent"
    assert summary["MODELS_CONFIG"] == str(Path("config/models.yaml"))
    shown = "\n".join(summary.values())
    for secret in SECRETS.values():
        assert secret not in shown


def test_a_missing_key_fails_only_when_asked_for_and_names_the_variable() -> None:
    settings = _settings()  # loading without any secret is fine

    with pytest.raises(MissingSetting) as raised:
        settings.require_openrouter_key()

    assert raised.value.variable == "OPENROUTER_API_KEY"
    assert str(raised.value) == (
        "OPENROUTER_API_KEY is not set (environment or .env); the agent's model needs it"
    )


def test_the_langsmith_key_is_asked_for_the_same_way(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(MissingSetting, match="LANGSMITH_API_KEY") as raised:
        _settings().require_langsmith_key()
    assert raised.value.variable == "LANGSMITH_API_KEY"
    assert "a LangSmith experiment" in str(raised.value)

    monkeypatch.setenv("LANGSMITH_API_KEY", SECRETS["LANGSMITH_API_KEY"])
    key = _settings().require_langsmith_key()

    assert key.get_secret_value() == SECRETS["LANGSMITH_API_KEY"]


def test_a_blank_key_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "   ")
    monkeypatch.setenv("LANGSMITH_API_KEY", "   ")
    settings = _settings()

    with pytest.raises(MissingSetting, match="OPENROUTER_API_KEY"):
        settings.require_openrouter_key()
    with pytest.raises(MissingSetting, match="LANGSMITH_API_KEY"):
        settings.require_langsmith_key()


@pytest.mark.parametrize(("key", "ready"), [(None, False), ("  ", False), ("sk-or-x", True)])
def test_the_model_is_ready_only_with_an_openrouter_key(key: str | None, ready: bool) -> None:
    """What the platform shows (`SimState.model_ready`): without it, runs escalate."""
    assert _settings(openrouter_api_key=key).model_ready is ready
