"""The app's settings: one object, read from the environment and `.env`.

Every entry point (API, worker, CLI, evals) reads the same `Settings` and hands it to
the composition root (`freight_agent.bootstrap`), which is the only code that turns it
into adapters. Nothing below the root reads the environment.

Secrets are `SecretStr`: a repr, a JSON dump or a validation error never shows them, and
`describe()` reports each one only as set or unset. They are optional, so the offline
modes (scripted, replay, in-memory storage) start with none; the code that needs one asks
for it with a `require_*` method, which names the variable when it is missing.

Each field is read from the variable of the same name in capitals (`llm_mode` from
`LLM_MODE`). The environment wins over `.env`, and an empty value counts as unset, so a
plain copy of `.env.example` loads with the defaults. Relative paths resolve against the
working directory, which is the repository root for `uv run agent` and the API.
"""

from pathlib import Path
from typing import Literal, TypeGuard

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PersistenceKind = Literal["memory", "postgres"]
LlmMode = Literal["scripted", "replay", "live", "record"]


class MissingSetting(ValueError):
    """A setting the chosen mode needs is not set. Names the variable, never a value."""

    def __init__(self, variable: str, needed_for: str) -> None:
        super().__init__(f"{variable} is not set (environment or .env); {needed_for} needs it")
        self.variable = variable


def is_set(secret: SecretStr | None) -> TypeGuard[SecretStr]:
    """A blank secret is as good as none: a provider would reject it anyway."""
    return secret is not None and bool(secret.get_secret_value().strip())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # `.env.example` lists every variable with no value
        extra="ignore",  # `.env` may hold variables for other tools (docker compose)
        frozen=True,
        hide_input_in_errors=True,  # a rejected value could be a secret pasted in the wrong line
    )

    # Secrets
    openrouter_api_key: SecretStr | None = None
    database_url: SecretStr | None = None  # pooled endpoint: API
    database_url_direct: SecretStr | None = None  # direct endpoint: worker, migrations
    langsmith_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None

    # Tracing: LangSmith when on and the key is set, otherwise no tracing
    langsmith_tracing: bool = False
    langsmith_project: str = "freight-agent"

    # Adapters
    persistence: PersistenceKind = "memory"
    llm_mode: LlmMode = "replay"  # offline and deterministic: no key, no network

    # Paths
    models_config: Path = Path("config/models.yaml")
    sops_dir: Path = Path("sops")
    evals_dir: Path = Path("evals")
    cassettes_dir: Path = Path("evals/cassettes")

    def require_openrouter_key(self) -> SecretStr:
        return _required(self.openrouter_api_key, "OPENROUTER_API_KEY", "a live model")

    def require_database_url(self) -> SecretStr:
        return _required(self.database_url, "DATABASE_URL", "Postgres persistence")

    def describe(self) -> dict[str, str]:
        """Every setting by its variable name, safe to print or log: a secret shows only
        as set or unset, never its value or its length."""
        summary: dict[str, str] = {}
        for name, value in self:
            if isinstance(value, SecretStr) or value is None:
                shown = "set" if is_set(value) else "unset"
            elif isinstance(value, bool):
                shown = str(value).lower()  # as written in `.env`
            else:
                shown = str(value)
            summary[name.upper()] = shown
        return summary


def _required(secret: SecretStr | None, variable: str, needed_for: str) -> SecretStr:
    if not is_set(secret):
        raise MissingSetting(variable, needed_for)
    return secret
