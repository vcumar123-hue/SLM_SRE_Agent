"""
The model manifest: exactly which model the model planners use, and under what settings. It lives in the frozen planners folder, so it is hashed into
the planner freeze and cannot change afterwards; the planner freeze command refuses until it is complete.

What is frozen
  * the model: the identifier LM Studio serves it under, publisher, architecture, quantisation, and the **SHA-256 of the exact GGUF file** (the revision:
    a community GGUF has no commit id of its own; the file hash identifies it uniquely);
  * the inference parameters sent on every request (temperature 0, a fixed seed, a strict JSON-schema response format);
  * the budgets in force (timeouts and token limits), taken from `Budgets()` so they cannot drift from the runtime's own;
  * the prompt versions (filled in when the prompts are written; the manifest is incomplete until they are).

What is observed, not frozen: LM Studio loads a model on demand and chooses the context length at load time, so the loaded context length and load state
are recorded by `verify()` at run time (and belong in the run's report), not asserted here.

Local only. Every planner uses this local endpoint. No planner may use an external provider: Groq needs explicit per-tenant permission naming the planner
purpose, and none is assumed (design 17, decision 4).
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agentruntime.contracts import Budgets

MANIFEST_VERSION = "1"

MODEL: Dict[str, Any] = {
    "served_id": "google/gemma-3-4b",
    "publisher": "google",
    "architecture": "gemma3",
    "format": "gguf",
    "quantization": "Q4_K_M",
    "file": "lmstudio-community/gemma-3-4b-it-GGUF/gemma-3-4b-it-Q4_K_M.gguf",
    "file_bytes": 2489757856,
    "file_sha256": "be49949e48422e4547b00af14179a193d3777eea7fbbd7d6e1b0861304628a01",
    "server": "LM Studio, OpenAI-compatible /v1/chat/completions",
    "endpoint": "http://localhost:1234/v1",
    "scope": "local only: no planner uses an external provider",
}

INFERENCE: Dict[str, Any] = {
    "temperature": 0.0,
    "top_p": 1.0,
    "seed": 1,                                   # fixed so repeated requests are as repeatable as the server allows; the value itself is arbitrary
    "n": 1,
    "stream": False,
    "response_format": "json_schema (strict, additionalProperties false)",
    "max_tokens": "per request, from the budgets below (planner / synthesis), clamped by the remaining run token budget",
    "prompt_budget_tokens": 4096,                # planner prompts must fit this, whatever context length the server loads
}

# Which Budgets fields are part of the model's operating envelope (values are read from Budgets() so they cannot drift from the runtime).
BUDGET_FIELDS = ("model_call_timeout_s", "tool_timeout_s", "deadline_s", "max_model_calls", "max_tokens_planner", "max_tokens_synthesis",
                 "max_tokens_run", "max_steps", "max_tool_calls")

# Prompt versions: one entry per prompt the model planners send. None until the prompt exists.
PROMPTS: Dict[str, Optional[str]] = {
    "planner": None,
    "synthesis": None,
}


def budgets_in_force() -> Dict[str, Any]:
    dumped = Budgets().model_dump()
    return {k: dumped[k] for k in BUDGET_FIELDS}


def manifest() -> Dict[str, Any]:
    return {"manifest_version": MANIFEST_VERSION, "model": dict(MODEL), "inference": dict(INFERENCE), "budgets": budgets_in_force(), "prompts": dict(PROMPTS)}


def manifest_hash() -> str:
    return hashlib.sha256(json.dumps(manifest(), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def incomplete() -> List[str]:
    """What is still missing before the planners may be frozen. Empty when the manifest is complete."""
    problems: List[str] = []
    for key in ("served_id", "publisher", "architecture", "format", "quantization", "file", "server", "endpoint", "scope"):
        if not str(MODEL.get(key) or "").strip():
            problems.append(f"model.{key} is empty")
    if not re.fullmatch(r"[0-9a-f]{64}", str(MODEL.get("file_sha256") or "")):
        problems.append("model.file_sha256 must be a 64-character hexadecimal SHA-256")
    if not isinstance(MODEL.get("file_bytes"), int) or MODEL["file_bytes"] <= 0:
        problems.append("model.file_bytes must be a positive integer")
    for key in ("temperature", "top_p", "seed", "response_format", "prompt_budget_tokens"):
        if INFERENCE.get(key) is None:
            problems.append(f"inference.{key} is not set")
    for name, version in PROMPTS.items():
        if not str(version or "").strip():
            problems.append(f"prompt '{name}' has no version yet (written together with the planners)")
    return problems


# -- checking the manifest against the real thing ---------------------------------------------------------------------------------------------------

def _get_json(url: str, opener: Callable, timeout_s: float) -> Any:
    with opener(url, timeout_s) as response:
        return json.loads(response.read().decode("utf-8"))


def _default_opener(url: str, timeout_s: float):
    return urllib.request.urlopen(url, timeout=timeout_s)


def file_sha256(path: Path, chunk: int = 8 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(opener: Callable = _default_opener, models_dir: Optional[Path] = None, check_file: bool = True, timeout_s: float = 5.0) -> Dict[str, Any]:
    """Check the manifest against the running server and the model file. Returns {'ok': bool, 'checks': [...], 'observed': {...}}.
    Nothing here raises: an unreachable server is a failed check. The key holder runs this before the experiment (`cli verify-model`)."""
    checks: List[Dict[str, Any]] = []
    observed: Dict[str, Any] = {}

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    base = MODEL["endpoint"].rstrip("/")
    root = base[: -len("/v1")] if base.endswith("/v1") else base
    try:
        served = [m.get("id") for m in _get_json(base + "/models", opener, timeout_s).get("data", [])]
        add("server serves the model", MODEL["served_id"] in served, f"serves {len(served)} model(s)")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        add("server reachable", False, f"{type(exc).__name__}: {exc}")
        served = []
    if MODEL["served_id"] in served:
        try:
            detail = _get_json(f"{root}/api/v0/models/{MODEL['served_id']}", opener, timeout_s)
            for key, want in (("publisher", MODEL["publisher"]), ("arch", MODEL["architecture"]), ("quantization", MODEL["quantization"])):
                add(f"server reports {key}", str(detail.get(key)) == want, f"{detail.get(key)!r} (manifest {want!r})")
            observed = {"state": detail.get("state"), "loaded_context_length": detail.get("loaded_context_length"),
                        "max_context_length": detail.get("max_context_length")}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            add("server model details", False, f"{type(exc).__name__}: {exc}")
    if check_file:
        directory = Path(models_dir) if models_dir is not None else Path.home() / ".lmstudio" / "models"
        path = directory / MODEL["file"]
        if not path.is_file():
            add("model file present", False, str(path))
        else:
            size = path.stat().st_size
            add("model file size", size == MODEL["file_bytes"], f"{size} bytes (manifest {MODEL['file_bytes']})")
            actual = file_sha256(path)
            add("model file sha256", actual == MODEL["file_sha256"], actual)
    return {"ok": all(c["ok"] for c in checks) and bool(checks), "checks": checks, "observed": observed, "manifest_hash": manifest_hash(),
            "manifest_complete": not incomplete()}
