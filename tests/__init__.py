"""Test package. Tests must never write to real runtime data (debug logs, queue, evidence, audit)."""
import settings_portable

settings_portable.DEBUG_MODE = False  # pipeline/fallback debug events would otherwise append to data/debug/*.jsonl
