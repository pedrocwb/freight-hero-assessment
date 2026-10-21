from pathlib import Path
from typing import Any

import pytest
import yaml

from freight_agent.adapters.llm.models_config import (
    ModelPricing,
    ModelsConfig,
    ProviderRouting,
    RoleConfig,
    load_models_config,
)
from freight_agent.ports.llm import ModelRole

ROOT = Path(__file__).resolve().parents[3]
SHIPPED = ROOT / "config" / "models.yaml"


def _config(**overrides: Any) -> dict[str, Any]:
    role = {"model": "acme/small", "fallbacks": ["acme/tiny"], "temperature": 0, "max_tokens": 256}
    raw: dict[str, Any] = {
        "roles": {r.value: dict(role) for r in ModelRole},
        "pricing": {
            "acme/small": {"input": 0.1, "output": 0.4, "cached_input": 0.01},
            "acme/tiny": {"input": 0.05, "output": 0.2},
            "acme/mid": {"input": 0.2, "output": 0.8},
        },
    }
    raw.update(overrides)
    return raw


def _write(tmp_path: Path, raw: dict[str, Any]) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


@pytest.mark.unit
def test_the_shipped_config_has_every_role_priced() -> None:
    config = load_models_config(SHIPPED)

    assert set(config.roles) == set(ModelRole)
    for role in ModelRole:
        for entry in config.role(role).chain():
            assert config.pricing_for(entry.model).output > 0


@pytest.mark.unit
def test_the_gate_and_decide_stay_on_cheap_models() -> None:
    """The user's rule: cheap, light models first. Under US$ 1 per 1M output tokens."""
    config = load_models_config(SHIPPED)

    for role in (ModelRole.GATE, ModelRole.AGENT):
        assert config.pricing_for(config.role(role).model).output < 1.0


@pytest.mark.unit
def test_loads_a_valid_file(tmp_path: Path) -> None:
    config = load_models_config(_write(tmp_path, _config()))

    gate = config.role(ModelRole.GATE)
    assert gate.model == "acme/small"
    assert gate.fallbacks == ("acme/tiny",)
    assert gate.temperature == 0
    assert gate.max_tokens == 256


@pytest.mark.unit
def test_a_missing_role_is_an_error(tmp_path: Path) -> None:
    raw = _config()
    del raw["roles"]["judge"]

    with pytest.raises(ValueError, match="judge"):
        load_models_config(_write(tmp_path, raw))


@pytest.mark.unit
def test_a_model_without_pricing_is_an_error(tmp_path: Path) -> None:
    raw = _config()
    raw["roles"]["agent"]["fallbacks"] = ["acme/unpriced"]

    with pytest.raises(ValueError, match="acme/unpriced"):
        load_models_config(_write(tmp_path, raw))


@pytest.mark.unit
def test_a_typo_in_a_setting_is_an_error(tmp_path: Path) -> None:
    raw = _config()
    raw["roles"]["gate"]["temprature"] = 0.2

    with pytest.raises(ValueError, match="temprature"):
        load_models_config(_write(tmp_path, raw))


@pytest.mark.unit
def test_a_fallback_that_repeats_the_model_is_an_error(tmp_path: Path) -> None:
    raw = _config()
    raw["roles"]["gate"]["fallbacks"] = ["acme/small"]

    with pytest.raises(ValueError, match="acme/small"):
        load_models_config(_write(tmp_path, raw))


@pytest.mark.unit
def test_a_file_that_is_not_a_mapping_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"models\.yaml"):
        load_models_config(path)


@pytest.mark.unit
def test_cost_prices_cached_input_apart_from_the_rest_of_the_input() -> None:
    pricing = ModelPricing(input=0.10, output=0.40, cached_input=0.01)

    cost = pricing.cost_usd(input_tokens=1_000, output_tokens=100, cached_tokens=400)

    # 600 uncached input, 400 cached input, 100 output, per 1M tokens
    assert cost == pytest.approx((600 * 0.10 + 400 * 0.01 + 100 * 0.40) / 1_000_000)


@pytest.mark.unit
def test_cached_input_costs_the_input_price_when_no_cache_price_is_listed() -> None:
    pricing = ModelPricing(input=0.20, output=0.80)

    assert pricing.cost_usd(input_tokens=1_000, output_tokens=0, cached_tokens=400) == (
        pytest.approx(1_000 * 0.20 / 1_000_000)
    )


@pytest.mark.unit
def test_the_chain_pins_the_provider_for_the_primary_model_only() -> None:
    pinned = ProviderRouting(order=("google-ai-studio",), allow_fallbacks=False)
    role = RoleConfig(
        model="acme/small",
        fallbacks=("acme/tiny", "acme/mid"),
        temperature=0,
        max_tokens=300,
        provider=pinned,
    )

    chain = role.chain()

    assert [c.model for c in chain] == ["acme/small", "acme/tiny", "acme/mid"]
    assert chain[0].provider == pinned
    assert all(c.provider == ProviderRouting() for c in chain[1:])
    assert all(c.fallbacks == () for c in chain)
    assert all((c.temperature, c.max_tokens) == (0, 300) for c in chain)


@pytest.mark.unit
def test_provider_routing_is_sent_without_unset_preferences() -> None:
    routing = ProviderRouting(order=("openai",), allow_fallbacks=False)

    assert routing.as_openrouter() == {
        "order": ["openai"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }


@pytest.mark.unit
def test_pricing_lookup_names_the_missing_model() -> None:
    config = ModelsConfig.model_validate(_config())

    with pytest.raises(KeyError, match="acme/none"):
        config.pricing_for("acme/none")


@pytest.mark.unit
def test_the_shipped_gate_can_run_on_a_pinned_decision_model() -> None:
    config = load_models_config(SHIPPED)

    gate = config.decisions[ModelRole.GATE]
    assert gate.model == "typesafe/jev-1.14"  # a pinned version, not the moving alias
    assert (gate.max_attempts, gate.attempt_timeout_s, gate.backoff_s) == (3, 10, 0.5)
    assert config.pricing_for(gate.model).input == 0.042


@pytest.mark.unit
def test_decisions_are_optional_and_their_model_must_be_priced(tmp_path: Path) -> None:
    assert load_models_config(_write(tmp_path, _config())).decisions == {}

    raw = _config(decisions={"gate": {"model": "acme/decider"}})
    with pytest.raises(ValueError, match="decision models with no pricing entry: acme/decider"):
        load_models_config(_write(tmp_path, raw))
