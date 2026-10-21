"""The facility memory on a LangGraph store: one namespace per client and facility, semantic
search on an embeddings index, the extraction queue, and the embedders."""

import json
from datetime import datetime, timedelta

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
    memory_store,
)
from freight_agent.adapters.memory.store import (
    StoreFacilityMemory,
    facts_namespace,
    jobs_namespace,
)
from freight_agent.domain.memory import (
    ExtractionJob,
    ExtractionStatus,
    FacilityFact,
    FactDelete,
    FactKind,
)
from tests.conftest import LA

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 2, 9, 0, tzinfo=LA)
PICKUP = "riverbend-plant~stockton-ca"
DELIVERY = "valley-dc~reno-nv"


def fact(
    fact_id: str,
    text: str,
    *,
    client: str = "northline",
    key: str = PICKUP,
    minutes: int = 0,
    kind: FactKind = FactKind.OTHER,
) -> FacilityFact:
    at = T0 + timedelta(minutes=minutes)
    return FacilityFact(
        fact_id=fact_id,
        client_id=client,
        facility_key=key,
        facility_name="Riverbend plant",
        text=text,
        kind=kind,
        source_load_id="470001",
        source_event_id="m1",
        source_at=at,
        created_at=at,
        updated_at=at,
    )


@pytest.fixture
def memory() -> StoreFacilityMemory:
    return StoreFacilityMemory(memory_store(HashEmbeddings(), 256))


async def test_a_client_never_reads_another_clients_facts(memory: StoreFacilityMemory) -> None:
    await memory.apply("northline", upserts=[fact("f1", "Check in at gate 4")])
    await memory.apply("acme", upserts=[fact("f2", "Check in at gate 9", client="acme")])

    assert [f.fact_id for f in await memory.recall("northline", PICKUP)] == ["f1"]
    assert [f.fact_id for f in await memory.recall("acme", PICKUP)] == ["f2"]
    assert [f.fact_id for f in await memory.search("acme", "check in gate")] == ["f2"]
    assert [f.fact_id for f in await memory.facts("northline")] == ["f1"]
    assert await memory.recall("keystone", PICKUP) == []


async def test_a_write_naming_another_client_writes_nothing(memory: StoreFacilityMemory) -> None:
    with pytest.raises(ValueError, match="names another client"):
        await memory.apply(
            "northline",
            upserts=[fact("f1", "gate 4"), fact("f2", "gate 9", client="acme")],
        )
    with pytest.raises(ValueError, match="names another client"):
        await memory.apply(
            "northline", deletes=[FactDelete(client_id="acme", facility_key=PICKUP, fact_id="x")]
        )

    assert await memory.facts("northline") == []


async def test_a_value_of_another_client_is_dropped_on_read() -> None:
    """Two client ids with the same namespace label still never see each other."""
    store = memory_store(HashEmbeddings(), 256)
    memory = StoreFacilityMemory(store)
    await memory.apply("acme.freight", upserts=[fact("f1", "gate 4", client="acme.freight")])

    assert await memory.recall("acme-freight", PICKUP) == []
    assert facts_namespace("acme.freight", PICKUP) == ("facility_memory", "acme-freight", PICKUP)


async def test_recall_is_the_most_recent_first_and_delete_removes(
    memory: StoreFacilityMemory,
) -> None:
    await memory.apply(
        "northline",
        upserts=[
            fact("f1", "Check in at gate 4"),
            fact("f2", "Dock closes at 15:00", minutes=10),
            fact("f3", "PO required at the gate", minutes=5),
            fact("f4", "Unload at door 12", key=DELIVERY),
        ],
    )

    assert [f.fact_id for f in await memory.recall("northline", PICKUP)] == ["f2", "f3", "f1"]
    assert [f.fact_id for f in await memory.recall("northline", PICKUP, limit=1)] == ["f2"]
    assert [f.fact_id for f in await memory.facts("northline")] == ["f2", "f3", "f1", "f4"]

    await memory.apply(
        "northline", deletes=[FactDelete(client_id="northline", facility_key=PICKUP, fact_id="f2")]
    )

    assert [f.fact_id for f in await memory.recall("northline", PICKUP)] == ["f3", "f1"]


async def test_search_ranks_by_meaning_within_the_client_and_the_facility(
    memory: StoreFacilityMemory,
) -> None:
    await memory.apply(
        "northline",
        upserts=[
            fact("f1", "Dock closes at 15:00"),
            fact("f2", "Check in at gate 4"),
            fact("f3", "Check in at the guard shack", key=DELIVERY),
        ],
    )

    best = await memory.search("northline", "where do I check in", limit=3)
    assert best[0].fact_id in ("f2", "f3")
    only_pickup = await memory.search("northline", "check in", facility_key=PICKUP, limit=3)
    assert {f.fact_id for f in only_pickup} == {"f1", "f2"}
    assert only_pickup[0].fact_id == "f2"


async def test_the_queue_takes_a_load_once_and_lists_the_pending_oldest_first(
    memory: StoreFacilityMemory,
) -> None:
    late = ExtractionJob(load_id="2", client_id="acme", queued_at=T0 + timedelta(hours=1))
    early = ExtractionJob(load_id="1", client_id="northline", queued_at=T0)

    assert await memory.enqueue(late)
    assert await memory.enqueue(early)
    assert not await memory.enqueue(early)
    assert [j.load_id for j in await memory.pending()] == ["1", "2"]

    await memory.save(early.model_copy(update={"status": ExtractionStatus.DONE}))

    assert [j.load_id for j in await memory.pending()] == ["2"]
    found = await memory.get("northline", "1")
    assert found is not None and found.status is ExtractionStatus.DONE
    assert await memory.get("acme", "1") is None
    assert jobs_namespace("acme") == ("facility_memory_jobs", "acme")


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
