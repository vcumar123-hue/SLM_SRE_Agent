from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Iterable, List

from sentence_transformers import SentenceTransformer

LOGGER = logging.getLogger("embedding_store")
if not LOGGER.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MODEL_NAME = os.getenv("AIOPS_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")


@lru_cache(maxsize=1)
def get_model() -> SentenceTransformer:
    """
    Load and cache the local embedding model.
    """
    LOGGER.info("Loading embedding model: %s", MODEL_NAME)
    return SentenceTransformer(MODEL_NAME)


def ping_model() -> bool:
    try:
        model = get_model()
        _ = model.encode(["health check"], normalize_embeddings=True)
        return True
    except Exception:
        LOGGER.exception("Embedding model health check failed")
        return False


def get_embedding_dimension() -> int:
    """
    Return embedding dimension for the configured model.
    """
    model = get_model()
    dim = model.get_embedding_dimension()
    return int(dim)


def embed_text(text: str, normalize: bool = True) -> List[float]:
    """
    Embed a single text string.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("text cannot be empty")

    model = get_model()
    vector = model.encode(text, normalize_embeddings=normalize)
    return [float(x) for x in vector.tolist()]


def embed_texts(texts: Iterable[str], normalize: bool = True) -> List[List[float]]:
    """
    Embed multiple texts in batch.
    """
    cleaned = [str(t or "").strip() for t in texts]
    if not cleaned:
        return []

    model = get_model()
    vectors = model.encode(cleaned, normalize_embeddings=normalize)
    return [[float(x) for x in row.tolist()] for row in vectors]


if __name__ == "__main__":
    print("Model healthy:", ping_model())
    if ping_model():
        print("Embedding dimension:", get_embedding_dimension())
        print("Single embedding length:", len(embed_text("Users cannot access VPN after password reset")))
