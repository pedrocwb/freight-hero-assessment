"""A deterministic `Embedder` for scripted, replay and CI runs (no network, no key).

Owner: card W3-KNW (C0 wrote a working version).

Each word is hashed into one of `dim` buckets with a sign, and the vector is
L2-normalized, so texts that share words point the same way and the same text always
gives the same vector. It is not a language model: it only has to be stable, since tools
never run offline and no real query is embedded there (F11).
"""

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class FakeEmbedder:
    """Implements `ports.knowledge.Embedder`."""

    dim: int = 64
    model_id: str = "fake/token-hash"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for word in _WORD.findall(text.lower()):
            digest = hashlib.sha256(word.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dim
            vector[bucket] += 1.0 if digest[4] % 2 == 0 else -1.0
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector
