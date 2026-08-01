from pathlib import Path
from typing import Any, Optional, Union

from file_manager import append_jsonl
from settings_portable import FALLBACK_LOG_FILE


def log_fallback_event(record: dict[str, Any], path: Optional[Union[str, Path]] = None) -> None:
    """
    Append one fallback event to the fallback log.

    If path is not provided, uses FALLBACK_LOG_FILE from settings_portable.
    """
    target = Path(path) if path is not None else FALLBACK_LOG_FILE
    append_jsonl(target, record)