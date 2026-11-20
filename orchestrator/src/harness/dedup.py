"""Dedup incoming findings by embedding similarity (guide: Qdrant). A confirmed
verdict only becomes a *unique* finding if it isn't near an earlier one.

MVP: in-memory cosine over this run's embeddings, which is enough to collapse
repeats within a run. If QDRANT_URL is set the vectors are also upserted to
Qdrant (best-effort, non-fatal) so dedup can persist across runs and the
dashboard's Qdrant view populates.
"""
from __future__ import annotations

import math

import httpx

COLLECTION = "harness_findings"


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class Dedup:
    def __init__(self, threshold: float, qdrant_url: str | None = None):
        self.threshold = threshold
        self.qdrant_url = qdrant_url.rstrip("/") if qdrant_url else None
        self._seen: list[tuple[list[float], str]] = []
        self._dim: int | None = None
        if self.qdrant_url:
            self._ensure_collection()

    def _ensure_collection(self) -> None:
        # best-effort; a failure just means in-memory only
        try:
            httpx.get(f"{self.qdrant_url}/collections/{COLLECTION}", timeout=5)
        except Exception:
            self.qdrant_url = None

    def is_duplicate(self, vector: list[float], label: str) -> tuple[bool, float]:
        best = 0.0
        for vec, _lbl in self._seen:
            if len(vec) != len(vector):
                continue
            best = max(best, cosine(vec, vector))
        dup = best >= self.threshold
        if not dup:
            self._seen.append((vector, label))
            self._persist(vector, label)
        return dup, best

    def _persist(self, vector: list[float], label: str) -> None:
        if not self.qdrant_url:
            return
        try:
            if self._dim is None:
                self._dim = len(vector)
                httpx.put(
                    f"{self.qdrant_url}/collections/{COLLECTION}",
                    json={"vectors": {"size": self._dim, "distance": "Cosine"}},
                    timeout=5,
                )
            pid = len(self._seen)
            httpx.put(
                f"{self.qdrant_url}/collections/{COLLECTION}/points",
                json={"points": [{"id": pid, "vector": vector, "payload": {"label": label}}]},
                timeout=5,
            )
        except Exception:
            pass
