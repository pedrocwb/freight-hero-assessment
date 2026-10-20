"""The OpenRouter embedder (`POST /api/v1/embeddings`, `openai/text-embedding-3-small`,
1536 dimensions, from `config/models.yaml` `embedding`), for live and record runs.

Owner: card W3-KNW. Tested with an httpx `MockTransport`; offline runs (scripted,
replay, CI) use `fake_embedder.FakeEmbedder`, through `embedder_for`.

The request is OpenAI's: `{"model": ..., "input": [texts]}`; the answer's `data` holds
one `{"index", "embedding"}` per text. Texts go in batches, so a long SOP is a few
requests. Any failure is an `EmbeddingError` naming the model, never the key.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import httpx
from pydantic import SecretStr

from freight_agent.adapters.knowledge.fake_embedder import FakeEmbedder
from freight_agent.adapters.llm.models_config import load_models_config
from freight_agent.ports.knowledge import Embedder, EmbeddingError
from freight_agent.settings import Settings

OPENROUTER_API: Final = "https://openrouter.ai/api/v1"
DEFAULT_MODEL: Final = "openai/text-embedding-3-small"
DEFAULT_DIM: Final = 1536  # the Postgres index's column
_MAX_DETAIL: Final = 200


class OpenRouterEmbedder:
    """Implements `ports.knowledge.Embedder` over OpenRouter's embeddings endpoint."""

    def __init__(
        self,
        api_key: SecretStr,
        *,
        model: str = DEFAULT_MODEL,
        dim: int = DEFAULT_DIM,
        base_url: str = OPENROUTER_API,
        timeout_s: float = 30.0,
        batch_size: int = 64,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self._key = api_key
        self._model = model
        self._dim = dim
        self._base_url = base_url
        self._timeout_s = timeout_s
        self._batch_size = batch_size
        self._transport = transport

    def __repr__(self) -> str:
        return f"OpenRouterEmbedder(model={self._model!r}, dim={self._dim})"

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"Authorization": f"Bearer {self._key.get_secret_value()}"}
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=self._timeout_s,
            transport=self._transport,
        ) as client:
            for start in range(0, len(texts), self._batch_size):
                batch = list(texts[start : start + self._batch_size])
                vectors.extend(await self._embed_batch(client, batch))
        return vectors

    async def _embed_batch(self, client: httpx.AsyncClient, batch: list[str]) -> list[list[float]]:
        try:
            response = await client.post("/embeddings", json={"model": self._model, "input": batch})
        except httpx.HTTPError as exc:
            raise self._error(f"request failed: {type(exc).__name__}") from None
        if response.status_code != httpx.codes.OK:
            raise self._error(f"HTTP {response.status_code}: {response.text[:_MAX_DETAIL]}")
        try:
            body: Any = response.json()
            data = sorted(body["data"], key=lambda item: item["index"])
            vectors = [[float(v) for v in item["embedding"]] for item in data]
        except (ValueError, KeyError, TypeError) as exc:
            raise self._error(f"unexpected answer ({type(exc).__name__})") from None
        if len(vectors) != len(batch):
            raise self._error(f"{len(vectors)} embeddings for {len(batch)} texts")
        if any(len(vector) != self._dim for vector in vectors):
            raise self._error(f"an embedding does not have {self._dim} dimensions")
        return vectors

    def _error(self, problem: str) -> EmbeddingError:
        return EmbeddingError(f"embedding model {self._model}: {problem}")


def embedder_for(settings: Settings) -> Embedder:
    """The embedder of `settings.llm_mode`: OpenRouter in live and record mode (the key
    is required), the deterministic fake otherwise. Both give `config/models.yaml`'s
    `embedding.dim` (1536 when the file has no `embedding`)."""
    model, dim = _embedding_config(settings.models_config)
    if settings.llm_mode in ("live", "record"):
        return OpenRouterEmbedder(settings.require_openrouter_key(), model=model, dim=dim)
    return FakeEmbedder(dim=dim)


def _embedding_config(path: Path) -> tuple[str, int]:
    if not path.is_file():
        return DEFAULT_MODEL, DEFAULT_DIM
    embedding = load_models_config(path).embedding
    if embedding is None:
        return DEFAULT_MODEL, DEFAULT_DIM
    return embedding.model, embedding.dim
