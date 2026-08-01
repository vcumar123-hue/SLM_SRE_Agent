from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import settings_portable as settings


def _path(name: str, default: Path) -> Path:
    value = getattr(settings, name, default)
    return value if isinstance(value, Path) else Path(str(value))


FEEDBACK_LEARNING_FILE = _path("FEEDBACK_LEARNING_FILE", Path("data/feedback_learning.json"))
FEEDBACK_FILE = _path("FEEDBACK_FILE", Path("data/feedback.json"))


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _safe_write_json(path: Path, payload: Any) -> None:
    _ensure_parent(path)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _normalize_label(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "unknown"


def _key(domain: str, classification: str) -> str:
    return f"{(domain or '').strip().lower()}::{_normalize_label(classification)}"


def _half_life_days() -> float:
    return float(
        getattr(settings, "LEARNING_HALF_LIFE_DAYS",
        getattr(settings, "LEARNING_DECAY_DAYS", 30))
    )


def _positive_delta() -> float:
    return float(
        getattr(settings, "LEARNING_POSITIVE_DELTA",
        getattr(settings, "LEARNING_POSITIVE_INCREMENT", 0.25))
    )


def _negative_delta() -> float:
    return float(
        getattr(settings, "LEARNING_NEGATIVE_DELTA",
        getattr(settings, "LEARNING_NEGATIVE_INCREMENT", 0.20))
    )


def _min_weight() -> float:
    return float(getattr(settings, "LEARNING_MIN_WEIGHT", 0.50))


def _max_weight() -> float:
    return float(getattr(settings, "LEARNING_MAX_WEIGHT", 5.00))


def _base_weight() -> float:
    return float(getattr(settings, "LEARNING_BASE_WEIGHT", 1.00))


def _apply_decay(entry: Dict[str, Any]) -> Dict[str, Any]:
    weight = float(entry.get("weight", _base_weight()))
    updated_at = entry.get("updated_at")
    half_life = _half_life_days()
    if not updated_at or half_life <= 0:
        entry["effective_weight"] = round(weight, 6)
        return entry
    try:
        ts = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
        age_days = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0)
    except Exception:
        entry["effective_weight"] = round(weight, 6)
        return entry
    retained = math.exp(-math.log(2) * (age_days / half_life))
    eff = _base_weight() + (weight - _base_weight()) * retained
    entry["effective_weight"] = round(_clamp(eff, _min_weight(), _max_weight()), 6)
    entry["staleness_days"] = round(age_days, 2)
    return entry


def _load_weights() -> Dict[str, Any]:
    payload = _safe_read_json(FEEDBACK_LEARNING_FILE, default={})
    return payload if isinstance(payload, dict) else {}


def _save_weights(payload: Dict[str, Any]) -> None:
    _safe_write_json(FEEDBACK_LEARNING_FILE, payload)


def get_weight_entry(domain: str, classification: str) -> Dict[str, Any]:
    weights = _load_weights()
    key = _key(domain, classification)
    entry = weights.get(key)
    if not isinstance(entry, dict):
        entry = {
            "weight": _base_weight(),
            "effective_weight": _base_weight(),
            "positive_count": 0,
            "negative_count": 0,
            "correction_count": 0,
            "conflict_count": 0,
            "last_ticket": None,
            "updated_at": None,
            "domain": (domain or "").strip().lower() or "general",
            "classification": _normalize_label(classification),
            "last_predicted_as": None,
        }
    return _apply_decay(entry)


def update_feedback_weights(record: Dict[str, Any]) -> Dict[str, Any]:
    weights = _load_weights()

    domain = (record.get("stored_domain") or record.get("domain") or "general").strip().lower()
    predicted = _normalize_label(record.get("original_classification") or record.get("predicted", ""))
    corrected = _normalize_label(record.get("corrected_classification") or record.get("corrected", ""))
    ticket = record.get("ticket")
    is_correct = bool(record.get("is_correct", predicted == corrected if predicted and corrected else True))

    corrected_key = _key(domain, corrected)
    corrected_entry = get_weight_entry(domain, corrected)
    increment = _positive_delta()

    # frequency bonus and conflict handling
    frequency_bonus = min(0.20, 0.02 * int(corrected_entry.get("positive_count", 0)))
    corrected_entry["weight"] = _clamp(
        float(corrected_entry.get("weight", _base_weight())) + increment + frequency_bonus,
        _min_weight(),
        _max_weight(),
    )
    corrected_entry["positive_count"] = int(corrected_entry.get("positive_count", 0)) + 1
    corrected_entry["last_ticket"] = ticket
    corrected_entry["updated_at"] = _now_iso()
    corrected_entry["domain"] = domain
    corrected_entry["classification"] = corrected
    if predicted and predicted != corrected:
        corrected_entry["correction_count"] = int(corrected_entry.get("correction_count", 0)) + 1
        corrected_entry["last_predicted_as"] = predicted
    corrected_entry = _apply_decay(corrected_entry)
    weights[corrected_key] = corrected_entry

    penalized_key = None
    if predicted and predicted != corrected:
        penalized_key = _key(domain, predicted)
        penalized_entry = get_weight_entry(domain, predicted)
        decrement = _negative_delta()
        conflict_penalty = min(0.15, 0.03 * int(penalized_entry.get("negative_count", 0)))
        penalized_entry["weight"] = _clamp(
            float(penalized_entry.get("weight", _base_weight())) - decrement - conflict_penalty,
            _min_weight(),
            _max_weight(),
        )
        penalized_entry["negative_count"] = int(penalized_entry.get("negative_count", 0)) + 1
        penalized_entry["conflict_count"] = int(penalized_entry.get("conflict_count", 0)) + 1
        penalized_entry["last_ticket"] = ticket
        penalized_entry["updated_at"] = _now_iso()
        penalized_entry["domain"] = domain
        penalized_entry["classification"] = predicted
        penalized_entry["last_predicted_as"] = predicted
        penalized_entry = _apply_decay(penalized_entry)
        weights[penalized_key] = penalized_entry

    _save_weights(weights)
    return {"corrected_key": corrected_key, "penalized_key": penalized_key, "weights": weights}


def record_feedback_event(record: Dict[str, Any]) -> Dict[str, Any]:
    history = _safe_read_json(FEEDBACK_FILE, default=[])
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "timestamp": record.get("timestamp") or _now_iso(),
            "request_id": record.get("request_id"),
            "ticket": record.get("ticket"),
            "original_domain": record.get("original_domain"),
            "original_classification": record.get("original_classification"),
            "corrected_classification": record.get("corrected_classification"),
            "submitted_domain": record.get("submitted_domain"),
            "stored_domain": record.get("stored_domain"),
            "is_correct": bool(record.get("is_correct", False)),
        }
    )
    _safe_write_json(FEEDBACK_FILE, history)
    weights = update_feedback_weights(record)
    return {"status": "ok", "history_count": len(history), **weights}


def store_feedback_learning(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Persist the correction into the weights store and a searchable normalized mirror.
    """
    result = update_feedback_weights(record)
    weights = _load_weights()
    mirror: List[Dict[str, Any]] = []
    for entry in weights.values():
        if isinstance(entry, dict):
            mirror.append(_apply_decay(dict(entry)))
    mirror.sort(key=lambda x: float(x.get("effective_weight", 1.0)), reverse=True)
    _safe_write_json(FEEDBACK_LEARNING_FILE, {k: v for k, v in weights.items()})
    return {"status": "ok", "entries": len(mirror), **result}


def search_feedback_learning(ticket: str, top_k: int = 3) -> List[Dict[str, Any]]:
    """
    Search learned classes using lightweight token overlap against the ticket text.
    Returns the same shape expected by app.py normalize_retrieved_incidents.
    """
    ticket = (ticket or "").strip().lower()
    if not ticket:
        return []

    tokens = set(re.findall(r"[a-z0-9_]+", ticket))
    if not tokens:
        return []

    weights = _load_weights()
    results: List[Dict[str, Any]] = []
    for key, entry in weights.items():
        if not isinstance(entry, dict):
            continue
        e = _apply_decay(dict(entry))
        classification = str(e.get("classification") or "").strip()
        domain = str(e.get("domain") or "general").strip().lower()
        label_tokens = set(re.findall(r"[a-z0-9_]+", classification.replace("_", " ")))
        overlap = len(tokens & label_tokens) / max(1, len(label_tokens)) if label_tokens else 0.0
        score = overlap * 0.45 + min(0.55, float(e.get("effective_weight", 1.0)) / max(_max_weight(), 1.0))
        if score <= 0.20:
            continue
        results.append(
            {
                "id": f"learned::{key}",
                "score": round(min(score, 1.0), 6),
                "domain": domain,
                "classification": classification,
                "summary": f"Learned correction pattern for {classification} in {domain}. Positive={e.get('positive_count', 0)}, Negative={e.get('negative_count', 0)}",
                "source": "feedback_learning",
            }
        )
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]
