from __future__ import annotations

import logging
import math
import os
from typing import Iterable, List

LOGGER = logging.getLogger("embedding_store")
if not LOGGER.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# LM Studio serves an OpenAI-compatible API.
# Override via env vars if your LM Studio runs on a different port or host.
LM_STUDIO_BASE_URL = os.getenv("LM_STUDIO_BASE_URL", os.getenv("OLLAMA_BASE_URL", "http://localhost:1234/v1"))
MODEL_NAME = os.getenv(
    "EMBEDDING_MODEL",
    os.getenv("AIOPS_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
)

# Fallback: sentence_transformers when LM Studio is unavailable and
# EMBEDDING_BACKEND=sentence_transformers is set.
EMBEDDING_BACKEND = os.getenv("EMBEDDING_BACKEND", "sentence_transformers").strip().lower()


# ── LM Studio / OpenAI-compatible backend ─────────────────────────────────────

def _lm_studio_embed(texts: List[str]) -> List[List[float]]:
    import requests
    url = f"{LM_STUDIO_BASE_URL.rstrip('/')}/embeddings"
    resp = requests.post(
        url,
        json={"model": MODEL_NAME, "input": texts},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    # OpenAI format: {"data": [{"embedding": [...], "index": 0}, ...]}
    items = sorted(data["data"], key=lambda x: x["index"])
    return [[float(v) for v in item["embedding"]] for item in items]


def _normalize(vec: List[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


# ── sentence_transformers fallback ─────────────────────────────────────────────

_ST_MODEL = None


def _st_model():
    """Load the sentence-transformers model once per process (loading takes about a second)."""
    global _ST_MODEL
    if _ST_MODEL is None:
        from sentence_transformers import SentenceTransformer  # type: ignore[import]
        LOGGER.info("Loading sentence-transformers model: %s", MODEL_NAME)
        _ST_MODEL = SentenceTransformer(MODEL_NAME)
    return _ST_MODEL


def _st_embed(texts: List[str], normalize: bool) -> List[List[float]]:
    vectors = _st_model().encode(texts, normalize_embeddings=normalize, show_progress_bar=False)
    return [[float(x) for x in row.tolist()] for row in vectors]


# ── Public API ────────────────────────────────────────────────────────────────

def embed_texts(texts: Iterable[str], normalize: bool = True) -> List[List[float]]:
    cleaned = [str(t or "").strip() for t in texts]
    if not cleaned:
        return []

    if EMBEDDING_BACKEND == "sentence_transformers":
        return _st_embed(cleaned, normalize)

    vecs = _lm_studio_embed(cleaned)
    if normalize:
        vecs = [_normalize(v) for v in vecs]
    return vecs


def embed_text(text: str, normalize: bool = True) -> List[float]:
    text = (text or "").strip()
    if not text:
        raise ValueError("text cannot be empty")
    return embed_texts([text], normalize=normalize)[0]


def ping_model() -> bool:
    try:
        result = embed_texts(["health check"])
        return bool(result and len(result[0]) > 0)
    except Exception:
        LOGGER.exception("Embedding model health check failed")
        return False


def get_embedding_dimension() -> int:
    return len(embed_text("dimension probe"))


# Legacy compat: some callers do `from embedding_store import get_model`
def get_model():
    raise RuntimeError(
        "get_model() is not available with the LM Studio backend. "
        "Use embed_text() / embed_texts() instead."
    )


if __name__ == "__main__":
    print(f"Backend : {EMBEDDING_BACKEND}")
    print(f"Model   : {MODEL_NAME}")
    print(f"Base URL: {LM_STUDIO_BASE_URL}")
    print("Healthy :", ping_model())
    if ping_model():
        print("Dim     :", get_embedding_dimension())
        print("Vec len :", len(embed_text("Users cannot access VPN after password reset")))
