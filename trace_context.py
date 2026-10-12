from __future__ import annotations
import threading
import uuid
from typing import Optional

_local = threading.local()

def new_trace_id() -> str:
    return uuid.uuid4().hex

def set_trace_id(trace_id: str) -> None:
    _local.trace_id = trace_id

def get_trace_id() -> Optional[str]:
    return getattr(_local, "trace_id", None)

def ensure_trace_id() -> str:
    tid = get_trace_id()
    if not tid:
        tid = new_trace_id()
        set_trace_id(tid)
    return tid

def clear_trace_id() -> None:
    _local.trace_id = None
