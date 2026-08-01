import os
import requests
import numpy as np
import json

LM_STUDIO_URL = "http://localhost:1234/v1"
EMBED_MODEL = "text-embedding-nomic-embed-text-v1.5"

KB_FOLDER = "kb"
OUTPUT_FILE = "vector_store.json"


def get_embedding(text):
    r = requests.post(
        f"{LM_STUDIO_URL}/embeddings",
        json={
            "model": EMBED_MODEL,
            "input": text
        },
        timeout=60
    )
    r.raise_for_status()
    return r.json()["data"][0]["embedding"]


vector_db = []

for file in os.listdir(KB_FOLDER):

    with open(f"{KB_FOLDER}/{file}", "r", encoding="utf-8") as f:
        text = f.read()

    chunks = text.split("\n\n")

    for chunk in chunks:

        embedding = get_embedding(chunk)

        vector_db.append({
            "text": chunk,
            "embedding": embedding
        })


with open(OUTPUT_FILE, "w") as f:
    json.dump(vector_db, f)

print("Knowledge base indexed.")