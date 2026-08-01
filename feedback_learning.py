import json
from pathlib import Path
from typing import Any, Dict, List, Optional

WEIGHT_FILE = Path("data/feedback_weights.json")


def _load_json(path: Path, default: Any):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _clean_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _norm_token(value: Any, default: str = "") -> str:
    return _clean_str(value, default).lower().replace(" ", "_")


def _tenant_key(tenant_id: Any) -> str:
    return _norm_token(tenant_id, "client_a")


def _domain_key(domain: Any) -> str:
    return _norm_token(domain, "general")


def _class_key(classification: Any) -> str:
    return _norm_token(classification, "unknown")


def _build_weight_key(
    *,
    tenant_id: Any = "client_a",
    domain: Any = "general",
    classification: Any = "unknown",
) -> str:
    return f"{_tenant_key(tenant_id)}::{_domain_key(domain)}::{_class_key(classification)}"


def load_weights() -> Dict[str, Any]:
    return _load_json(WEIGHT_FILE, {})


def save_weights(weights: Dict[str, Any]) -> None:
    _save_json(WEIGHT_FILE, weights)


def get_weight_record(
    *,
    tenant_id: Any = "client_a",
    domain: Any = "general",
    classification: Any = "unknown",
) -> Dict[str, Any]:
    weights = load_weights()
    key = _build_weight_key(tenant_id=tenant_id, domain=domain, classification=classification)
    record = weights.get(key)
    if not isinstance(record, dict):
        record = {
            "tenant_id": _tenant_key(tenant_id),
            "domain": _domain_key(domain),
            "classification": _class_key(classification),
            "weight": 1.0,
            "positive": 0,
            "negative": 0,
            "last_updated": "",
        }
    return record


def update_feedback_weights(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Backward-compatible updater.

    Expected inputs can include either:
    - corrected_classification / original_classification / is_correct / tenant_id / domain
    or
    - corrected / domain
    """
    weights = load_weights()

    tenant_id = record.get("tenant_id", "client_a")
    domain = record.get("stored_domain") or record.get("submitted_domain") or record.get("domain") or "general"
    corrected = (
        record.get("corrected_classification")
        or record.get("corrected")
        or record.get("classification")
        or "unknown"
    )
    is_correct = bool(record.get("is_correct", True))

    key = _build_weight_key(tenant_id=tenant_id, domain=domain, classification=corrected)
    current = weights.get(key)
    if not isinstance(current, dict):
        current = {
            "tenant_id": _tenant_key(tenant_id),
            "domain": _domain_key(domain),
            "classification": _class_key(corrected),
            "weight": 1.0,
            "positive": 0,
            "negative": 0,
            "last_updated": "",
        }

    if is_correct:
        current["positive"] = int(current.get("positive", 0)) + 1
        current["weight"] = min(1.8, float(current.get("weight", 1.0)) + 0.10)
    else:
        current["negative"] = int(current.get("negative", 0)) + 1
        current["weight"] = max(0.45, float(current.get("weight", 1.0)) - 0.12)

    current["last_updated"] = record.get("reviewed_at") or record.get("updated_at") or ""
    weights[key] = current
    save_weights(weights)
    return current


def apply_feedback_weights(
    matches: List[Dict[str, Any]],
    *,
    tenant_id: str = "client_a",
) -> List[Dict[str, Any]]:
    """
    Apply tenant-aware feedback weights to retrieval results.

    Priority:
    1. exact tenant/domain/classification weight
    2. fallback to original score if no weight exists

    Adds:
    - feedback_weight
    - feedback_positive
    - feedback_negative
    """
    weights = load_weights()
    tenant_clean = _tenant_key(tenant_id)

    adjusted: List[Dict[str, Any]] = []
    for match in matches:
        row = dict(match)
        domain = row.get("domain", "general")
        classification = (
            row.get("classification")
            or row.get("title")
            or "unknown"
        )
        key = _build_weight_key(
            tenant_id=tenant_clean,
            domain=domain,
            classification=classification,
        )
        record = weights.get(key, {})
        weight = float(record.get("weight", 1.0)) if isinstance(record, dict) else 1.0

        try:
            row["score"] = float(row.get("score", 0.0)) * weight
        except Exception:
            row["score"] = 0.0

        row["feedback_weight"] = weight
        row["feedback_positive"] = int(record.get("positive", 0)) if isinstance(record, dict) else 0
        row["feedback_negative"] = int(record.get("negative", 0)) if isinstance(record, dict) else 0
        adjusted.append(row)

    return sorted(adjusted, key=lambda x: float(x.get("score", 0.0)), reverse=True)


def build_global_pattern_candidates(
    min_positive: int = 3,
    max_negative: int = 1,
) -> List[Dict[str, Any]]:
    """
    Returns learned patterns that may be good candidates for promotion into
    a global learned patterns index after sanitization / review.
    """
    weights = load_weights()
    candidates: List[Dict[str, Any]] = []
    for _, record in weights.items():
        if not isinstance(record, dict):
            continue
        positive = int(record.get("positive", 0))
        negative = int(record.get("negative", 0))
        if positive >= min_positive and negative <= max_negative:
            candidates.append(
                {
                    "tenant_id": record.get("tenant_id", "client_a"),
                    "domain": record.get("domain", "general"),
                    "classification": record.get("classification", "unknown"),
                    "weight": float(record.get("weight", 1.0)),
                    "positive": positive,
                    "negative": negative,
                    "last_updated": record.get("last_updated", ""),
                    "promotion_ready": True,
                }
            )
    candidates.sort(key=lambda x: (x["positive"], x["weight"]), reverse=True)
    return candidates
