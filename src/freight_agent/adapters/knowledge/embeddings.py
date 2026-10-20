"""The OpenRouter embedder (`POST /api/v1/embeddings`, `openai/text-embedding-3-small`,
1536 dimensions, from `config/models.yaml` `embedding`), for live and record runs.

Owner: card W3-KNW (stub from C0). Test it with an httpx `MockTransport`; offline runs
use `fake_embedder.FakeEmbedder`.
"""
