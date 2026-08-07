import os
import requests
from qdrant_client import QdrantClient

LM_STUDIO_BASE = os.getenv("MODEL_BASE_URL", "http://127.0.0.1:1234/v1")
EMBED_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-nomic-embed-text-v1.5")
CHAT_MODEL = os.getenv("PRIMARY_VISION_MODEL", os.getenv("PRIMARY_CHAT_MODEL", "gemma-3n-4b-vision-finetuned-i1"))
COLLECTION_NAME = "incident_knowledge"


def get_embedding(text: str):
    response = requests.post(
        f"{LM_STUDIO_BASE}/embeddings",
        json={
            "model": EMBED_MODEL,
            "input": text
        },
        timeout=120
    )
    response.raise_for_status()
    return response.json()["data"][0]["embedding"]


def search_incidents(query: str, limit: int = 3):
    qdrant = QdrantClient(host="localhost", port=6333)
    query_vector = get_embedding(query)

    results = qdrant.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        limit=limit
    ).points

    return results


def build_context(results):
    context_blocks = []

    for idx, r in enumerate(results, start=1):
        payload = r.payload
        context_blocks.append(
            f"""Retrieved Incident {idx}
Score: {r.score:.4f}
Incident ID: {payload.get("incident_id")}
Domain: {payload.get("domain")}
Title: {payload.get("title")}
Content: {payload.get("content")}
"""
        )

    return "\n".join(context_blocks)


def ask_llm(user_ticket: str, context: str):
    system_prompt = """
You are an IT operations support assistant specializing in compute and database incidents.

Use the retrieved incident knowledge below to help analyze the new issue.

Return your answer in this exact structure:

Classification:
Probable Causes:
Next Checks:
Recommended Actions:
Confidence:

Be concise, practical, and operations-focused.
Do not invent facts not supported by the retrieved context or common platform knowledge.
"""

    user_prompt = f"""
New Incident:
{user_ticket}

Retrieved Knowledge:
{context}
"""

    response = requests.post(
        f"{LM_STUDIO_BASE}/chat/completions",
        json={
            "model": CHAT_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.2,
            "max_tokens": 500
        },
        timeout=180
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def main():
    query = "SQL Server transaction log full after backup failure"

    print("\nNew Ticket:")
    print(query)

    results = search_incidents(query, limit=3)
    context = build_context(results)

    print("\nRetrieved Context:")
    print(context)

    answer = ask_llm(query, context)

    print("\nLLM Analysis:\n")
    print(answer)


if __name__ == "__main__":
    main()