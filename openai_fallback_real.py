import json
from typing import Any

import requests

from settings_portable import (
    ENABLE_OPENAI_FALLBACK,
    FALLBACK_THRESHOLD,
    FALLBACK_TIMEOUT_SECONDS,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
)


def should_trigger_fallback(confidence_result: dict[str, Any]) -> bool:
    if not ENABLE_OPENAI_FALLBACK:
        return False
    return float(confidence_result.get("final_confidence", 0.0)) < FALLBACK_THRESHOLD


def _build_payload(ticket: str, context: str) -> dict[str, Any]:
    instructions = (
        "You are a senior IT operations engineer. "
        "Return strict JSON only with keys: "
        "classification, probable_causes, next_checks, recommended_actions, confidence, escalate. "
        "Use short factual lists. No markdown."
    )
    prompt = f"Ticket:\n{ticket}\n\nContext:\n{context}\n"
    return {
        "model": OPENAI_MODEL,
        "instructions": instructions,
        "input": prompt,
    }


def _extract_text(resp_json: dict[str, Any]) -> str:
    if isinstance(resp_json.get("output_text"), str) and resp_json["output_text"].strip():
        return resp_json["output_text"].strip()

    output = resp_json.get("output") or []
    chunks = []
    for item in output:
        content = item.get("content") or []
        for c in content:
            if c.get("type") in {"output_text", "text"} and c.get("text"):
                chunks.append(c["text"])
    return "\n".join(chunks).strip()


def _clean_json(text: str) -> str:
    text = (text or "").strip().replace("```json", "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    import re
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text


def run_openai_fallback(ticket: str, context: str) -> dict[str, Any]:
    if not ENABLE_OPENAI_FALLBACK:
        return {
            "classification": "fallback_disabled",
            "probable_causes": ["OpenAI fallback is disabled by configuration."],
            "next_checks": [],
            "recommended_actions": [],
            "confidence": 0.2,
            "escalate": True,
        }

    if not OPENAI_API_KEY:
        return {
            "classification": "fallback_unavailable",
            "probable_causes": ["OpenAI API key is not configured."],
            "next_checks": [],
            "recommended_actions": [],
            "confidence": 0.2,
            "escalate": True,
        }

    url = f"{OPENAI_BASE_URL.rstrip('/')}/responses"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = _build_payload(ticket, context)

    r = requests.post(url, headers=headers, json=payload, timeout=FALLBACK_TIMEOUT_SECONDS)
    r.raise_for_status()
    body = r.json()

    raw_text = _extract_text(body)
    cleaned = _clean_json(raw_text)

    try:
        parsed = json.loads(cleaned)
    except Exception:
        parsed = {
            "classification": "fallback_generated",
            "probable_causes": [raw_text or "Fallback produced a non-JSON response."],
            "next_checks": [],
            "recommended_actions": [],
            "confidence": 0.9,
            "escalate": True,
        }

    parsed.setdefault("classification", "fallback_generated")
    parsed.setdefault("probable_causes", [])
    parsed.setdefault("next_checks", [])
    parsed.setdefault("recommended_actions", [])
    parsed.setdefault("confidence", 0.9)
    parsed.setdefault("escalate", True)
    parsed["fallback_source"] = "openai_responses_api"
    parsed["fallback_model"] = OPENAI_MODEL
    return parsed
