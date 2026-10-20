"""The Postgres index's entry point, which the composition root loads by name.

Building it opens no connection: the pool opens on the first call (the Postgres
behaviour itself is in `tests/contract/test_sop_index.py`).
"""

from pathlib import Path
from typing import Any

import pytest

from freight_agent import bootstrap
from freight_agent.adapters.knowledge.embeddings import OpenRouterEmbedder
from freight_agent.adapters.knowledge.fake_embedder import FakeEmbedder
from freight_agent.adapters.knowledge.postgres import PostgresSopIndex, knowledge_index
from freight_agent.settings import MissingSetting, Settings

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
UNREACHABLE = "postgresql://agent:agent@127.0.0.1:1/agent"


def settings(**values: Any) -> Settings:
    base: dict[str, Any] = {
        "persistence": "postgres",
        "llm_mode": "replay",
        "database_url": UNREACHABLE,
        "openrouter_api_key": None,
        "models_config": ROOT / "config" / "models.yaml",
        "sops_dir": ROOT / "sops",
        **values,
    }
    return Settings(_env_file=None, **base)  # pyright: ignore[reportCallIssue]


def test_the_index_is_built_without_connecting() -> None:
    index = knowledge_index(settings())

    assert isinstance(index, PostgresSopIndex)
    assert isinstance(index._embedder, FakeEmbedder)  # pyright: ignore[reportPrivateUsage]
    assert index._embedder.dim == 1536  # pyright: ignore[reportPrivateUsage]


def test_live_mode_embeds_queries_on_openrouter() -> None:
    index = knowledge_index(settings(llm_mode="live", openrouter_api_key="sk-or-x"))

    assert isinstance(index._embedder, OpenRouterEmbedder)  # pyright: ignore[reportPrivateUsage]


def test_the_composition_root_may_pass_its_own_embedder() -> None:
    embedder = FakeEmbedder(dim=1536, model_id="root/embedder")

    index = knowledge_index(settings(llm_mode="live"), embedder=embedder)

    assert index._embedder is embedder  # pyright: ignore[reportPrivateUsage]


def test_the_index_needs_the_database_url() -> None:
    with pytest.raises(MissingSetting, match="DATABASE_URL"):
        knowledge_index(settings(database_url=None))


def test_the_composition_root_finds_it_by_name() -> None:
    assert isinstance(bootstrap.build_knowledge(settings()), PostgresSopIndex)


async def test_closing_an_index_that_never_opened_does_nothing() -> None:
    await knowledge_index(settings()).close()


def test_an_invalid_schema_fails_before_any_connection() -> None:
    with pytest.raises(ValueError, match="schema"):
        PostgresSopIndex(UNREACHABLE, embedder=FakeEmbedder(), schema="Bad-Name")
