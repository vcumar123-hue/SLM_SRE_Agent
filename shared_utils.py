from __future__ import annotations
import re
import traceback
from typing import Any, Dict, List, Optional, Set
import settings_portable as settings
from file_manager import append_debug_event


# ---------------------------------------------------------------------------
# Source priority constant (referenced by source_priority_value)
# ---------------------------------------------------------------------------

SOURCE_PRIORITY = {
    "curated_enterprise_library": 400,
    "canonical_enterprise_seed": 300,
    "promoted_from_fallback": 200,
    "feedback_learning": 175,
    "feedback_learning_db": 170,
    "synthetic_enterprise_baseline": 100,
    "incident": 90,
    "unknown": 50,
}


# ---------------------------------------------------------------------------
# Debug helpers
# ---------------------------------------------------------------------------

def debug_enabled(flag: bool) -> bool:
    return settings.DEBUG_MODE and flag


def pipeline_debug(event_type: str, payload: Dict[str, Any], request_id: Optional[str] = None) -> None:
    if not settings.DEBUG_MODE:
        return
    append_debug_event(
        settings.PIPELINE_DEBUG_FILE,
        event_type=event_type,
        payload=payload,
        request_id=request_id,
        component="app",
    )


def retrieval_debug(event_type: str, payload: Dict[str, Any], request_id: Optional[str] = None) -> None:
    if not debug_enabled(settings.DEBUG_RETRIEVAL):
        return
    append_debug_event(
        settings.RETRIEVAL_DEBUG_FILE,
        event_type=event_type,
        payload=payload,
        request_id=request_id,
        component="retrieval",
    )


def confidence_debug(event_type: str, payload: Dict[str, Any], request_id: Optional[str] = None) -> None:
    if not debug_enabled(settings.DEBUG_CONFIDENCE):
        return
    append_debug_event(
        settings.CONFIDENCE_DEBUG_FILE,
        event_type=event_type,
        payload=payload,
        request_id=request_id,
        component="confidence",
    )


def feedback_debug(event_type: str, payload: Dict[str, Any], request_id: Optional[str] = None) -> None:
    if not debug_enabled(settings.DEBUG_FEEDBACK):
        return
    append_debug_event(
        settings.FEEDBACK_DEBUG_FILE,
        event_type=event_type,
        payload=payload,
        request_id=request_id,
        component="feedback",
    )


def fallback_debug(event_type: str, payload: Dict[str, Any], request_id: Optional[str] = None) -> None:
    if not debug_enabled(settings.DEBUG_FALLBACK):
        return
    append_debug_event(
        settings.FALLBACK_DEBUG_FILE,
        event_type=event_type,
        payload=payload,
        request_id=request_id,
        component="fallback",
    )


def knowledge_debug(event_type: str, payload: Dict[str, Any], request_id: Optional[str] = None) -> None:
    if not settings.DEBUG_MODE:
        return
    append_debug_event(
        settings.PIPELINE_DEBUG_FILE,
        event_type=event_type,
        payload=payload,
        request_id=request_id,
        component="knowledge",
    )


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def dedupe_preserve_order(values: Optional[List[Any]]) -> List[str]:
    if not values:
        return []
    out: List[str] = []
    seen = set()
    for value in values:
        text = clean_text(str(value)) if value is not None else ""
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def normalize_phrase(text: str) -> str:
    text = clean_text(text).lower().replace("_", " ")
    replacements = {
        r"\bverify\b": "check",
        r"\binspect\b": "check",
        r"\bvalidate\b": "check",
        r"\bconfirm\b": "check",
        r"\bre[- ]?synchronize\b": "synchronize",
        r"\bre[- ]?distribute\b": "distribute",
        r"\bcorrect\b": "fix",
        r"\bretest\b": "test",
    }
    for pattern, repl in replacements.items():
        text = re.sub(pattern, repl, text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_classification_token(value: Optional[str]) -> str:
    return clean_text(value).lower().replace(" ", "_")


# ---------------------------------------------------------------------------
# Batch / mainframe signal detection
# ---------------------------------------------------------------------------

def is_mainframe_batch_abend_signal(ticket: str) -> bool:
    """Detect mainframe / TWS / TOC batch abend incidents that may not say "TWS job failed".

    Client ticket dumps often use mainframe batch language such as STCnnnnn,
    RC1/RC2, MF jobs, CMP abends, PJABP job names, and "fix or bypass".
    These are still batch execution failures and should not be downgraded to
    general_operational_issue or database_latency just because service/offering
    contains Database or Web Hosting.
    """
    raw = clean_text(ticket).lower()
    blob = normalize_phrase(ticket)

    failure_signal = any(term in blob for term in [
        "abend",
        "abended",
        "abending",
        "failed abend",
        "failed abended",
        "job failed",
        "jobs abending",
    ])
    if not failure_signal:
        return False

    scheduler_or_mainframe_signal = any(term in blob for term in [
        "tws",
        "mf job",
        "mf jobs",
        "mainframe",
        "mainframe database",
        "toc",
        "toc ris",
        "risreportingcommunicationoncall",
        "batch",
        "scheduler",
        "cmp",
        "jobstream",
        "job stream",
        "fix or bypass",
        "will prevent",
        "shutdown",
    ])

    regex_signal = bool(
        re.search(r"\bstc\d{3,}\b", raw)
        or re.search(r"\bpj[a-z0-9]{3,}\b", raw)
        or re.search(r"\brc1\s*/\s*rc2\b", raw)
        or re.search(r"\brc1\s*[:=]", raw)
        or re.search(r"\brc2\s*[:=]", raw)
        or re.search(r"\bgc\d{4,}\b", raw)
    )

    return bool(scheduler_or_mainframe_signal or regex_signal)

def is_batch_delay_signal(ticket: str) -> bool:
    blob = normalize_phrase(ticket)
    return any(term in blob for term in [
        "tws job late",
        "job late",
        "not started",
        "oppwait",
        "waiting on dataset",
        "waiting on datasets",
    ])

def is_batch_failure_signal(ticket: str) -> bool:
    blob = normalize_phrase(ticket)
    return (
        is_mainframe_batch_abend_signal(ticket)
        or any(term in blob for term in [
            "tws job failed",
            "tws job failed abended",
            "job failed abended",
            "failed abended",
            "failed abend",
            "abended",
            "abend",
        ])
    )


# ---------------------------------------------------------------------------
# Tokenization / similarity
# ---------------------------------------------------------------------------

def tokenize_signature(text: str) -> set[str]:
    norm = normalize_phrase(text)
    tokens = [t for t in norm.split() if len(t) > 2]
    stop = {
        "the", "and", "for", "with", "that", "this", "into", "from", "across",
        "all", "are", "was", "were", "but", "not", "only", "after", "before",
        "while", "ensure", "required", "services", "service", "system",
    }
    return {t for t in tokens if t not in stop}


def similarity_ratio(a: str, b: str) -> float:
    sa = tokenize_signature(a)
    sb = tokenize_signature(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


# ---------------------------------------------------------------------------
# Container / DNS / VPN signal detection constants and helpers
# ---------------------------------------------------------------------------

CONTAINER_CONTEXT_TERMS = [
    "container_connection_issue",
    "container",
    "containers",
    "containerized",
    "ecs",
    "ecs service",
    "ecs task",
    "container task",
    "kubernetes",
    "k8s",
    "pod",
    "pods",
    "service selector",
    "pod labels",
    "endpoint registration",
    "container port",
    "port mapping",
    "port mappings",
    "port binding",
    "host port mapping",
    "task networking",
    "target group",
    "target registration",
    "alb",
    "application load balancer",
    "load balancer",
    "ingress rule",
    "network policy",
    "service health checks",
    "health check path",
]

CONTAINER_FAILURE_TERMS = [
    "cannot connect",
    "can't connect",
    "cannot reach",
    "connection timeout",
    "connection timeouts",
    "client requests are timing out",
    "requests are timing out",
    "requests timeout",
    "timing out",
    "timeout",
    "no traffic",
    "traffic is not reaching",
    "does not route traffic",
    "not route traffic",
    "target group is unhealthy",
    "unhealthy targets",
    "target group health is failing",
    "target group shows unhealthy",
    "service endpoint",
    "service still does not route",
]

DNS_NEGATIVE_EVIDENCE_TERMS = [
    "dns resolves correctly",
    "dns resolution succeeds",
    "dns resolution is successful",
    "dns lookup is successful",
    "dns lookup successful",
    "dns is healthy",
    "dns returns the expected",
    "returns the expected load balancer",
    "dns resolves to the expected",
    "name resolution succeeds",
]

DNS_POSITIVE_FAILURE_TERMS = [
    "dns does not resolve",
    "dns resolution fails",
    "dns lookup fails",
    "cannot resolve",
    "can't resolve",
    "unable to resolve",
    "nxdomain",
    "servfail",
    "coredns failure",
    "pods cannot resolve",
    "cannot resolve service names",
    "name resolution failure",
]

VPN_NEGATIVE_EVIDENCE_TERMS = [
    "vpn connectivity is healthy",
    "vpn is healthy",
    "vpn tunnel is healthy",
    "remote access is healthy",
    "users can reach other internal services",
    "can reach other internal services",
    "issue is isolated to the container service",
    "isolated to the container service",
    "isolated to container service",
]


def text_has_any(text: str, terms: List[str]) -> bool:
    blob = normalize_phrase(text)
    raw = clean_text(text).lower()
    return any(term in blob or term in raw for term in terms)


def has_dns_negative_evidence(ticket: str) -> bool:
    return text_has_any(ticket, DNS_NEGATIVE_EVIDENCE_TERMS)


def has_dns_positive_failure_evidence(ticket: str) -> bool:
    return text_has_any(ticket, DNS_POSITIVE_FAILURE_TERMS)


def has_vpn_negative_evidence(ticket: str) -> bool:
    return text_has_any(ticket, VPN_NEGATIVE_EVIDENCE_TERMS)


def has_container_connectivity_signal(ticket: str) -> bool:
    blob = normalize_phrase(ticket)
    raw = clean_text(ticket).lower()
    explicit = "container_connection_issue" in raw or "container connection issue" in blob
    context = text_has_any(ticket, CONTAINER_CONTEXT_TERMS)
    failure = text_has_any(ticket, CONTAINER_FAILURE_TERMS)
    # Strong AWS/Kubernetes/load-balancer container vocabulary plus timeout/no-traffic
    # should override generic DNS/VPN/API-gateway labels, unless DNS itself is the
    # explicit failed dependency.
    if explicit:
        return True
    if context and failure and not has_dns_positive_failure_evidence(ticket):
        return True
    return False


# ---------------------------------------------------------------------------
# Incident item helpers
# ---------------------------------------------------------------------------

def item_blob(item: Dict[str, Any]) -> str:
    return normalize_phrase(
        f"{item.get('id','')} {item.get('title','')} {item.get('classification','')} "
        f"{item.get('summary','')} {item.get('content','')} {item.get('root_cause','')}"
    )


def item_is_container_relevant(item: Dict[str, Any]) -> bool:
    blob = item_blob(item)
    return any(term in blob for term in [
        "container", "containerized", "ecs", "kubernetes", "pod", "pods",
        "target group", "alb", "application load balancer", "load balancer",
        "port mapping", "port binding", "container port", "service selector",
        "endpoint registration", "task networking", "health check",
    ])


def item_contradicted_by_ticket(ticket: str, item: Dict[str, Any]) -> bool:
    blob = item_blob(item)
    cls = normalize_classification_token(item.get("classification"))

    if has_dns_negative_evidence(ticket) and ("dns" in blob or "dns" in cls):
        return True
    if has_vpn_negative_evidence(ticket) and ("vpn" in blob or "vpn" in cls):
        return True

    # If the ticket is unambiguously container connectivity, prevent weak learned
    # memories from unrelated VPN/DNS families from entering the candidate set.
    if has_container_connectivity_signal(ticket):
        source = clean_text(item.get("source", "")).lower()
        if ("vpn" in blob or "vpn" in cls) and source in {"feedback_learning", "feedback_learning_db"}:
            return True
        if has_dns_negative_evidence(ticket) and ("dns" in blob or "dns" in cls):
            return True
    return False


def filter_contradicted_incidents(ticket: str, incidents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [x for x in (incidents or []) if not item_contradicted_by_ticket(ticket, x)]


# ---------------------------------------------------------------------------
# Deduplication and section ordering
# ---------------------------------------------------------------------------

def dedupe_semanticish(values: Optional[List[Any]], threshold: float = 0.72) -> List[str]:
    if not values:
        return []
    candidates = [clean_text(str(v)) for v in values if clean_text(str(v))]
    kept: List[str] = []
    kept_norms: List[str] = []
    for text in candidates:
        norm = normalize_phrase(text)
        duplicate = False
        for existing_norm in kept_norms:
            if norm == existing_norm or norm in existing_norm or existing_norm in norm:
                duplicate = True
                break
            if similarity_ratio(norm, existing_norm) >= threshold:
                duplicate = True
                break
        if not duplicate:
            kept.append(text)
            kept_norms.append(norm)
    return kept


def prioritize_and_dedupe_sections(primary_items: List[str], secondary_items: List[str], tertiary_items: List[str], max_items: int) -> List[str]:
    ordered = []
    ordered.extend(primary_items or [])
    ordered.extend(secondary_items or [])
    ordered.extend(tertiary_items or [])
    return dedupe_semanticish(ordered)[:max_items]


# ---------------------------------------------------------------------------
# Bucket classification helpers
# ---------------------------------------------------------------------------

def is_diagnostic_phrase(text: str) -> bool:
    t = normalize_phrase(text)
    diagnostic_prefixes = (
        "check ",
        "validate ",
        "verify ",
        "inspect ",
        "confirm ",
        "compare ",
    )
    return t.startswith(diagnostic_prefixes)


def classify_check_bucket(text: str) -> str:
    t = normalize_phrase(text)
    if "key" in t and ("distribution" in t or "propagation" in t or "synchronize" in t):
        return "key_distribution"
    if "trust" in t or "key set" in t or "signing key set" in t:
        return "trust_validation"
    if "rbac" in t or "policy" in t or "role" in t:
        return "rbac_policy"
    if "token" in t or "claim" in t or "compare" in t:
        return "token_validation"
    if "rotation" in t or "window" in t or "old keys" in t:
        return "rotation_window"
    if "log" in t:
        return "logging"
    return "generic"


def classify_action_bucket(text: str) -> str:
    t = normalize_phrase(text)
    if is_diagnostic_phrase(t):
        return "diagnostic_check"
    if "sync" in t or "distribute" in t or "signing key" in t:
        return "key_sync"
    if "rbac" in t or "policy" in t or "role" in t or "mapping" in t:
        return "policy_fix"
    if "config" in t or "configuration" in t:
        return "config_fix"
    if "test" in t or "revalidate" in t:
        return "validation_retest"
    if "monitor" in t:
        return "monitoring"
    return "generic"


def bucket_dedupe(items: List[str], classifier_fn, max_items: int) -> List[str]:
    bucket_map: Dict[str, str] = {}
    ordered_buckets: List[str] = []
    for item in items:
        clean_item = clean_text(item)
        if not clean_item:
            continue
        bucket = classifier_fn(clean_item)
        if bucket not in bucket_map:
            bucket_map[bucket] = clean_item
            ordered_buckets.append(bucket)
        else:
            if len(clean_item) > len(bucket_map[bucket]):
                bucket_map[bucket] = clean_item
    result = [bucket_map[b] for b in ordered_buckets]
    return result[:max_items]


def source_priority_value(source) -> int:
    return SOURCE_PRIORITY.get(clean_text(source).lower(), 0)
