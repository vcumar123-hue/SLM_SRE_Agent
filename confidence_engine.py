from __future__ import annotations

from typing import Any, Dict, List


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def compute_embedding_score(top_matches: List[Dict[str, Any]]) -> float:
    if not top_matches:
        return 0.0
    return clamp01(top_matches[0].get("score", 0.0))


def compute_retrieval_score(top_matches: List[Dict[str, Any]]) -> float:
    if not top_matches:
        return 0.0
    scores = [clamp01(m.get("score", 0.0)) for m in top_matches[:3]]
    s1 = scores[0]
    avg = sum(scores) / len(scores)
    domains = [_norm(m.get("domain")) for m in top_matches[:3]]
    if len(set(domains)) == 1:
        diversity = 0.90
    elif len(set(domains)) == 2:
        diversity = 0.65
    else:
        diversity = 0.35
    return clamp01(0.50 * s1 + 0.30 * avg + 0.20 * diversity)


def compute_classification_score(
    routed_domain: str,
    llm_domain: str,
    top_matches: List[Dict[str, Any]],
) -> float:
    routed_domain = _norm(routed_domain)
    llm_domain = _norm(llm_domain)

    if routed_domain == llm_domain and routed_domain:
        domain_match = 0.90
    elif llm_domain and routed_domain and (llm_domain in routed_domain or routed_domain in llm_domain):
        domain_match = 0.70
    else:
        domain_match = 0.40

    domains = [_norm(m.get("domain")) for m in top_matches[:3]]
    taxonomy_match = domains.count(routed_domain) / len(domains) if domains else 0.0
    keyword_signal = 0.75 if top_matches else 0.30
    return clamp01(0.40 * domain_match + 0.40 * taxonomy_match + 0.20 * keyword_signal)


def compute_llm_score(llm_confidence: float) -> float:
    if llm_confidence is None:
        return 0.50
    return clamp01(llm_confidence)


def compute_source_alignment(
    top_matches: List[Dict[str, Any]],
    *,
    tenant_id: str = "client_a",
) -> float:
    if not top_matches:
        return 0.0

    tenant_key = _norm(tenant_id)
    trusted_source = {
        "client_private_knowledge": 1.00,
        "global_learned_patterns": 0.92,
        "promoted_from_fallback": 0.88,
        "kb": 0.84,
        "feedback_learning": 0.78,
        "feedback_learning_db": 0.76,
        "incident": 0.74,
        "ticket": 0.66,
        "topology": 0.72,
        "document": 0.70,
    }
    trusted_collection = {
        "global_base_index": 0.80,
        "global_learned_patterns_index": 0.92,
    }

    weighted = []
    for m in top_matches[:3]:
        source_layer = _norm(m.get("source_layer"))
        source = _norm(m.get("source") or m.get("source_type"))
        collection_name = _norm(m.get("collection_name"))
        match_tenant = _norm(m.get("tenant_id"))

        score = 0.60
        if source_layer:
            score = max(score, trusted_source.get(source_layer, score))
        if source:
            score = max(score, trusted_source.get(source, score))
        if collection_name:
            score = max(score, trusted_collection.get(collection_name, score))
            if collection_name == f"{tenant_key}_index":
                score = max(score, 1.00)
        if match_tenant and tenant_key and match_tenant == tenant_key:
            score = min(1.0, score + 0.05)

        weighted.append(score)

    return clamp01(sum(weighted) / len(weighted))


def compute_staleness_score(top_matches: List[Dict[str, Any]]) -> float:
    if not top_matches:
        return 0.50
    penalties = [float(m.get("staleness_penalty", 0.0) or 0.0) for m in top_matches[:3]]
    avg_penalty = sum(penalties) / len(penalties)
    return clamp01(1.0 - min(avg_penalty / 0.12, 1.0))


def compute_collection_priority_score(
    top_matches: List[Dict[str, Any]],
    *,
    tenant_id: str = "client_a",
) -> float:
    if not top_matches:
        return 0.0

    tenant_key = _norm(tenant_id)
    weights = []
    for m in top_matches[:3]:
        collection = _norm(m.get("collection_name"))
        if collection == f"{tenant_key}_index":
            weights.append(1.00)
        elif collection == "global_learned_patterns_index":
            weights.append(0.90)
        elif collection == "global_base_index":
            weights.append(0.76)
        elif collection:
            weights.append(0.68)
        else:
            weights.append(0.55)
    return clamp01(sum(weights) / len(weights))


def compute_final_confidence(
    embedding_score: float,
    llm_score: float,
    retrieval_score: float,
    classification_score: float,
    learned_feedback_score: float = 0.0,
    source_alignment: float = 0.5,
    staleness_score: float = 0.5,
    collection_priority_score: float = 0.6,
) -> float:
    confidence = (
        0.18 * embedding_score +
        0.10 * llm_score +
        0.18 * retrieval_score +
        0.17 * classification_score +
        0.10 * clamp01(learned_feedback_score) +
        0.12 * source_alignment +
        0.07 * staleness_score +
        0.08 * collection_priority_score
    )
    return clamp01(confidence)


def decide_action(confidence: float) -> Dict[str, Any]:
    if confidence >= 0.84:
        decision = "local_answer"
        label = "High"
        fallback = False
    elif confidence >= 0.68:
        decision = "local_answer_with_warning"
        label = "Medium"
        fallback = False
    elif confidence >= 0.48:
        decision = "fallback_recommended"
        label = "Low"
        fallback = True
    else:
        decision = "fallback_required"
        label = "Very Low"
        fallback = True
    return {"decision": decision, "confidence_label": label, "fallback_required": fallback}


def compute_confidence(
    top_matches: List[Dict[str, Any]],
    routed_domain: str,
    llm_domain: str,
    llm_confidence: float,
    *,
    tenant_id: str = "client_a",
) -> Dict[str, Any]:
    embedding_score = compute_embedding_score(top_matches)
    retrieval_score = compute_retrieval_score(top_matches)
    classification_score = compute_classification_score(routed_domain, llm_domain, top_matches)
    llm_score = compute_llm_score(llm_confidence)
    source_alignment = compute_source_alignment(top_matches, tenant_id=tenant_id)
    staleness_score = compute_staleness_score(top_matches)
    collection_priority_score = compute_collection_priority_score(top_matches, tenant_id=tenant_id)
    final_confidence = compute_final_confidence(
        embedding_score,
        llm_score,
        retrieval_score,
        classification_score,
        learned_feedback_score=0.0,
        source_alignment=source_alignment,
        staleness_score=staleness_score,
        collection_priority_score=collection_priority_score,
    )
    decision_data = decide_action(final_confidence)
    return {
        "embedding_score": round(embedding_score, 3),
        "llm_score": round(llm_score, 3),
        "retrieval_score": round(retrieval_score, 3),
        "classification_score": round(classification_score, 3),
        "source_alignment": round(source_alignment, 3),
        "staleness_score": round(staleness_score, 3),
        "collection_priority_score": round(collection_priority_score, 3),
        "final_confidence": round(final_confidence, 3),
        **decision_data,
    }


# Release 2 (design 10.3): the optional verified-investigation input. Its weight is a constant that may only be set from calibration evidence; until then it is 0.0,
# so supplying a grounding value is recorded in the result but changes no confidence. Without a value the function behaves exactly as it always has.
EVIDENCE_GROUNDING_WEIGHT = 0.0


def apply_evidence_grounding(primary_confidence: float, evidence_grounding: Any, weight: float = None) -> float:
    """Move the confidence toward the grounding by `weight` (0 = unchanged). None, a non-number or an out-of-range value leaves it unchanged."""
    w = EVIDENCE_GROUNDING_WEIGHT if weight is None else weight
    try:
        g = float(evidence_grounding)
    except (TypeError, ValueError):
        return primary_confidence
    if w <= 0.0 or not 0.0 <= g <= 1.0:
        return primary_confidence
    return clamp01(primary_confidence + clamp01(w) * (g - primary_confidence))


def compute_confidence_score(
    embedding_score: float,
    llm_score: float,
    retrieval_quality: float,
    classification_certainty: float,
    learned_feedback_score: float = 0.0,
    source_alignment: float = 0.50,
    staleness_score: float = 0.70,
    collection_priority_score: float = 0.75,
    evidence_grounding: Any = None,
) -> Dict[str, Any]:
    """
    app.py-compatible entry point.

    v2 additions:
    - collection_priority_score
    - richer source alignment support
    """
    primary_confidence = compute_final_confidence(
        embedding_score=clamp01(embedding_score),
        llm_score=clamp01(llm_score),
        retrieval_score=clamp01(retrieval_quality),
        classification_score=clamp01(classification_certainty),
        learned_feedback_score=clamp01(learned_feedback_score),
        source_alignment=clamp01(source_alignment),
        staleness_score=clamp01(staleness_score),
        collection_priority_score=clamp01(collection_priority_score),
    )
    # Zero retrieval quality = no KB matches at all — keyword-only classification.
    # Cap confidence at 0.35 so the UI never shows a misleadingly high score.
    if retrieval_quality == 0.0:
        primary_confidence = min(primary_confidence, 0.35)
    if evidence_grounding is not None:
        primary_confidence = apply_evidence_grounding(primary_confidence, evidence_grounding)
    decision = decide_action(primary_confidence)
    extra = {"evidence_grounding": evidence_grounding} if evidence_grounding is not None else {}
    return {
        **extra,
        "embedding_score": round(clamp01(embedding_score), 4),
        "llm_score": round(clamp01(llm_score), 4),
        "retrieval_quality": round(clamp01(retrieval_quality), 4),
        "classification_certainty": round(clamp01(classification_certainty), 4),
        "learned_feedback_score": round(clamp01(learned_feedback_score), 4),
        "source_alignment": round(clamp01(source_alignment), 4),
        "staleness_score": round(clamp01(staleness_score), 4),
        "collection_priority_score": round(clamp01(collection_priority_score), 4),
        "primary_confidence": round(primary_confidence, 4),
        **decision,
    }
