"""The OpenRouter embedder, on an httpx `MockTransport` (no network, no key spent)."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from freight_agent.adapters.knowledge.embeddings import (
    DEFAULT_DIM,
    OpenRouterEmbedder,
    embedder_for,
)
from freight_agent.adapters.knowledge.fake_embedder import FakeEmbedder
from freight_agent.ports.knowledge import EmbeddingError
from freight_agent.settings import Settings

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
KEY = SecretStr("sk-or-test-key")
MODEL = "openai/text-embedding-3-small"


def vector(seed: int, dim: int = 3) -> list[float]:
    return [seed + i / 10 for i in range(dim)]


def answering(
    seen: list[httpx.Request], *, dim: int = 3, reverse: bool = False
) -> httpx.MockTransport:
    """Answers like OpenRouter: one embedding per input, with its index."""

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        texts = json.loads(request.content)["input"]
        data = [
            {"object": "embedding", "index": i, "embedding": vector(i, dim)}
            for i in range(len(texts))
        ]
        body = {"object": "list", "model": MODEL, "data": data[::-1] if reverse else data}
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handle)


def embedder(transport: httpx.MockTransport, **kwargs: Any) -> OpenRouterEmbedder:
    return OpenRouterEmbedder(
        KEY, model=MODEL, dim=kwargs.pop("dim", 3), transport=transport, **kwargs
    )


async def test_texts_go_to_the_embeddings_endpoint_with_the_model_and_the_key() -> None:
    seen: list[httpx.Request] = []

    vectors = await embedder(answering(seen)).embed(["trailer temperature", "seal number"])

    assert vectors == [vector(0), vector(1)]
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "https://openrouter.ai/api/v1/embeddings"
    assert request.headers["Authorization"] == "Bearer sk-or-test-key"
    assert json.loads(request.content) == {
        "model": MODEL,
        "input": ["trailer temperature", "seal number"],
    }


async def test_the_answer_is_put_back_in_the_order_of_the_texts() -> None:
    vectors = await embedder(answering([], reverse=True)).embed(["a", "b", "c"])

    assert vectors == [vector(0), vector(1), vector(2)]


async def test_texts_go_in_batches() -> None:
    seen: list[httpx.Request] = []

    vectors = await embedder(answering(seen), batch_size=2).embed(["a", "b", "c", "d", "e"])

    assert [len(json.loads(r.content)["input"]) for r in seen] == [2, 2, 1]
    assert vectors == [vector(0), vector(1), vector(0), vector(1), vector(0)]


async def test_no_text_makes_no_request() -> None:
    seen: list[httpx.Request] = []

    assert await embedder(answering(seen)).embed([]) == []
    assert seen == []


@pytest.mark.parametrize(
    ("response", "problem"),
    [
        (httpx.Response(401, text="No auth credentials found"), "HTTP 401: No auth credentials"),
        (httpx.Response(200, text="not json"), "unexpected answer"),
        (httpx.Response(200, json={"error": "rate limited"}), "unexpected answer"),
        (httpx.Response(200, json={"data": []}), "0 embeddings for 1 texts"),
        (
            httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]}),
            "does not have 3 dimensions",
        ),
    ],
)
async def test_a_bad_answer_is_an_embedding_error_naming_the_model(
    response: httpx.Response, problem: str
) -> None:
    transport = httpx.MockTransport(lambda request: response)

    with pytest.raises(EmbeddingError, match=problem) as raised:
        await embedder(transport).embed(["text"])

    assert MODEL in str(raised.value)


async def test_a_network_failure_is_an_embedding_error_without_the_key() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(EmbeddingError, match="ConnectError") as raised:
        await embedder(httpx.MockTransport(fail)).embed(["text"])

    assert "sk-or-test-key" not in str(raised.value)
    assert "sk-or-test-key" not in repr(embedder(httpx.MockTransport(fail)))


def test_a_batch_holds_at_least_one_text() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        OpenRouterEmbedder(KEY, batch_size=0)


def settings(**values: Any) -> Settings:
    base: dict[str, Any] = {
        "openrouter_api_key": None,
        "models_config": ROOT / "config" / "models.yaml",
        **values,
    }
    return Settings(_env_file=None, **base)  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize("mode", ["scripted", "replay"])
def test_offline_modes_embed_with_the_fake_at_the_configured_width(mode: str) -> None:
    found = embedder_for(settings(llm_mode=mode))

    assert isinstance(found, FakeEmbedder)
    assert found.dim == DEFAULT_DIM == 1536


@pytest.mark.parametrize("mode", ["live", "record"])
def test_live_modes_embed_on_openrouter_with_the_configured_model(mode: str) -> None:
    found = embedder_for(settings(llm_mode=mode, openrouter_api_key="sk-or-x"))

    assert isinstance(found, OpenRouterEmbedder)
    assert (found.model_id, found.dim) == (MODEL, 1536)


def test_without_a_models_file_the_defaults_apply(tmp_path: Path) -> None:
    found = embedder_for(settings(llm_mode="scripted", models_config=tmp_path / "none.yaml"))

    assert isinstance(found, FakeEmbedder) and found.dim == 1536
