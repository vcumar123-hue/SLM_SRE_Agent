"""
PlaybookAgent — generates structured Step 1..N resolution playbooks via LLM.

Priority order when get_resolution_playbook() is called:
  1. Hardcoded RESOLUTION_PLAYBOOKS dict (instant)
  2. Classification-keyed cache in data/generated_playbooks.json (instant after first call)
  3. This agent — LLM call, result stored to cache for all future calls

The agent is called synchronously from get_resolution_playbook() on a cache miss.
The cache is classification-scoped (same steps for all incidents of same type).
"""
from __future__ import annotations

import json
import os
import time
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Cache — PostgreSQL primary, JSON file fallback ────────────────────────────
_CACHE_PATH = Path("data/generated_playbooks.json")
_mem_cache: Dict[str, List[Dict[str, Any]]] = {}

try:
    from postgres_store import get_generated_playbook_pg, upsert_generated_playbook
    _PG_AVAILABLE = True
except Exception:
    get_generated_playbook_pg = None  # type: ignore[assignment]
    upsert_generated_playbook = None  # type: ignore[assignment]
    _PG_AVAILABLE = False


def _json_cache_load() -> Dict[str, List[Dict[str, Any]]]:
    try:
        if _CACHE_PATH.exists():
            raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except Exception:
        pass
    return {}


def _json_cache_write(classification: str, steps: List[Dict[str, Any]]) -> None:
    try:
        existing = _json_cache_load()
        existing[classification] = steps
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("PlaybookAgent: JSON cache write failed: %s", exc)


def _current_tenant_id() -> str:
    """Resolve the active tenant for playbook cache scoping."""
    try:
        from tenant_context import get_tenant
        tid = get_tenant()
        return tid if tid else "default"
    except Exception:
        return "default"


def get_cached_playbook(classification: str) -> Optional[List[Dict[str, Any]]]:
    tenant_id = _current_tenant_id()
    cache_key = f"{tenant_id}:{classification}"
    # 1. In-process memory cache (fastest)
    if cache_key in _mem_cache:
        return _mem_cache[cache_key]
    # 2. PostgreSQL — scoped by tenant
    if _PG_AVAILABLE and get_generated_playbook_pg is not None:
        try:
            steps = get_generated_playbook_pg(classification, tenant_id=tenant_id)
            if steps is not None:
                _mem_cache[cache_key] = steps
                return steps
        except Exception as exc:
            logger.warning("PlaybookAgent: PG cache read failed: %s", exc)
    # 3. JSON file fallback (not tenant-scoped — dev/offline use only)
    data = _json_cache_load()
    steps = data.get(classification)
    if steps is not None:
        _mem_cache[cache_key] = steps
    return steps


def set_cached_playbook(classification: str, steps: List[Dict[str, Any]], model_used: str = "") -> None:
    tenant_id = _current_tenant_id()
    cache_key = f"{tenant_id}:{classification}"
    _mem_cache[cache_key] = steps
    # Write to PostgreSQL (primary) — scoped by tenant
    if _PG_AVAILABLE and upsert_generated_playbook is not None:
        try:
            upsert_generated_playbook(classification, steps, model_used=model_used, tenant_id=tenant_id)
        except Exception as exc:
            logger.warning("PlaybookAgent: PG cache write failed: %s", exc)
    # Write to JSON (fallback, for environments without DB)
    _json_cache_write(classification, steps)


# ── LLM call ─────────────────────────────────────────────────────────────────
_STEP_SCHEMA = """{
  "step": <integer>,
  "title": "<short imperative title>",
  "context": "<one sentence — why this step matters at this point>",
  "action": "<precise action the engineer takes>",
  "commands": ["<tool or command>", ...],
  "decision": [["<if condition>", "<then action>"], ...],
  "caution": "<optional — only if a dangerous mistake is possible, else omit>"
}"""

_SYSTEM_PROMPT = """\
You are a senior IT operations engineer writing a structured resolution runbook.
Your output is a JSON array of step objects. Each object follows this exact schema:

{schema}

Rules:
- 3 to 7 steps only. No padding steps.
- Each step must have: step, title, context, action, commands, decision.
- caution is optional — only include if there is a real risk of data loss or service disruption.
- commands: list the actual tool, transaction code, CLI command, or system the engineer uses.
- decision: list of [condition, consequence] pairs — what the engineer does based on what they find.
- Do NOT include any text outside the JSON array. No preamble, no commentary.
- Output must be valid JSON that parses directly with json.loads().
""".format(schema=_STEP_SCHEMA)

_USER_TEMPLATE = """\
Incident classification: {classification}
Incident description: {ticket}

{rag_context}

{bias_context}
Write a step-by-step resolution runbook for this incident type.
Return ONLY the JSON array. No other text.
"""


def _build_bias_context(classification: str) -> str:
    """Pull long-term memory step success rates and inject into the LLM prompt."""
    try:
        from long_term_memory import get_step_bias, get_resolution_summary
        summary = get_resolution_summary(classification)
        bias = get_step_bias(classification)
        parts = []
        if summary:
            parts.append(f"Historical context: {summary}.")
        if bias:
            high = [s for s, r in sorted(bias.items(), key=lambda x: -x[1]) if r >= 0.7]
            low  = [s for s, r in sorted(bias.items(), key=lambda x: x[1])  if r < 0.4]
            if high:
                parts.append(f"Steps with high historical success: {', '.join(high[:3])}.")
            if low:
                parts.append(f"Steps to reconsider or improve: {', '.join(low[:2])}.")
        return ("\n".join(parts) + "\n") if parts else ""
    except Exception:
        return ""


def _parse_steps(raw_content: str) -> Optional[List[Dict[str, Any]]]:
    """Parse a model reply into validated playbook steps (tolerates a markdown code fence)."""
    raw_content = (raw_content or "").strip()
    if raw_content.startswith("```"):
        raw_content = raw_content.split("```")[1]
        if raw_content.startswith("json"):
            raw_content = raw_content[4:]
    steps = json.loads(raw_content)
    if not isinstance(steps, list):
        raise ValueError("LLM returned non-list")
    valid = [s for s in steps if isinstance(s, dict) and s.get("step") and s.get("title") and s.get("action")]
    return valid or None


def _build_user_message(classification: str, ticket: str, rag_context: str) -> str:
    return _USER_TEMPLATE.format(
        classification=classification,
        ticket=(ticket or "")[:800],
        rag_context=f"Relevant context from knowledge base:\n{rag_context}" if rag_context else "",
        bias_context=_build_bias_context(classification),
    )


def _call_local_llm(classification: str, ticket: str, rag_context: str, tenant_id: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """Generate playbook steps with the local model (LM Studio / Ollama), so no ticket text leaves the machine.

    Used when the external provider is not configured or not permitted. If the configured model endpoint is not local,
    the tenant's egress policy applies exactly as it does for the external provider.
    """
    import settings_portable as settings
    base_url = str(getattr(settings, "MODEL_BASE_URL", "") or "")
    model = str(getattr(settings, "PRIMARY_CHAT_MODEL", "") or "")
    if not base_url or not model:
        return None
    try:
        import requests
    except ImportError:
        return None

    from egress_policy import EgressDenied, provider_for_url, redact, require_external_llm
    user_msg = _build_user_message(classification, ticket, rag_context)
    _provider, is_local = provider_for_url(base_url)
    if not is_local:
        try:
            decision = require_external_llm(tenant_id, "playbook_generation", base_url)
        except EgressDenied as denied:
            logger.warning("PlaybookAgent: model endpoint is not local and egress is denied for '%s': %s", classification, denied)
            return None
        user_msg = redact(user_msg, decision.redaction)

    payload = {
        "model": model,
        "messages": [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user_msg}],
        "temperature": 0.2,
        "max_tokens": 2048,
    }
    headers = {"Authorization": f"Bearer {getattr(settings, 'MODEL_API_KEY', 'lm-studio')}", "Content-Type": "application/json"}
    try:
        t0 = time.time()
        resp = requests.post(f"{base_url.rstrip('/')}/chat/completions", headers=headers, json=payload,
                             timeout=float(os.getenv("AIOPS_LOCAL_PLAYBOOK_TIMEOUT_S", "180")))
        resp.raise_for_status()
        steps = _parse_steps(resp.json()["choices"][0]["message"]["content"])
        if steps:
            for step in steps:
                step.setdefault("generated_by", model)
        logger.info("PlaybookAgent: local model %s generated %d steps for '%s' in %.1fs",
                    model, len(steps or []), classification, time.time() - t0)
        return steps
    except Exception as exc:  # noqa: BLE001
        logger.warning("PlaybookAgent: local model call failed for '%s': %s", classification, exc)
        return None


def _call_llm(classification: str, ticket: str, rag_context: str, tenant_id: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """Call Groq (or compatible OpenAI endpoint) to generate playbook steps."""
    api_key = os.getenv("GROQ_API_KEY", "")
    base_url = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

    if not api_key:
        logger.warning("PlaybookAgent: GROQ_API_KEY not set — cannot generate playbook")
        return None

    try:
        import requests  # already in requirements
    except ImportError:
        logger.error("PlaybookAgent: requests not available")
        return None

    # Release 1 (W5): deny-by-default tenant policy for every external model call.
    from egress_policy import EgressDenied, require_external_llm, redact
    try:
        _egress = require_external_llm(tenant_id, "playbook_generation", base_url)
    except EgressDenied as denied:
        logger.warning("PlaybookAgent: external LLM call denied for '%s': %s", classification, denied)
        return None

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    user_msg = _USER_TEMPLATE.format(
        classification=classification,
        ticket=(ticket or "")[:800],
        rag_context=f"Relevant context from knowledge base:\n{rag_context}" if rag_context else "",
        bias_context=_build_bias_context(classification),
    )
    user_msg = redact(user_msg, _egress.redaction)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.2,
        "max_tokens": 2048,
    }

    try:
        t0 = time.time()
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        elapsed = round(time.time() - t0, 2)
        resp.raise_for_status()
        raw_content = resp.json()["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if present
        if raw_content.startswith("```"):
            raw_content = raw_content.split("```")[1]
            if raw_content.startswith("json"):
                raw_content = raw_content[4:]
        steps = json.loads(raw_content)
        if not isinstance(steps, list):
            raise ValueError("LLM returned non-list")
        # Validate required fields
        valid = []
        for s in steps:
            if isinstance(s, dict) and s.get("step") and s.get("title") and s.get("action"):
                valid.append(s)
        logger.info("PlaybookAgent: generated %d steps for '%s' in %.2fs", len(valid), classification, elapsed)
        return valid if valid else None
    except Exception as exc:
        logger.warning("PlaybookAgent: LLM call failed for '%s': %s", classification, exc)
        return None


# ── Public entry point ────────────────────────────────────────────────────────
def generate_playbook(
    classification: str,
    ticket: str = "",
    rag_context: str = "",
    allow_llm: bool = True,
    tenant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Return structured playbook steps for classification.
    Checks cache first; calls LLM on miss and caches the result.
    Returns [] if LLM is unavailable or fails.
    """
    if not classification:
        return []

    cached = get_cached_playbook(classification)
    if cached is not None:
        logger.debug("PlaybookAgent: cache hit for '%s' (%d steps)", classification, len(cached))
        return cached

    steps = None
    model_used = ""
    if allow_llm:
        logger.info("PlaybookAgent: cache miss for '%s' - calling the external model", classification)
        steps = _call_llm(classification, ticket, rag_context, tenant_id=tenant_id)
        model_used = os.getenv("GROQ_MODEL", "")
    if not steps:
        # Not configured, not permitted for this request or tenant, or it failed: the local model keeps data on the machine.
        logger.info("PlaybookAgent: cache miss for '%s' - using the local model", classification)
        steps = _call_local_llm(classification, ticket, rag_context, tenant_id=tenant_id)
        if steps:
            import settings_portable as _settings
            model_used = str(getattr(_settings, "PRIMARY_CHAT_MODEL", "local"))
    if steps:
        set_cached_playbook(classification, steps, model_used=model_used)
        return steps
    return []
