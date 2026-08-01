# vectorize_and_upsert.py
# AI Ops Platform - Production Bulk Vector Loader
# Reads /app/data/rag/index_ready.json
# Generates embeddings
# Upserts into Qdrant collections
# Resume-safe / checkpointed

from __future__ import annotations

import json
import os
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List

from embedding_store import embed_texts, ping_model
from vector_store import ensure_collection, upsert_points


# =====================================================
# CONFIG
# =====================================================

INPUT_FILE = os.getenv("AIOPS_RAG_INDEX_FILE", "/app/data/rag/index_ready.json")
CHECKPOINT_FILE = os.getenv("AIOPS_RAG_CHECKPOINT_FILE", "/app/data/rag/upsert_checkpoint.json")
FAILED_FILE = os.getenv("AIOPS_RAG_FAILED_FILE", "/app/data/rag/upsert_failed.json")

BATCH_SIZE = int(os.getenv("AIOPS_RAG_BATCH_SIZE", "128"))
SLEEP_SECONDS = float(os.getenv("AIOPS_RAG_SLEEP_SECONDS", "0"))

DEFAULT_TENANT = os.getenv("AIOPS_DEFAULT_TENANT_ID", "client_a")

CLIENT_COLLECTION = f"{DEFAULT_TENANT}_index"
GLOBAL_BASE_COLLECTION = "global_base_index"
GLOBAL_LEARNED_COLLECTION = "global_learned_patterns_index"


# =====================================================
# HELPERS
# =====================================================

def read_json(path: str, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: str, data):
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def clean(v: Any) -> str:
    return str(v or "").strip()


def build_text(row: Dict[str, Any]) -> str:
    parts = [
        clean(row.get("title")),
        clean(row.get("content")),
        clean(row.get("summary")),
        clean(row.get("classification")),
        clean(row.get("domain")),
        clean(row.get("service_scope")),
        clean(row.get("knowledge_type")),
        clean(row.get("system")),
    ]
    txt = ". ".join([p for p in parts if p])
    return txt[:8000]


def choose_collection(row: Dict[str, Any]) -> str:
    scope = clean(row.get("knowledge_scope")).lower()
    source = clean(row.get("source_type")).lower()

    if scope == "global_base":
        return GLOBAL_BASE_COLLECTION

    if scope == "global_learned":
        return GLOBAL_LEARNED_COLLECTION

    if source in {"incident", "canonical_incident"}:
        return GLOBAL_BASE_COLLECTION

    return CLIENT_COLLECTION


def build_point(row: Dict[str, Any], vector: List[float]) -> Dict[str, Any]:
    row_id = clean(row.get("id")) or clean(row.get("incident_id")) or clean(row.get("point_id"))

    if not row_id:
        row_id = f"auto_{abs(hash(build_text(row)))}"

    tenant_id = clean(row.get("tenant_id")) or DEFAULT_TENANT
    knowledge_scope = clean(row.get("knowledge_scope")) or "client_private"

    collection = choose_collection(row)

    return {
        "id": row_id,
        "vector": vector,
        "source_type": clean(row.get("source_type")) or "document",
        "title": clean(row.get("title")),
        "content": clean(row.get("content")) or clean(row.get("summary")),
        "classification": clean(row.get("classification")) or "unknown",
        "domain": clean(row.get("domain")) or "general",
        "service_scope": clean(row.get("service_scope")),
        "environment": clean(row.get("environment")),
        "severity": clean(row.get("severity")),
        "extra_payload": {
            "tenant_id": tenant_id,
            "collection_name": collection,
            "knowledge_scope": knowledge_scope,
            "knowledge_type": clean(row.get("knowledge_type")),
            "source_layer": clean(row.get("source_layer")),
            "source_file": clean(row.get("source_file")),
            "doc_type": clean(row.get("doc_type")),
        },
        "collection_name": collection,
    }


def ensure_all_collections():
    print("Ensuring Qdrant collections...")
    ensure_collection(collection_name=CLIENT_COLLECTION)
    ensure_collection(collection_name=GLOBAL_BASE_COLLECTION)
    ensure_collection(collection_name=GLOBAL_LEARNED_COLLECTION)


# =====================================================
# MAIN
# =====================================================

def main():
    print("======================================")
    print("AI Ops Bulk Vector Loader Starting...")
    print("======================================")

    if not ping_model():
        raise RuntimeError("Embedding model not healthy")

    ensure_all_collections()

    rows = read_json(INPUT_FILE, [])
    if not isinstance(rows, list):
        raise RuntimeError("index_ready.json must contain list")

    total = len(rows)
    print(f"Loaded rows: {total}")

    checkpoint = read_json(CHECKPOINT_FILE, {"last_index": 0})
    start_idx = int(checkpoint.get("last_index", 0))

    failed_rows = read_json(FAILED_FILE, [])

    print(f"Resuming from row: {start_idx}")

    started = time.time()

    for batch_start in range(start_idx, total, BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE, total)
        batch = rows[batch_start:batch_end]

        texts = []
        usable_rows = []

        for row in batch:
            txt = build_text(row)
            if txt:
                texts.append(txt)
                usable_rows.append(row)

        if not texts:
            continue

        try:
            vectors = embed_texts(texts)

            by_collection = {}

            for row, vec in zip(usable_rows, vectors):
                point = build_point(row, vec)
                col = point["collection_name"]
                by_collection.setdefault(col, []).append(point)

            inserted = 0

            for collection_name, points in by_collection.items():
                count = upsert_points(points, collection_name=collection_name)
                inserted += count

            checkpoint["last_index"] = batch_end
            write_json(CHECKPOINT_FILE, checkpoint)

            elapsed = time.time() - started
            rate = batch_end / elapsed if elapsed > 0 else 0

            print(
                f"[OK] {batch_end}/{total} "
                f"({round(batch_end/total*100,1)}%) "
                f"Inserted={inserted} "
                f"Rate={round(rate,1)} rows/sec"
            )

        except Exception as e:
            print(f"[ERROR] Batch {batch_start}-{batch_end}: {e}")

            failed_rows.append({
                "batch_start": batch_start,
                "batch_end": batch_end,
                "error": str(e),
                "traceback": traceback.format_exc(),
            })
            write_json(FAILED_FILE, failed_rows)

        if SLEEP_SECONDS > 0:
            time.sleep(SLEEP_SECONDS)

    total_time = round(time.time() - started, 1)

    print("======================================")
    print("Bulk Load Completed")
    print(f"Rows processed: {total}")
    print(f"Elapsed sec: {total_time}")
    print(f"Checkpoint file: {CHECKPOINT_FILE}")
    print(f"Failed file: {FAILED_FILE}")
    print("======================================")


if __name__ == "__main__":
    main()