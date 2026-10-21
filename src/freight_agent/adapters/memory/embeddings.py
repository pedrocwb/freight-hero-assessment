"""Embeddings for the facility memory's semantic search, as LangChain `Embeddings`, the
shape a LangGraph store's index takes (`IndexConfig.embed`).

- `OpenRouterEmbeddings`: the model of the `embeddings.memory` entry of
  `config/models.yaml` (`openai/text-embedding-3-small`), on OpenRouter's
  OpenAI-compatible `POST /api/v1/embeddings`. Each call's cost (OpenRouter reports it
  in `usage.cost`; else the catalog price of the tokens) is added to `spent_usd`.
- `HashEmbeddings`: deterministic and offline, for the tests and a process without the
  OpenRouter key. Each word is hashed into one of `dims` buckets, so texts that share
  words are close: lexical, not semantic, and good enough to rank a handful of facts.
"""

import asyncio
import hashlib
import math
import re
from collections.abc import Sequence
from typing import Any, cast

import httpx
from langchain_core.embeddings import Embeddings
from langgraph.store.base import IndexConfig
from langgraph.store.memory import InMemoryStore
from pydantic import SecretStr

from freight_agent.adapters.llm.models_config import EmbeddingConfig, ModelPricing

OPENROUTER_EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"
HASH_DIMS = 256
_WORD = re.compile(r"[a-z0-9]+")


class EmbeddingsError(Exception):
    """The embeddings provider failed or answered something else than vectors."""


class HashEmbeddings(Embeddings):
    def __init__(self, dims: int = HASH_DIMS) -> None:
        self.dims = dims

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dims
        for word in _WORD.findall(text.lower()):
            digest = hashlib.sha256(word.encode("utf-8")).digest()
            vector[int.from_bytes(digest[:4], "big") % self.dims] += 1.0
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        return [x / norm for x in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class OpenRouterEmbeddings(Embeddings):
    def __init__(
        self,
        config: EmbeddingConfig,
        pricing: ModelPricing,
        key: SecretStr,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.pricing = pricing
        self._key = key
        self._transport = transport
        self.spent_usd = 0.0
        self.calls = 0

    @property
    def dims(self) -> int:
        return self.config.dims

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        async with httpx.AsyncClient(
            transport=self._transport, timeout=self.config.timeout_s
        ) as client:
            try:
                response = await client.post(
                    OPENROUTER_EMBEDDINGS_URL,
                    headers={
                        "Authorization": f"Bearer {self._key.get_secret_value()}",
                        "X-Title": "freight-agent",
                    },
                    json={"model": self.config.model, "input": texts},
                )
            except httpx.HTTPError as exc:
                raise EmbeddingsError(f"{self.config.model}: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise EmbeddingsError(
                f"{self.config.model}: HTTP {response.status_code}: {response.text[:200]}"
            )
        return self._vectors(response.json(), len(texts))

    def _vectors(self, body: Any, expected: int) -> list[list[float]]:
        try:
            data = cast("list[dict[str, Any]]", body["data"])
            ordered = sorted(data, key=lambda d: int(d.get("index", 0)))
            vectors = [[float(x) for x in d["embedding"]] for d in ordered]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingsError(f"{self.config.model}: unexpected answer ({exc})") from None
        if len(vectors) != expected or any(len(v) != self.dims for v in vectors):
            raise EmbeddingsError(
                f"{self.config.model}: {len(vectors)} vectors for {expected} texts, or not "
                f"{self.dims} dimensions"
            )
        usage = body.get("usage") if isinstance(body, dict) else None
        self.calls += 1
        self.spent_usd += _cost(cast("dict[str, Any]", usage or {}), self.pricing)
        return vectors

    async def aembed_query(self, text: str) -> list[float]:
        return (await self.aembed_documents([text]))[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """The synchronous path (LangChain's interface asks for one); the store calls the
        async one."""
        return asyncio.run(self.aembed_documents(texts))

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def _cost(usage: dict[str, Any], pricing: ModelPricing) -> float:
    reported = usage.get("cost")
    if isinstance(reported, int | float) and not isinstance(reported, bool):
        return float(reported)
    tokens = usage.get("prompt_tokens", 0)
    return pricing.cost_usd(input_tokens=tokens if isinstance(tokens, int) else 0, output_tokens=0)


def memory_store(
    embeddings: Embeddings, dims: int, *, fields: Sequence[str] = ("text",)
) -> InMemoryStore:
    """An in-memory LangGraph store whose index embeds the facts' `text`."""
    index: IndexConfig = {"embed": embeddings, "dims": dims, "fields": list(fields)}
    return InMemoryStore(index=index)


def embeddings_dims(embeddings: Embeddings) -> int:
    dims = getattr(embeddings, "dims", None)
    if not isinstance(dims, int):
        raise TypeError(f"{type(embeddings).__name__} does not say its dimensions")
    return dims
