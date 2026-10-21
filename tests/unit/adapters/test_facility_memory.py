"""The facility memory on an `InMemoryStore` (what the contract suite cannot see: the jobs
are not embedded), and the embedders. The behaviour through the interfaces, on both
stores, is `tests/contract/test_facility_memory.py`."""

import json
from datetime import datetime

import httpx
import pytest
from langchain_core.embeddings import Embeddings
from langgraph.store.memory import InMemoryStore
from pydantic import SecretStr

from freight_agent.adapters.llm.models_config import EmbeddingConfig, ModelPricing
from freight_agent.adapters.memory.embeddings import (
    EmbeddingsError,
    HashEmbeddings,
    OpenRouterEmbeddings,
    embeddings_dims,
)
from freight_agent.adapters.memory.store import StoreFacilityMemory
from freight_agent.domain.memory import ExtractionJob
from tests.conftest import LA

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 2, 9, 0, tzinfo=LA)


async def test_jobs_are_not_embedded() -> None:
    class Counting(HashEmbeddings):
        def __init__(self) -> None:
            super().__init__()
            self.texts: list[str] = []

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            self.texts.extend(texts)
            return super().embed_documents(texts)

    embeddings = Counting()
    store = InMemoryStore(index={"embed": embeddings, "dims": 256, "fields": ["text"]})
    memory = StoreFacilityMemory(store)
    await memory.enqueue(ExtractionJob(load_id="1", client_id="northline", queued_at=T0))

    assert embeddings.texts == []


# --- embedders ------------------------------------------------------------------------------


def test_the_hash_embeddings_are_deterministic_and_normalized() -> None:
    embeddings = HashEmbeddings(dims=32)
    one, two = embeddings.embed_documents(["Check in at gate 4", "check in at GATE 4"])

    assert one == two == embeddings.embed_query("check in at gate 4")
    assert sum(x * x for x in one) == pytest.approx(1.0)
    assert embeddings.embed_query("") == [0.0] * 32
    assert embeddings_dims(embeddings) == 32


def test_dims_are_required_of_an_embedder() -> None:
    class Anonymous(Embeddings):
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] for _ in texts]

        def embed_query(self, text: str) -> list[float]:
            return [1.0]

    with pytest.raises(TypeError, match="does not say its dimensions"):
        embeddings_dims(Anonymous())


CONFIG = EmbeddingConfig(model="openai/text-embedding-3-small", dims=3, timeout_s=5)
PRICING = ModelPricing(input=0.02, output=0)


def _openrouter(handler: httpx.MockTransport) -> OpenRouterEmbeddings:
    return OpenRouterEmbeddings(CONFIG, PRICING, SecretStr("k"), transport=handler)


async def test_openrouter_embeddings_send_the_texts_and_count_the_cost() -> None:
    sent: list[dict[str, object]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer k"
        body = {
            "data": [
                {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                {"index": 0, "embedding": [1.0, 0.0, 0.0]},
            ],
            "usage": {"prompt_tokens": 9, "cost": 1.8e-07},
        }
        return httpx.Response(200, json=body)

    embeddings = _openrouter(httpx.MockTransport(answer))
    vectors = await embeddings.aembed_documents(["a", "b"])

    assert vectors == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert sent == [{"model": "openai/text-embedding-3-small", "input": ["a", "b"]}]
    assert embeddings.spent_usd == pytest.approx(1.8e-07)
    assert embeddings.calls == 1
    assert await embeddings.aembed_documents([]) == []


async def test_without_a_reported_cost_the_catalog_price_counts() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        body = {
            "data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}],
            "usage": {"prompt_tokens": 50},
        }
        return httpx.Response(200, json=body)

    embeddings = _openrouter(httpx.MockTransport(answer))
    assert await embeddings.aembed_query("a") == [1.0, 0.0, 0.0]
    assert embeddings.spent_usd == pytest.approx(50 * 0.02 / 1_000_000)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, text="rate limited"),
        httpx.Response(200, json={"nothing": []}),
        httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]}),
    ],
)
async def test_a_bad_answer_is_an_embeddings_error(response: httpx.Response) -> None:
    embeddings = _openrouter(httpx.MockTransport(lambda request: response))
    with pytest.raises(EmbeddingsError):
        await embeddings.aembed_documents(["a"])


async def test_a_network_failure_is_an_embeddings_error() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(EmbeddingsError, match="ConnectError"):
        await _openrouter(httpx.MockTransport(fail)).aembed_documents(["a"])


def test_the_sync_path_runs_the_async_one() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        body = {"data": [{"index": 0, "embedding": [0.0, 0.0, 1.0]}], "usage": {}}
        return httpx.Response(200, json=body)

    embeddings = _openrouter(httpx.MockTransport(answer))
    assert embeddings.embed_query("a") == [0.0, 0.0, 1.0]
    assert embeddings.dims == 3
