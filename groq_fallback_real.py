import json
import re
from typing import Any
import os

import requests


def _get_env_value(name: str, default: str = "") -> str:
    return os.getenv(name, default)


GROQ_API_KEY = _get_env_value("GROQ_API_KEY", "")
GROQ_MODEL = _get_env_value("GROQ_MODEL", "llama-3.3-70b-versatile")
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


def _build_prompt(ticket: str, context: str) -> str:
    return f"""
You are a senior IT operations fallback analyst. Your job is to produce a precise, technically useful incident triage response.

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
   - as specific as possible
   - technical and root-cause-oriented
   - derived from the ticket wording when possible
3. Avoid vague classifications such as:
   - service_issue
   - general_issue
   - unknown_issue
   - application_problem
4. Prefer specific classifications like:
   - certificate_rotation_policy_conflict
   - service_mesh_tls_misconfiguration
   - post_change_certificate_validation_failure
   - sidecar_policy_enforcement_issue
5. probable_causes must be short technical causes, not broad business language.
6. next_checks must be operationally ordered and validation-first.
   Start with inspection and validation before disruptive steps.
7. recommended_actions must also be operationally ordered.
   Prefer:
   - validate
   - inspect
   - compare
   - confirm
   - isolate
   before:
   - restart
   - rollback
   - redeploy
8. Do not recommend restart or rollback as the first action unless the ticket strongly indicates an immediate rollback condition.
9. confidence must be between 0 and 1.
10. Set escalate=true when evidence is weak, ambiguous, cross-domain, or the issue may require human investigation.
11. Use at most:
   - 1 classification
   - 2 to 4 probable_causes
   - 3 to 5 next_checks
   - 3 to 5 recommended_actions

Examples of good style:
- classification: certificate_rotation_policy_conflict
- probable_causes: ["certificate_trust_chain_mismatch", "service_mesh_policy_conflict"]
- next_checks: ["verify_certificate_chain", "compare_mesh_policy_changes", "inspect_sidecar_proxy_logs"]
- recommended_actions: ["validate_trust_bundle_distribution", "correct_conflicting_mesh_policy", "restart_affected_proxies_if_needed"]

Now return the JSON only.
""".strip()


def _clean_json_text(text: str) -> str:
    text = (text or "").strip()
    text = text.replace("```json", "").replace("```", "").strip()

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group()

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

    parsed.setdefault("confidence", 0.75)
    parsed.setdefault("escalate", True)

    parsed["fallback_source"] = "groq_chat_completions"
    parsed["fallback_model"] = GROQ_MODEL
    parsed["fallback_provider"] = "groq"

    return parsed


def run_openai_fallback(ticket: str, context: str) -> dict[str, Any]:
    if not FALLBACK_ACTIVE:
        return {
            "classification": "fallback_disabled",
            "probable_causes": ["fallback_disabled_by_configuration"],
            "next_checks": [],
            "recommended_actions": [],
            "confidence": 0.2,
            "escalate": True,
            "fallback_source": "groq_chat_completions",
            "fallback_model": GROQ_MODEL,
            "fallback_provider": "groq",
        }

    if not GROQ_API_KEY:
        return {
            "classification": "fallback_unavailable",
            "probable_causes": ["groq_api_key_not_configured"],
            "next_checks": [],
            "recommended_actions": [],
            "confidence": 0.2,
            "escalate": True,
            "fallback_source": "groq_chat_completions",
            "fallback_model": GROQ_MODEL,
            "fallback_provider": "groq",
        }

    url = f"{GROQ_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": GROQ_MODEL,
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
        "max_tokens": 700,
    }

    r = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=GROQ_TIMEOUT_SECONDS,
    )
    r.raise_for_status()

    body = r.json()
    raw_text = body["choices"][0]["message"]["content"]
    cleaned = _clean_json_text(raw_text)

    try:
        parsed = json.loads(cleaned)
        return _normalize_response(parsed)
    except Exception:
        return {
            "classification": "ambiguous_post_change_service_issue",
            "probable_causes": ["fallback_non_json_response"],
            "next_checks": ["inspect_fallback_raw_output"],
            "recommended_actions": ["review_fallback_prompt_output"],
            "confidence": 0.5,
            "escalate": True,
            "fallback_source": "groq_chat_completions",
            "fallback_model": GROQ_MODEL,
            "fallback_provider": "groq",
        }


def analyze_with_groq_fallback(ticket: str) -> dict:
    context = ""
    return run_openai_fallback(ticket, context)