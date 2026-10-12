from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

from file_manager import ensure_dir, load_env_file


# -------------------------------------------------------------------
# Base paths
# -------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"

# Load .env safely
load_env_file(ENV_FILE)

DATA_DIR = ensure_dir(Path(os.getenv("DATA_DIR", PROJECT_ROOT / "data")))
LOG_DIR = ensure_dir(Path(os.getenv("LOG_DIR", PROJECT_ROOT / "logs")))
DEBUG_LOG_DIR = ensure_dir(Path(os.getenv("DEBUG_LOG_DIR", DATA_DIR / "debug")))
CACHE_DIR = ensure_dir(Path(os.getenv("CACHE_DIR", DATA_DIR / "cache")))
EXPORT_DIR = ensure_dir(Path(os.getenv("EXPORT_DIR", DATA_DIR / "exports")))


# -------------------------------------------------------------------
# App config
# -------------------------------------------------------------------
APP_NAME = os.getenv("APP_NAME", "AI SRE Platform")
APP_ENV = os.getenv("APP_ENV", "dev")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))


# -------------------------------------------------------------------
# Helper
# -------------------------------------------------------------------
def _get_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name, str(default)).strip().lower()
    return val in {"1", "true", "yes", "y", "on"}


# -------------------------------------------------------------------
# Debug flags
# -------------------------------------------------------------------
DEBUG_MODE = _get_bool("DEBUG_MODE", True)
DEBUG_RETRIEVAL = _get_bool("DEBUG_RETRIEVAL", True)
DEBUG_CONFIDENCE = _get_bool("DEBUG_CONFIDENCE", True)
DEBUG_FEEDBACK = _get_bool("DEBUG_FEEDBACK", True)
DEBUG_FALLBACK = _get_bool("DEBUG_FALLBACK", True)
DEBUG_UI = _get_bool("DEBUG_UI", True)


# -------------------------------------------------------------------
# Core storage files
# -------------------------------------------------------------------
INCIDENTS_FILE = Path(os.getenv("INCIDENTS_FILE", DATA_DIR / "incidents.json"))
TAXONOMY_FILE = Path(os.getenv("TAXONOMY_FILE", DATA_DIR / "taxonomy.json"))

FEEDBACK_FILE = Path(os.getenv("FEEDBACK_FILE", DATA_DIR / "feedback.json"))
FEEDBACK_LEARNING_FILE = Path(os.getenv("FEEDBACK_LEARNING_FILE", DATA_DIR / "feedback_learning.json"))
FEEDBACK_LOG_FILE = Path(os.getenv("FEEDBACK_LOG_FILE", LOG_DIR / "feedback_log.jsonl"))

CONFIDENCE_LOG_FILE = Path(os.getenv("CONFIDENCE_LOG_FILE", LOG_DIR / "confidence_log.jsonl"))
FALLBACK_LOG_FILE = Path(os.getenv("FALLBACK_LOG_FILE", LOG_DIR / "fallback_log.jsonl"))
APP_LOG_FILE = Path(os.getenv("APP_LOG_FILE", LOG_DIR / "app_log.jsonl"))


# -------------------------------------------------------------------
# Debug logs
# -------------------------------------------------------------------
RETRIEVAL_DEBUG_FILE = Path(os.getenv("RETRIEVAL_DEBUG_FILE", DEBUG_LOG_DIR / "retrieval_debug.jsonl"))
CONFIDENCE_DEBUG_FILE = Path(os.getenv("CONFIDENCE_DEBUG_FILE", DEBUG_LOG_DIR / "confidence_debug.jsonl"))
FEEDBACK_DEBUG_FILE = Path(os.getenv("FEEDBACK_DEBUG_FILE", DEBUG_LOG_DIR / "feedback_debug.jsonl"))
FALLBACK_DEBUG_FILE = Path(os.getenv("FALLBACK_DEBUG_FILE", DEBUG_LOG_DIR / "fallback_debug.jsonl"))
PIPELINE_DEBUG_FILE = Path(os.getenv("PIPELINE_DEBUG_FILE", DEBUG_LOG_DIR / "pipeline_debug.jsonl"))
UI_DEBUG_FILE = Path(os.getenv("UI_DEBUG_FILE", DEBUG_LOG_DIR / "ui_debug.jsonl"))
PROMOTION_AUDIT_FILE = Path(os.getenv("PROMOTION_AUDIT_FILE", DEBUG_LOG_DIR / "promotion_audit.jsonl"))

# Ensure directories exist
for p in [
    INCIDENTS_FILE,
    TAXONOMY_FILE,
    FEEDBACK_FILE,
    FEEDBACK_LEARNING_FILE,
    FEEDBACK_LOG_FILE,
    CONFIDENCE_LOG_FILE,
    FALLBACK_LOG_FILE,
    APP_LOG_FILE,
    RETRIEVAL_DEBUG_FILE,
    CONFIDENCE_DEBUG_FILE,
    FEEDBACK_DEBUG_FILE,
    FALLBACK_DEBUG_FILE,
    PIPELINE_DEBUG_FILE,
    UI_DEBUG_FILE,
    PROMOTION_AUDIT_FILE,
]:
    ensure_dir(p.parent)


# -------------------------------------------------------------------
# Model / Embedding config  — auto-detects LM Studio or Ollama
# -------------------------------------------------------------------

# Model name translations: LM Studio filename → Ollama tag
_OLLAMA_MODEL_MAP: Dict[str, str] = {
    "gemma-3-4b-it":                       "gemma3:4b",
    "google/gemma-3-4b":                   "gemma3:4b",
    "gemma-3n-4b-vision-finetuned-i1":     "gemma3n:4b",
    "text-embedding-nomic-embed-text-v1.5": "nomic-embed-text",
    "qwen2.5-3b-instruct":                 "qwen2.5:3b",
}


def _detect_local_model_provider() -> tuple[str, str, str]:
    """
    Probe known local model servers in order: LM Studio → Ollama (local) → Ollama (Docker).
    Returns (base_url, api_key, provider_label).
    Uses only stdlib so there are no import-order issues.
    """
    import urllib.request
    import urllib.error

    candidates = [
        ("lmstudio",      "http://localhost:1234/v1",  "lm-studio"),
        ("ollama_local",  "http://localhost:11434/v1", "ollama"),
        ("ollama_docker", "http://ollama:11434/v1",    "ollama"),
    ]
    for label, url, api_key in candidates:
        try:
            req = urllib.request.Request(
                f"{url}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return url, api_key, label
        except Exception:
            continue
    # Nothing found — fall back to LM Studio default so existing code still works
    return "http://localhost:1234/v1", "lm-studio", "none"


def _remap_model(name: str, provider: str) -> str:
    """Translate an LM Studio model name to its Ollama equivalent when needed."""
    if provider.startswith("ollama"):
        return _OLLAMA_MODEL_MAP.get(name, name)
    return name


# If MODEL_BASE_URL is explicitly set, trust it and skip probing.
_explicit_url = os.getenv("MODEL_BASE_URL")
if _explicit_url:
    MODEL_BASE_URL  = _explicit_url
    MODEL_API_KEY   = os.getenv("MODEL_API_KEY", "lm-studio")
    LOCAL_MODEL_PROVIDER = os.getenv("LOCAL_MODEL_PROVIDER", "explicit")
else:
    MODEL_BASE_URL, MODEL_API_KEY, LOCAL_MODEL_PROVIDER = _detect_local_model_provider()
    print(f"[settings] Local model provider detected: {LOCAL_MODEL_PROVIDER} → {MODEL_BASE_URL}")

# Model names — env vars win; otherwise use defaults remapped for the detected provider
PRIMARY_CHAT_MODEL = os.getenv(
    "PRIMARY_CHAT_MODEL",
    _remap_model("gemma-3-4b-it", LOCAL_MODEL_PROVIDER),
)
PRIMARY_VISION_MODEL = os.getenv(
    "PRIMARY_VISION_MODEL",
    _remap_model("gemma-3n-4b-vision-finetuned-i1", LOCAL_MODEL_PROVIDER),
)
EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    _remap_model("text-embedding-nomic-embed-text-v1.5", LOCAL_MODEL_PROVIDER),
)

REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "60"))


# -------------------------------------------------------------------
# Qdrant config
# -------------------------------------------------------------------
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")
QDRANT_USE_HTTPS = _get_bool("QDRANT_USE_HTTPS", False)

QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "ai_ops_incidents")
QDRANT_FEEDBACK_COLLECTION = os.getenv("QDRANT_FEEDBACK_COLLECTION", QDRANT_COLLECTION)

TOP_K_RETRIEVAL = int(os.getenv("TOP_K_RETRIEVAL", "5"))
MIN_RETRIEVAL_SCORE = float(os.getenv("MIN_RETRIEVAL_SCORE", "0.25"))
HYBRID_ALPHA = float(os.getenv("HYBRID_ALPHA", "0.7"))


# -------------------------------------------------------------------
# Confidence config
# -------------------------------------------------------------------
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.65"))
HIGH_CONFIDENCE_THRESHOLD = float(os.getenv("HIGH_CONFIDENCE_THRESHOLD", "0.75"))

WEIGHT_EMBEDDING_SCORE = float(os.getenv("WEIGHT_EMBEDDING_SCORE", "0.35"))
WEIGHT_LLM_SCORE = float(os.getenv("WEIGHT_LLM_SCORE", "0.25"))
WEIGHT_RETRIEVAL_QUALITY = float(os.getenv("WEIGHT_RETRIEVAL_QUALITY", "0.20"))
WEIGHT_CLASSIFICATION_CERTAINTY = float(os.getenv("WEIGHT_CLASSIFICATION_CERTAINTY", "0.20"))

MIN_PRIMARY_CONFIDENCE_FLOOR = float(os.getenv("MIN_PRIMARY_CONFIDENCE_FLOOR", "0.05"))
ALLOW_FALLBACK_CONFIDENCE_DISPLAY = _get_bool("ALLOW_FALLBACK_CONFIDENCE_DISPLAY", True)


# -------------------------------------------------------------------
# Feedback learning
# -------------------------------------------------------------------
LEARNING_ENABLED = _get_bool("LEARNING_ENABLED", True)
LEARNING_BASE_WEIGHT = float(os.getenv("LEARNING_BASE_WEIGHT", "1.0"))
LEARNING_POSITIVE_INCREMENT = float(os.getenv("LEARNING_POSITIVE_INCREMENT", "0.25"))
LEARNING_NEGATIVE_INCREMENT = float(os.getenv("LEARNING_NEGATIVE_INCREMENT", "0.20"))
LEARNING_MAX_WEIGHT = float(os.getenv("LEARNING_MAX_WEIGHT", "5.0"))
LEARNING_DECAY_ENABLED = _get_bool("LEARNING_DECAY_ENABLED", True)
LEARNING_DECAY_DAYS = int(os.getenv("LEARNING_DECAY_DAYS", "30"))
LEARNING_DECAY_FACTOR = float(os.getenv("LEARNING_DECAY_FACTOR", "0.95"))


# -------------------------------------------------------------------
# Fallback config (UPDATED)
# -------------------------------------------------------------------

# Backward compatibility:
# supports BOTH:
# - FALLBACK_ENABLED
# - ENABLE_GROQ_FALLBACK (legacy)
FALLBACK_ENABLED = _get_bool(
    "FALLBACK_ENABLED",
    _get_bool("ENABLE_GROQ_FALLBACK", True)
)

FALLBACK_PROVIDER = os.getenv("FALLBACK_PROVIDER", "groq").lower()

# Groq config
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_TIMEOUT_SECONDS = int(os.getenv("GROQ_TIMEOUT_SECONDS", "60"))


# -------------------------------------------------------------------
# OCR / optional
# -------------------------------------------------------------------
OCR_ENABLED = _get_bool("OCR_ENABLED", False)


# -------------------------------------------------------------------
# UI config
# -------------------------------------------------------------------
SHOW_DEBUG_PANEL_IN_UI = _get_bool("SHOW_DEBUG_PANEL_IN_UI", True)
SHOW_HISTORICAL_DASHBOARD = _get_bool("SHOW_HISTORICAL_DASHBOARD", True)
DISPLAY_RESPONSE_SOURCE = _get_bool("DISPLAY_RESPONSE_SOURCE", True)


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def get_qdrant_url() -> str:
    scheme = "https" if QDRANT_USE_HTTPS else "http"
    return f"{scheme}://{QDRANT_HOST}:{QDRANT_PORT}"


def fallback_is_configured() -> bool:
    if not FALLBACK_ENABLED:
        return False

    if FALLBACK_PROVIDER == "groq":
        return bool(GROQ_API_KEY and GROQ_BASE_URL and GROQ_MODEL)

    return False


def debug_flags_summary() -> Dict:
    return {
        "DEBUG_MODE": DEBUG_MODE,
        "DEBUG_RETRIEVAL": DEBUG_RETRIEVAL,
        "DEBUG_CONFIDENCE": DEBUG_CONFIDENCE,
        "DEBUG_FEEDBACK": DEBUG_FEEDBACK,
        "DEBUG_FALLBACK": DEBUG_FALLBACK,
        "DEBUG_UI": DEBUG_UI,
    }


def as_dict() -> Dict:
    return {
        "APP_NAME": APP_NAME,
        "ENV": APP_ENV,
        "DATA_DIR": str(DATA_DIR),
        "LOG_DIR": str(LOG_DIR),
        "MODEL": PRIMARY_CHAT_MODEL,
        "EMBEDDING_MODEL": EMBEDDING_MODEL,
        "QDRANT_URL": get_qdrant_url(),
        "QDRANT_COLLECTION": QDRANT_COLLECTION,
        "CONFIDENCE_THRESHOLD": CONFIDENCE_THRESHOLD,
        "FALLBACK_ENABLED": FALLBACK_ENABLED,
        "FALLBACK_PROVIDER": FALLBACK_PROVIDER,
        "GROQ_MODEL": GROQ_MODEL,
        "GROQ_TIMEOUT_SECONDS": GROQ_TIMEOUT_SECONDS,
        "debug_flags": debug_flags_summary(),
    }
