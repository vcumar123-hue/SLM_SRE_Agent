import os
import requests
from qdrant_client import QdrantClient

LM_STUDIO_BASE = os.getenv("MODEL_BASE_URL", "http://127.0.0.1:1234/v1")
EMBED_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-nomic-embed-text-v1.5")
COLLECTION_NAME = "incident_knowledge"


def get_embedding(text: str):
    response = requests.post(
        f"{LM_STUDIO_BASE}/embeddings",
        json={
            "model": EMBED_MODEL,
            "input": text
        }
    )
    response.raise_for_status()
    return response.json()["data"][0]["embedding"]


def main():

    query = "SQL Server transaction log full after backup failure"

    query_vector = get_embedding(query)

    qdrant = QdrantClient(host="localhost", port=6333)

    results = qdrant.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        limit=3
    ).points

    print("\nQuery:", query)
    print("\nTop matches:\n")

    for r in results:
        payload = r.payload
        print("Score:", r.score)
        print("Incident:", payload["incident_id"])
        print("Title:", payload["title"])
        print("Content:", payload["content"])
        print("------------")


if __name__ == "__main__":
    main()