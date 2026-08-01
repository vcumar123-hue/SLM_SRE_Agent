from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


PathLike = Union[str, Path]


def to_path(path: PathLike) -> Path:
    """Convert incoming path-like value to pathlib.Path."""
    if isinstance(path, Path):
        return path
    return Path(path)


def ensure_dir(path: PathLike) -> Path:
    """
    Ensure a directory exists.
    Returns the resolved Path object.
    """
    p = to_path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def ensure_parent_dir(path: PathLike) -> Path:
    """
    Ensure the parent directory for a file path exists.
    Returns the original file Path.
    """
    p = to_path(path)
    if p.parent:
        p.parent.mkdir(parents=True, exist_ok=True)
    return p


def file_exists(path: PathLike) -> bool:
    return to_path(path).exists()


def safe_unlink(path: PathLike) -> bool:
    """
    Delete a file if it exists.
    Returns True if removed, False otherwise.
    """
    p = to_path(path)
    try:
        if p.exists():
            p.unlink()
            return True
        return False
    except Exception:
        return False


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_text(path: PathLike, default: str = "", encoding: str = "utf-8") -> str:
    """
    Read a text file safely.
    Returns default if file does not exist or read fails.
    """
    p = to_path(path)
    try:
        if not p.exists():
            return default
        return p.read_text(encoding=encoding)
    except Exception:
        return default


def write_text(path: PathLike, content: str, encoding: str = "utf-8") -> bool:
    """
    Write text content safely.
    Returns True on success.
    """
    p = ensure_parent_dir(path)
    try:
        p.write_text(content, encoding=encoding)
        return True
    except Exception:
        return False


def append_text(path: PathLike, content: str, encoding: str = "utf-8") -> bool:
    """
    Append text content safely.
    Returns True on success.
    """
    p = ensure_parent_dir(path)
    try:
        with p.open("a", encoding=encoding) as f:
            f.write(content)
        return True
    except Exception:
        return False


def read_json(path: PathLike, default: Optional[Any] = None, encoding: str = "utf-8") -> Any:
    """
    Read JSON file safely.
    Returns default if file does not exist or parsing fails.
    """
    p = to_path(path)
    try:
        if not p.exists():
            return default
        with p.open("r", encoding=encoding) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(
    path: PathLike,
    data: Any,
    indent: int = 2,
    encoding: str = "utf-8",
    ensure_ascii: bool = False,
) -> bool:
    """Write JSON file safely using atomic temp-file replace (CQ-03)."""
    import tempfile
    import os as _os
    p = ensure_parent_dir(path)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
        try:
            with _os.fdopen(fd, "w", encoding=encoding) as f:
                json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)
            _os.replace(tmp_path, p)
        except Exception:
            try:
                _os.unlink(tmp_path)
            except Exception:
                pass
            raise
        return True
    except Exception:
        return False


def read_jsonl(path: PathLike, encoding: str = "utf-8") -> List[Dict[str, Any]]:
    """
    Read JSONL file safely.
    Skips malformed lines.
    Returns list of parsed JSON objects.
    """
    p = to_path(path)
    records: List[Dict[str, Any]] = []

    try:
        if not p.exists():
            return records

        with p.open("r", encoding=encoding) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        records.append(obj)
                    else:
                        records.append({"value": obj})
                except Exception:
                    # Skip malformed lines rather than failing whole read
                    continue
    except Exception:
        return []

    return records


def append_jsonl(path: PathLike, record: Dict[str, Any], encoding: str = "utf-8") -> bool:
    """
    Append one JSON object to a JSONL file.
    Returns True on success.
    """
    p = ensure_parent_dir(path)
    try:
        with p.open("a", encoding=encoding) as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def overwrite_jsonl(path: PathLike, records: List[Dict[str, Any]], encoding: str = "utf-8") -> bool:
    """
    Replace a JSONL file with a new list of JSON records.
    Returns True on success.
    """
    p = ensure_parent_dir(path)
    try:
        with p.open("w", encoding=encoding) as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def append_debug_event(
    path: PathLike,
    event_type: str,
    payload: Dict[str, Any],
    request_id: Optional[str] = None,
    component: Optional[str] = None,
) -> bool:
    """
    Standardized debug event writer for JSONL logs.
    """
    record = {
        "timestamp": utc_now_iso(),
        "event_type": event_type,
        "component": component or "unknown",
        "request_id": request_id,
        "payload": payload,
    }
    return append_jsonl(path, record)


def list_files(directory: PathLike, pattern: str = "*") -> List[Path]:
    """
    Return sorted files matching a glob pattern.
    """
    p = to_path(directory)
    if not p.exists() or not p.is_dir():
        return []
    return sorted(p.glob(pattern))


def load_env_file(env_path: PathLike) -> None:
    """
    Lightweight .env loader to avoid requiring python-dotenv.
    Existing environment variables are preserved unless not already set.
    """
    p = to_path(env_path)
    if not p.exists():
        return

    try:
        for raw_line in p.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")

            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        # Silent fail keeps startup resilient
        return