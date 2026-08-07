import json
import os
import uuid
from pathlib import Path

import requests
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams, PointStruct

# ----------------------------
# Config
# ----------------------------
INCIDENTS_FILE = Path(r"C:\projects\ai\slm\data\incidents.json")
COLLECTION_NAME = "incident_knowledge"
LM_STUDIO_BASE = os.getenv("MODEL_BASE_URL", "http://127.0.0.1:1234/v1")
EMBED_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-nomic-embed-text-v1.5")
QDRANT_HOST = "localhost"
QDRANT_PORT = 6333


def get_embedding(text: str) -> list[float]:
    url = f"{LM_STUDIO_BASE}/embeddings"
    payload = {
        "model": EMBED_MODEL,
        "input": text
    }
    response = requests.post(url, json=payload, timeout=120)
    response.raise_for_status()
    data = response.json()
    return data["data"][0]["embedding"]


def build_text(record: dict) -> str:
    return (
        f"Domain: {record['domain']}\n"
        f"Title: {record['title']}\n"
        f"Content: {record['content']}"
    )


def main() -> None:
    if not INCIDENTS_FILE.exists():
        raise FileNotFoundError(f"Incidents file not found: {INCIDENTS_FILE}")

    with INCIDENTS_FILE.open("r", encoding="utf-8") as f:
        incidents = json.load(f)

    if not incidents:
        raise ValueError("incidents.json is empty")

    print(f"Loaded {len(incidents)} incidents from {INCIDENTS_FILE}")

    # Get one embedding first so we know vector size
    sample_text = build_text(incidents[0])
    sample_vector = get_embedding(sample_text)
    vector_size = len(sample_vector)
    print(f"Embedding size detected: {vector_size}")

    client = QDRANT_HOST
    qdrant = QdrantClient(host=client, port=QDRANT_PORT)

    existing = [c.name for c in qdrant.get_collections().collections]
    if COLLECTION_NAME not in existing:
        print(f"Creating collection: {COLLECTION_NAME}")
        qdrant.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )
    else:
        print(f"Collection already exists: {COLLECTION_NAME}")

    points: list[PointStruct] = []

    for i, incident in enumerate(incidents, start=1):
        text = build_text(incident)
        embedding = get_embedding(text)

        payload = {
            "incident_id": incident["id"],
            "domain": incident["domain"],
            "title": incident["title"],
            "content": incident["content"],
            "full_text": text,
        }

        points.append(
            PointStruct(
                id=str(uuid.uuid4()),
                vector=embedding,
                payload=payload,
            )
        )
        print(f"Prepared {i}/{len(incidents)} -> {incident['id']}")

    qdrant.upsert(
        collection_name=COLLECTION_NAME,
        points=points,
        wait=True,
    )

    print(f"\nDone. Upserted {len(points)} incidents into '{COLLECTION_NAME}'.")


if __name__ == "__main__":
    main()