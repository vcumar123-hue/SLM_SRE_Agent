import os
import requests
import numpy as np
import json

LM_STUDIO_URL = os.getenv("MODEL_BASE_URL", "http://localhost:1234/v1")
EMBED_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-nomic-embed-text-v1.5")

with open("vector_store.json") as f:
    vector_db = json.load(f)


def get_embedding(text):
    r = requests.post(
        f"{LM_STUDIO_URL}/embeddings",
        json={
            "model": EMBED_MODEL,
            "input": text
        }
    )
    return np.array(r.json()["data"][0]["embedding"])


def cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))


def retrieve(question):

    q_embed = get_embedding(question)

    scores = []

    for item in vector_db:
        emb = np.array(item["embedding"])
        score = cosine_similarity(q_embed, emb)
        scores.append((score, item["text"]))

    scores.sort(reverse=True)

    return [x[1] for x in scores[:3]]