"""
real_embedding_loader.py
Load real sentence-transformer embeddings from incidents JSON into Qdrant.

Purpose:
- Replace deterministic test vectors with real semantic embeddings
- Batch incidents from JSON
- Upsert them into Qdrant using vector_store.py + embedding_store.py

Usage examples:
    python real_embedding_loader.py
    python real_embedding_loader.py --limit 100
    python real_embedding_loader.py --json-path data/incidents.json --limit 250
    python real_embedding_loader.py --json-path data/incidents.json --limit 100 --batch-size 16

Notes:
- Safe for repeated runs because Qdrant IDs are deterministic in vector_store.py
- Starts small by default
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from embedding_store import get_embedding_dimension, ping_model, embed_texts
from vector_store import (
    VECTOR_SIZE,
    QDRANT_COLLECTION,
    ensure_collection,
    ping as qdrant_ping,
    upsert_points,
    count_points,
)


def _safe_read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _build_source_rows(json_path: Path, limit: int) -> List[Dict[str, Any]]:
    raw = _safe_read_json(json_path, default=[])
    if not isinstance(raw, list):
        raise ValueError(f"Expected a JSON array in {json_path}")

    rows: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue

        point_id = str(item.get("id") or "").strip()
        if not point_id:
            continue

        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or item.get("summary") or "").strip()
        classification = str(item.get("classification") or "unknown").strip()
        domain = str(item.get("domain") or "general").strip()
        service_scope = str(item.get("service_scope") or "").strip()
        environment = str(item.get("environment") or "").strip()
        severity = str(item.get("severity") or "").strip()

        combined = f"{title}. {content}".strip(". ").strip()
        if not combined:
            continue

        rows.append(
            {
                "id": point_id,
                "source_type": "incident",
                "title": title,
                "content": content,
                "combined_text": combined,
                "classification": classification,
                "domain": domain,
                "service_scope": service_scope,
                "environment": environment,
                "severity": severity,
                "extra_payload": {
                    "source": item.get("source"),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "test_vector": False,
                    "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
                },
            }
        )

        if len(rows) >= limit:
            break

    return rows


def _batched(items: List[Dict[str, Any]], batch_size: int) -> List[List[Dict[str, Any]]]:
    return [items[i:i + batch_size] for i in range(0, len(items), batch_size)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Load real embeddings from incidents JSON into Qdrant.")
    parser.add_argument("--json-path", default="data/incidents.json", help="Path to incidents JSON file")
    parser.add_argument("--limit", type=int, default=100, help="Number of incidents to load")
    parser.add_argument("--batch-size", type=int, default=16, help="Embedding/upsert batch size")
    args = parser.parse_args()

    json_path = Path(args.json_path)

    print(f"Embedding model healthy: {ping_model()}")
    if not ping_model():
        raise SystemExit("Embedding model is not available. Install sentence-transformers first.")

    dim = get_embedding_dimension()
    print(f"Embedding dimension: {dim}")
    if dim != VECTOR_SIZE:
        raise SystemExit(
            f"Embedding dimension mismatch. Model returned {dim}, but vector_store expects {VECTOR_SIZE}. "
            f"Update AIOPS_VECTOR_SIZE or use a matching model."
        )

    print(f"Qdrant reachable: {qdrant_ping()}")
    if not qdrant_ping():
        raise SystemExit("Qdrant is not reachable. Start the container first.")

    ensure_collection()

    source_rows = _build_source_rows(json_path, args.limit)
    print(f"Source rows prepared: {len(source_rows)}")
    if not source_rows:
        raise SystemExit(f"No valid incidents found in {json_path}")

    before = count_points()
    inserted_total = 0

    for batch_num, batch in enumerate(_batched(source_rows, args.batch_size), start=1):
        texts = [row["combined_text"] for row in batch]
        vectors = embed_texts(texts)

        upsert_rows = []
        for row, vector in zip(batch, vectors):
            upsert_rows.append(
                {
                    "id": row["id"],
                    "vector": vector,
                    "source_type": row["source_type"],
                    "title": row["title"],
                    "content": row["content"],
                    "classification": row["classification"],
                    "domain": row["domain"],
                    "service_scope": row["service_scope"],
                    "environment": row["environment"],
                    "severity": row["severity"],
                    "extra_payload": row["extra_payload"],
                }
            )

        inserted = upsert_points(upsert_rows)
        inserted_total += inserted
        print(f"Batch {batch_num}: embedded {len(batch)} rows, upserted {inserted}")

    after = count_points()

    print("-" * 80)
    print(f"Collection: {QDRANT_COLLECTION}")
    print(f"Input file: {json_path}")
    print(f"Requested rows: {args.limit}")
    print(f"Prepared rows: {len(source_rows)}")
    print(f"Inserted/upserted points total: {inserted_total}")
    print(f"Point count before: {before}")
    print(f"Point count after: {after}")

    if source_rows:
        print("Sample loaded point IDs:")
        for row in source_rows[:5]:
            print(f" - {row['id']} | {row['classification']} | {row['domain']}")


if __name__ == "__main__":
    main()
