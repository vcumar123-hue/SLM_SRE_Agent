from pathlib import Path
from typing import Any, Optional, Union

from file_manager import append_jsonl
from settings_portable import CONFIDENCE_LOG_FILE


def log_confidence(record: dict[str, Any], path: Optional[Union[str, Path]] = None) -> None:
    """
    Append one confidence record to the confidence log.

    If path is not provided, uses CONFIDENCE_LOG_FILE from settings_portable.
    """
    target = Path(path) if path is not None else CONFIDENCE_LOG_FILE
    append_jsonl(target, record)