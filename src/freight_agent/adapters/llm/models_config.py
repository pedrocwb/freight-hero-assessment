"""Which model serves each role, what it costs, and the OpenRouter key.

`config/models.yaml` is the only place a model is chosen: switching a model is a change
to that file. Each role names a primary model and an ordered fallback list. This adapter
serves one model per instance; walking the fallback list is the resilient wrapper's job
(`adapters/llm/resilient.py`), so `RoleConfig.chain()` hands it one config per model.

Prices are USD per 1M tokens, copied from the public OpenRouter catalog. They are
estimates for budgets and reports: the billed price depends on the endpoint OpenRouter
routes to, which is why the evals can pin a provider.
"""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from freight_agent.ports.llm import ModelRole

_PER_MILLION = 1_000_000

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high"]


class _Config(BaseModel):
    """Immutable, and a typo in the yaml is an error rather than a silently ignored key."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ProviderRouting(_Config):
    """OpenRouter provider preferences (its `provider` request field).

    `require_parameters` stays on: OpenRouter then routes only to endpoints that honour
    every parameter we send, so a forced tool choice is never silently dropped. The
    flip side is that a parameter a model does not take (some reasoning models take no
    `temperature`) leaves no endpoint, so such a role sets `temperature: null`.
    """

    order: tuple[str, ...] | None = None  # provider slugs, tried in this order
    allow_fallbacks: bool | None = None  # false: only the providers in `order`
    require_parameters: bool = True
    quantizations: tuple[str, ...] | None = None  # for open-weight models, e.g. [bf16]
    data_collection: Literal["allow", "deny"] | None = None

    def as_openrouter(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class RoleConfig(_Config):
    model: str = Field(min_length=1)  # OpenRouter model id, e.g. google/gemini-2.5-flash-lite
    fallbacks: tuple[str, ...] = ()
    temperature: float | None = Field(default=None, ge=0, le=2)  # None: not sent
    max_tokens: int = Field(ge=1)
    reasoning_effort: ReasoningEffort | None = None  # None: the model's default
    provider: ProviderRouting = ProviderRouting()

    @model_validator(mode="after")
    def _distinct_models(self) -> "RoleConfig":
        chain = (self.model, *self.fallbacks)
        repeated = sorted({m for m in chain if chain.count(m) > 1})
        if repeated:
            raise ValueError(f"a model appears twice in the chain: {', '.join(repeated)}")
        return self

    def chain(self) -> tuple["RoleConfig", ...]:
        """The primary, then each fallback, as single-model configs in order.

        Provider pinning applies to the primary only. It exists for reproducible evals,
        which run on the primary; a fallback is the availability path, so it keeps
        OpenRouter's default routing (a pinned provider rarely serves another model).
        """
        fallbacks = tuple(
            self.model_copy(update={"model": m, "fallbacks": (), "provider": ProviderRouting()})
            for m in self.fallbacks
        )
        return (self.model_copy(update={"fallbacks": ()}), *fallbacks)


class ModelPricing(_Config):
    """USD per 1M tokens, as listed in the OpenRouter catalog."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cached_input: float | None = Field(default=None, ge=0)  # None: billed as input
    context_length: int | None = Field(default=None, ge=1)  # informational

    def cost_usd(self, *, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
        """`input_tokens` includes the cached ones, as OpenRouter reports them."""
        cached = min(max(cached_tokens, 0), input_tokens)
        cached_price = self.input if self.cached_input is None else self.cached_input
        return (
            (input_tokens - cached) * self.input
            + cached * cached_price
            + output_tokens * self.output
        ) / _PER_MILLION


class ModelsConfig(_Config):
    roles: dict[ModelRole, RoleConfig]
    pricing: dict[str, ModelPricing]
    bakeoff_candidates: tuple[str, ...] = Field(min_length=3, max_length=5)

    @model_validator(mode="after")
    def _complete(self) -> "ModelsConfig":
        missing = [r.value for r in ModelRole if r not in self.roles]
        if missing:
            raise ValueError(f"roles missing from the config: {', '.join(missing)}")
        used = [m for role in self.roles.values() for m in (role.model, *role.fallbacks)]
        unpriced = sorted({m for m in (*used, *self.bakeoff_candidates) if m not in self.pricing})
        if unpriced:
            raise ValueError(f"models with no pricing entry: {', '.join(unpriced)}")
        return self

    def role(self, role: ModelRole) -> RoleConfig:
        return self.roles[role]

    def pricing_for(self, model_id: str) -> ModelPricing:
        try:
            return self.pricing[model_id]
        except KeyError:
            raise KeyError(f"no pricing entry for {model_id}") from None


def load_models_config(path: Path | str) -> ModelsConfig:
    """Read and check `config/models.yaml`. Errors name the file."""
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping with roles, pricing, bakeoff_candidates")
    try:
        return ModelsConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"{path}: {exc}") from exc


class Settings(BaseSettings):
    """Secrets for the live model, from the environment or `.env`.

    The key is a `SecretStr`: it never shows in a repr, a log line or a dump. It is
    optional here so scripted and replay runs load settings without one; a live model
    asks for it with `require_openrouter_key`.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openrouter_api_key: SecretStr | None = None

    def require_openrouter_key(self) -> SecretStr:
        key = self.openrouter_api_key
        if key is None or not key.get_secret_value().strip():
            raise ValueError("OPENROUTER_API_KEY is not set (environment or .env)")
        return key
