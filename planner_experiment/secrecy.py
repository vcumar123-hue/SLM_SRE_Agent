"""
The experiment key: what makes the recorded draw non-reversible.

A draw file records, per drawn ticket, a content hash, a cluster id, a ticket type and a text sketch. Unkeyed, every one of those can be
attacked with a dictionary: hash candidate words, word pairs, hosts or titles and see which appear (the vocabulary of IT tickets is small).
So everything derived from ticket **content** is stored as an HMAC-SHA256 under a 256-bit secret key that is never committed. Without the key
the recorded values are indistinguishable from random: nothing can be read out of them, and no dictionary helps.

What the key does and does not protect. The key is what stops a reader of the repository learning anything about the drawn tickets. It does not
make the draw safe from whoever holds it: with the key, someone can hash candidate words and word pairs and see which appear in a sketch, i.e.
run the same dictionary attack the unkeyed version allowed. So the key must be held only by the machine that draws, freezes and runs the
experiment (the owner's or a gated CI), never by developers or reviewers, and the code is built for that: a machine without the key fails closed.

One key for the whole study. Every round is drawn, frozen and run under the same key. Earlier rounds' clusters are recorded as HMACs under the key they
were drawn with, so under a different key none of them would match and their clusters would silently become eligible again; `draw_experiment`
therefore refuses a key that differs from any earlier round's, and a draw records the earlier rounds it excluded with their key check values.

Custody: the key lives in `data/planner_experiment.key` (git-ignored) or in the environment variable below, on the machine that draws, freezes and
runs the experiment (the owner's or a gated CI), and nowhere in the repository. A machine without the key can verify nothing keyed and, once a draw
exists, can run only the development subset outside the run claim (experiment.py). Losing the key means the draw cannot be re-verified, so a lost
key ends that round; it is recorded in the freeze, never regenerated silently (`create_key` refuses to overwrite).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
KEY_ENV = "AIOPS_PE_KEY"
KEY_FILE = ROOT / "data" / "planner_experiment.key"
KEY_BYTES = 32


class KeyError_(Exception):
    """The experiment key is missing, malformed, or being overwritten."""


def parse_key(text: str) -> bytes:
    raw = str(text).strip().lower()
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise KeyError_("the experiment key must be hexadecimal") from exc
    if len(key) != KEY_BYTES:
        raise KeyError_(f"the experiment key must be {KEY_BYTES} bytes ({KEY_BYTES * 2} hex characters)")
    return key


def load_key(path: Optional[Path] = None) -> Optional[bytes]:
    """The key from the environment, else from the key file; None if this machine holds none."""
    env = os.getenv(KEY_ENV, "").strip()
    if env:
        return parse_key(env)
    p = Path(path) if path is not None else KEY_FILE
    if p.is_file():
        return parse_key(p.read_text(encoding="utf-8"))
    return None


def create_key(path: Optional[Path] = None) -> str:
    """Create the key file once. Returns the key's check value (never the key)."""
    p = Path(path) if path is not None else KEY_FILE
    if p.exists():
        raise KeyError_(f"{p} already exists: the experiment key is created once and never replaced")
    p.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(KEY_BYTES)
    p.write_text(key.hex(), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return key_check(key)


def keyed(key: bytes, label: str, value: str) -> str:
    """HMAC-SHA256 of a value under the key, domain-separated by label (so a content hash is never usable as a token hash)."""
    return hmac.new(key, f"{label}\x00{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def key_check(key: bytes) -> str:
    """A value recorded with each draw so a wrong key is detected; it reveals nothing about the key."""
    return keyed(key, "key-check", "planner-experiment")[:16]
