import json
import re
from typing import Any
import os

import requests


def _get_env_value(name: str, default: str = "") -> str:
    return os.getenv(name, default)


GROQ_API_KEY = _get_env_value("GROQ_API_KEY", "")
GROQ_MODEL = _get_env_value("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_BASE_URL = _get_env_value("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

FALLBACK_ENABLED = _get_env_value("FALLBACK_ENABLED", "").lower() in {"1", "true", "yes", "on"}
ENABLE_GROQ_FALLBACK = _get_env_value("ENABLE_GROQ_FALLBACK", "").lower() in {"1", "true", "yes", "on"}
FALLBACK_ACTIVE = FALLBACK_ENABLED or ENABLE_GROQ_FALLBACK or True

GROQ_TIMEOUT_SECONDS = int(_get_env_value("GROQ_TIMEOUT_SECONDS", "60"))
CONFIDENCE_THRESHOLD = float(_get_env_value("CONFIDENCE_THRESHOLD", "0.40"))


def should_trigger_fallback(confidence_result: dict[str, Any]) -> bool:
    if not FALLBACK_ACTIVE:
        return False
    score = confidence_result.get("overall_confidence", confidence_result.get("final_confidence", 0.0))
    return float(score or 0.0) < CONFIDENCE_THRESHOLD


def _is_vague_ticket(ticket: str) -> bool:
    """True when a ticket has no specific symptoms — too few words or only product names."""
    import re as _re
    words = [w for w in _re.sub(r"[^a-z0-9\s]", " ", ticket.lower()).split() if len(w) > 2]
    symptom_signals = {
        "error", "fail", "failed", "failure", "crash", "down", "unreachable", "timeout",
        "slow", "latency", "spike", "high", "low", "memory", "cpu", "disk", "full",
        "hung", "hang", "stuck", "loop", "exception", "traceback", "denied", "refused",
        "cannot", "unable", "connect", "connection", "drop", "packet", "lost", "missing",
        "alert", "alarm", "warning", "critical", "outage", "unavailable", "response",
    }
    has_symptom = any(w in symptom_signals for w in words)
    return len(words) < 6 and not has_symptom


def _build_prompt(ticket: str, context: str) -> str:
    vague = _is_vague_ticket(ticket)
    if vague:
        vague_instruction = """
IMPORTANT: This ticket is too vague to determine a specific root cause.
- You MUST use a generic symptom-level classification (e.g. "application_error", "service_degradation", "middleware_issue").
- Do NOT invent a specific root cause (disk space, memory leak, certificate, etc.) that is not mentioned in the ticket.
- Set confidence to 0.2 or below and escalate=true.
- Ask the operator for more details in next_checks.
"""
    else:
        vague_instruction = ""

    return f"""
You are a senior IT operations fallback analyst. Your job is to produce a precise, technically useful incident triage response.
{vague_instruction}
Ticket:
{ticket}

Retrieved context:
{context}

Return ONLY valid JSON using this exact schema:
{{
  "classification": "",
  "probable_causes": [],
  "next_checks": [],
  "recommended_actions": [],
  "confidence": 0.0,
  "escalate": false
}}

Strict rules:
1. Output JSON only. No markdown. No prose outside JSON.
2. classification must be:
   - lowercase
   - snake_case
   - derived from symptoms explicitly stated in the ticket
   - if no specific symptoms are stated, use a generic symptom-level label (e.g. "application_error", "middleware_service_issue")
3. Do NOT invent a root cause (disk_space, memory_leak, certificate, etc.) unless the ticket explicitly mentions those symptoms.
4. Avoid hallucinating causes that are not grounded in the ticket text or retrieved context.
5. probable_causes must be short technical causes, not broad business language.
6. next_checks must be operationally ordered and validation-first.
   Start with inspection and validation before disruptive steps.
7. recommended_actions must also be operationally ordered.
   Prefer: validate, inspect, compare, confirm, isolate — before restart, rollback, redeploy.
8. Do not recommend restart or rollback as the first action unless the ticket strongly indicates it.
9. confidence must be between 0 and 1. Use low values (0.1–0.3) when the ticket is vague.
10. Set escalate=true when evidence is weak, ambiguous, or the issue may require human investigation.
11. Use at most: 1 classification, 2–4 probable_causes, 3–5 next_checks, 3–5 recommended_actions.

Now return the JSON only.
""".strip()


def _clean_json_text(text: str) -> str:
    text = (text or "").strip()
    # Strip reasoning/thinking blocks emitted by chain-of-thought models
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    text = text.replace("```json", "").replace("```", "").strip()

    # Find the LAST top-level JSON object (after any preamble text)
    # Walk backwards: find last '}' then scan left for its matching '{'
    last_close = text.rfind("}")
    if last_close != -1:
        depth = 0
        for i in range(last_close, -1, -1):
            if text[i] == "}":
                depth += 1
            elif text[i] == "{":
                depth -= 1
                if depth == 0:
                    text = text[i:last_close + 1]
                    break

    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text


def _specificity_upgrade(label: str) -> str:
    label = str(label or "").strip().lower()

    weak_labels = {
        "service_issue",
        "general_issue",
        "unknown_issue",
        "application_problem",
        "fallback_generated",
    }

    if label in weak_labels or not label:
        return "ambiguous_post_change_service_issue"

    return label


def _dedupe_keep_order(items: list[Any]) -> list[str]:
    seen = set()
    out = []
    for item in items or []:
        value = str(item).strip().lower()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _reorder_actions(actions: list[str]) -> list[str]:
    """
    Push validation / inspection actions ahead of restart / rollback style actions.
    """
    actions = _dedupe_keep_order(actions)

    validation_prefixes = (
        "verify_",
        "validate_",
        "inspect_",
        "check_",
        "compare_",
        "confirm_",
        "review_",
        "trace_",
        "analyze_",
        "isolate_",
    )
    disruptive_prefixes = (
        "restart_",
        "rollback_",
        "redeploy_",
        "recreate_",
        "failover_",
    )

    validate_first = []
    neutral = []
    disruptive = []

    for action in actions:
        if action.startswith(validation_prefixes):
            validate_first.append(action)
        elif action.startswith(disruptive_prefixes):
            disruptive.append(action)
        else:
            neutral.append(action)

    return validate_first + neutral + disruptive


def _normalize_response(parsed: dict[str, Any]) -> dict[str, Any]:
    parsed = dict(parsed or {})

    parsed["classification"] = _specificity_upgrade(parsed.get("classification", ""))

    parsed["probable_causes"] = _dedupe_keep_order(parsed.get("probable_causes", []))[:4]
    parsed["next_checks"] = _reorder_actions(parsed.get("next_checks", []))[:5]
    parsed["recommended_actions"] = _reorder_actions(parsed.get("recommended_actions", []))[:5]
    # If LLM returned no actions, derive from next_checks so the KB is never empty.
    if not parsed["recommended_actions"] and parsed["next_checks"]:
        parsed["recommended_actions"] = parsed["next_checks"][:3]

    parsed.setdefault("confidence", 0.75)
    parsed.setdefault("escalate", True)

    parsed["fallback_source"] = "groq_chat_completions"
    parsed["fallback_model"] = GROQ_MODEL
    parsed["fallback_provider"] = "groq"

    return parsed


def run_openai_fallback(ticket: str, context: str) -> dict[str, Any]:
    # Re-read at call time so a stale import never locks in a bad model name
    _model = os.getenv("GROQ_MODEL", GROQ_MODEL) or GROQ_MODEL
    _api_key = os.getenv("GROQ_API_KEY", GROQ_API_KEY) or GROQ_API_KEY
    _base_url = os.getenv("GROQ_BASE_URL", GROQ_BASE_URL) or GROQ_BASE_URL

    if not FALLBACK_ACTIVE:
        return {
            "classification": "fallback_disabled",
            "probable_causes": ["fallback_disabled_by_configuration"],
            "next_checks": [],
            "recommended_actions": [],
            "confidence": 0.2,
            "escalate": True,
            "fallback_source": "groq_chat_completions",
            "fallback_model": _model,
            "fallback_provider": "groq",
        }

    if not _api_key:
        return {
            "classification": "fallback_unavailable",
            "probable_causes": ["groq_api_key_not_configured"],
            "next_checks": [],
            "recommended_actions": [],
            "confidence": 0.2,
            "escalate": True,
            "fallback_source": "groq_chat_completions",
            "fallback_model": _model,
            "fallback_provider": "groq",
        }

    url = f"{_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {_api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": _model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a senior IT operations engineer. "
                    "Return ONLY valid JSON. "
                    "Prefer specific root-cause-oriented classifications and validation-first operational guidance."
                ),
            },
            {
                "role": "user",
                "content": _build_prompt(ticket, context),
            },
        ],
        "temperature": 0,
        "max_tokens": 1500,
    }

    try:
        r = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=GROQ_TIMEOUT_SECONDS,
        )
        r.raise_for_status()
    except requests.exceptions.ConnectionError as _ce:
        return {
            "classification": "fallback_connection_error",
            "probable_causes": [f"groq_api_connection_failed: {_ce}"],
            "next_checks": ["verify_groq_api_reachability", "check_network_connectivity"],
            "recommended_actions": ["retry_analysis", "check_groq_api_status"],
            "confidence": 0.1,
            "escalate": True,
            "fallback_source": "groq_chat_completions",
            "fallback_model": _model,
            "fallback_provider": "groq",
        }
    except requests.exceptions.Timeout:
        return {
            "classification": "fallback_timeout",
            "probable_causes": ["groq_api_response_timeout"],
            "next_checks": ["check_groq_api_latency"],
            "recommended_actions": ["retry_analysis"],
            "confidence": 0.1,
            "escalate": True,
            "fallback_source": "groq_chat_completions",
            "fallback_model": _model,
            "fallback_provider": "groq",
        }
    except requests.exceptions.HTTPError as _he:
        return {
            "classification": "fallback_api_error",
            "probable_causes": [f"groq_api_http_error: {_he}"],
            "next_checks": ["check_groq_api_key", "verify_model_availability"],
            "recommended_actions": ["check_groq_quota", "retry_analysis"],
            "confidence": 0.1,
            "escalate": True,
            "fallback_source": "groq_chat_completions",
            "fallback_model": _model,
            "fallback_provider": "groq",
        }

    body = r.json()
    # Some reasoning models put content in reasoning_content; fall back gracefully
    msg = body.get("choices", [{}])[0].get("message", {})
    raw_text = msg.get("content") or msg.get("reasoning_content") or ""
    if not raw_text:
        print(f"[groq_fallback] WARNING: empty content in response body: {body}", flush=True)
    cleaned = _clean_json_text(raw_text)

    try:
        parsed = json.loads(cleaned)
        return _normalize_response(parsed)
    except Exception as _e:
        print(f"[groq_fallback] JSON parse failed: {_e}\nraw_text={raw_text[:500]!r}\ncleaned={cleaned[:500]!r}", flush=True)
        return {
            "classification": "ambiguous_post_change_service_issue",
            "probable_causes": ["fallback_non_json_response"],
            "next_checks": ["inspect_fallback_raw_output"],
            "recommended_actions": ["review_fallback_prompt_output"],
            "confidence": 0.5,
            "escalate": True,
            "fallback_source": "groq_chat_completions",
            "fallback_model": _model,
            "fallback_provider": "groq",
        }


def build_context_from_incidents(incidents: list[dict]) -> str:
    """Build a grounding context string from retrieved incidents for the LLM prompt."""
    if not incidents:
        return ""
    lines = []
    for i, inc in enumerate(incidents[:3], 1):
        cls = inc.get("classification", "unknown").replace("_", " ")
        content = (inc.get("content") or inc.get("title") or "")[:400]
        score = inc.get("score", 0.0)
        lines.append(
            f"Reference incident {i} (classification: {cls}, similarity: {score:.2f}):\n{content}"
        )
    return "\n\n".join(lines)


def analyze_with_groq_fallback(ticket: str, context_incidents: list[dict] | None = None) -> dict:
    context = build_context_from_incidents(context_incidents or [])
    result = run_openai_fallback(ticket, context)
    if context_incidents:
        result["fallback_grounded"] = True
        result["grounding_incident_count"] = len(context_incidents[:3])
    return result