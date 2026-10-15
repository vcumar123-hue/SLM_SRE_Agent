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

Retiring a key, and registering the one in custody. A key that has been on a machine a development session can read has to be treated as
development-accessible, whatever anyone intended. It is **retired by its check value** (`retire`, recorded in `retired_keys.json`, which holds only check values
and reasons): every draw, case build, freeze and run then refuses it. The replacement is created and kept only on the separate key-holder machine, which prints
only its check value (`key-check`); that value is registered here as the key in custody (`register_custody`, `key_custody.json`), and from then on a draw,
freeze or run uses only a key whose check value is the registered one. The replacement key itself never touches the development machine.

Custody: the key lives in `data/planner_experiment.key` (git-ignored) or in the environment variable below, on the machine that draws, freezes and
runs the experiment (the owner's or a gated CI), and nowhere in the repository. A machine without the key can verify nothing keyed and, once a draw
exists, can run only the development subset outside the run claim (experiment.py). Losing the key means the draw cannot be re-verified, so a lost
key ends that round; it is recorded in the freeze, never regenerated silently (`create_key` refuses to overwrite).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional

from planner_experiment.freeze_names import PLACEHOLDER_NAMES

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


# -- retiring keys and registering custody -----------------------------------------------------------------------------------------------------------

CHECK_PATTERN = re.compile(r"[0-9a-f]{16}")


def _read_list(path: Path) -> list:
    path = Path(path)
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def retired_checks(path: Path) -> Dict[str, Dict[str, Any]]:
    return {e["key_check"]: e for e in _read_list(path)}


def is_retired(check: str, path: Path) -> bool:
    return check in retired_checks(path)


def _named(value: str, what: str) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in PLACEHOLDER_NAMES:
        raise KeyError_(f"{what} is required")
    return text


def retire(key: bytes, reason: str, retired_by: str, path: Path) -> Dict[str, Any]:
    """Retire a key by its check value (the key itself is never written). Append-only; retiring the same key twice is refused."""
    reason, retired_by = _named(reason, "a reason"), _named(retired_by, "who retired it")
    check = key_check(key)
    entries = _read_list(path)
    if any(e["key_check"] == check for e in entries):
        raise KeyError_(f"key {check} is already retired")
    entry = {"key_check": check, "reason": reason, "retired_by": retired_by, "date": time.strftime("%Y-%m-%d", time.gmtime())}
    entries.append(entry)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(entries, indent=1), encoding="utf-8")
    return entry


def custody(path: Path) -> Optional[Dict[str, Any]]:
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def register_custody(check: str, custodian: str, path: Path, retired_path: Path) -> Dict[str, Any]:
    """Register the key in custody by its check value, as stated by whoever holds it on the key-holder machine. The check of a retired key is
    refused. An existing registration can be replaced only after the key it names has been retired."""
    check = str(check).strip().lower()
    if not CHECK_PATTERN.fullmatch(check):
        raise KeyError_("a key check is 16 hexadecimal characters (print it with `key-check` on the key-holder machine)")
    custodian = _named(custodian, "the custodian (person and machine)")
    if is_retired(check, retired_path):
        raise KeyError_(f"key {check} is retired and cannot be put in custody")
    existing = custody(path)
    if existing is not None and not is_retired(existing["key_check"], retired_path):
        raise KeyError_(f"key {existing['key_check']} is already registered and has not been retired")
    record = {"key_check": check, "custodian": custodian, "date": time.strftime("%Y-%m-%d", time.gmtime()),
              "statement": "created and kept only on the custodian machine; never present on a machine a development session can read"}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(record, indent=1), encoding="utf-8")
    return record


def active_key(custody_path: Path, retired_path: Path) -> bytes:
    """The key to use for a draw: present, not retired, and exactly the registered key in custody. Raises KeyError_ otherwise."""
    key = load_key()
    if key is None:
        raise KeyError_("the experiment key is required before a planner-experiment set is drawn: everything recorded about the drawn tickets is keyed "
                        "(create it on the key-holder machine and register its check value)")
    check = key_check(key)
    if is_retired(check, retired_path):
        raise KeyError_(f"this key ({check}) is retired: it was on a machine a development session can read. Use the replacement key held on the "
                        f"key-holder machine")
    registered = custody(custody_path)
    if registered is None:
        raise KeyError_("no key is registered as being in custody: register the check value of the key held on the key-holder machine "
                        "(register-custody) before drawing")
    if registered["key_check"] != check:
        raise KeyError_(f"this key ({check}) is not the key registered as being in custody ({registered['key_check']})")
    return key
