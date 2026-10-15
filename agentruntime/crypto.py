"""
Application-level encryption for stored run data (design 11.2, owner decision 3).

AES-256-GCM with a key that lives outside the database: AIOPS_AGENT_DATA_KEY (base64, 32 bytes) or AIOPS_AGENT_DATA_KEY_FILE.
Ciphertexts are bound to their tenant, run and field through the authenticated data, so a ciphertext moved to another tenant or
field does not decrypt. Encryption is on by default; the only way off is a valid, recorded waiver (see Waiver).
If encryption is required and no key is available, persistence is refused and the mode resolves to `off`.
Plaintext storage needs a recorded waiver **file** (who, why, the policy reference, the date, threat model reviewed) loaded by
`load_waiver`; a Waiver built by a caller is never accepted, and an environment variable alone is not one.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

try:  # pragma: no cover - exercised where the package is installed
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:  # noqa: BLE001
    AESGCM = None  # type: ignore[assignment]

PREFIX = "v1"


class KeyMissing(Exception):
    """Encryption is required but no usable key is configured."""


class DecryptionError(Exception):
    """A ciphertext failed authentication (wrong key, wrong tenant, wrong field, or tampering)."""


_LOADED = object()      # proves a Waiver came from load_waiver(); a caller cannot supply it from outside this module


@dataclass(frozen=True)
class Waiver:
    """An auditable decision to store run data without application-level encryption (owner decision 3).

    A usable waiver exists only if it was **loaded from the recorded waiver file** by `load_waiver`: that proves the file exists, names who
    approved it, why, the client policy reference and the date, and that the threat model was reviewed. A Waiver built by a caller is
    never `verified`, and storage refuses it. `record_hash` is the SHA-256 of the file content, kept with every run for audit.
    """
    approved_by: str
    reason: str
    reference: str            # the client policy or threat-model document that permits infrastructure encryption alone
    approved_at: str
    record_hash: str = ""
    _token: object = field(default=None, repr=False, compare=False)

    def valid(self) -> bool:
        return all(isinstance(v, str) and v.strip() for v in (self.approved_by, self.reason, self.reference, self.approved_at))

    @property
    def verified(self) -> bool:
        return self._token is _LOADED and self.valid() and bool(self.record_hash)

    def record(self) -> dict:
        return {"approved_by": self.approved_by, "reason": self.reason, "reference": self.reference,
                "approved_at": self.approved_at, "record_hash": self.record_hash}


def waiver_path() -> Path:
    return Path(os.getenv("AIOPS_AGENT_DATA_WAIVER_FILE", "config/agent_data_encryption_waiver.json"))


def load_waiver(path: Optional[Path] = None) -> Optional[Waiver]:
    """The recorded, verified waiver, or None when there is none or it is incomplete (incomplete means not waived)."""
    p = path or waiver_path()
    try:
        if not p.is_file():
            return None
        raw = p.read_bytes()
        data = json.loads(raw.decode("utf-8-sig"))
        waiver = Waiver(str(data.get("approved_by", "")), str(data.get("reason", "")), str(data.get("reference", "")),
                        str(data.get("approved_at", "")), hashlib.sha256(raw).hexdigest(), _LOADED)
        return waiver if waiver.valid() and data.get("threat_model_reviewed") is True else None
    except (OSError, ValueError, AttributeError):
        return None


def encryption_required(waiver_loader: Optional[Callable[[], Optional[Waiver]]] = None) -> bool:
    return (waiver_loader or load_waiver)() is None


def _load_key() -> Optional[bytes]:
    raw = os.getenv("AIOPS_AGENT_DATA_KEY", "").strip()
    if not raw:
        key_file = os.getenv("AIOPS_AGENT_DATA_KEY_FILE", "").strip()
        if key_file:
            try:
                raw = Path(key_file).read_text(encoding="utf-8").strip()
            except OSError:
                return None
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception:  # noqa: BLE001
        return None
    return key if len(key) == 32 else None


def generate_key() -> str:
    """A new key in the format the environment variable expects (store it outside the database)."""
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def key_available() -> bool:
    return AESGCM is not None and _load_key() is not None


class Cipher:
    def __init__(self, key: bytes) -> None:
        if AESGCM is None:
            raise KeyMissing("the 'cryptography' package is not installed")
        if len(key) != 32:
            raise KeyMissing("the data key must be 32 bytes")
        self._aead = AESGCM(key)
        self.key_id = hashlib.sha256(key).hexdigest()[:8]

    @staticmethod
    def _aad(tenant_id: str, run_id: str, field: str) -> bytes:
        return f"{tenant_id}|{run_id}|{field}".encode("utf-8")

    def encrypt(self, plaintext: str, tenant_id: str, run_id: str, field: str) -> str:
        nonce = secrets.token_bytes(12)
        ct = self._aead.encrypt(nonce, plaintext.encode("utf-8"), self._aad(tenant_id, run_id, field))
        return f"{PREFIX}:{self.key_id}:" + base64.b64encode(nonce + ct).decode("ascii")

    def decrypt(self, token: str, tenant_id: str, run_id: str, field: str) -> str:
        try:
            prefix, key_id, body = token.split(":", 2)
            if prefix != PREFIX:
                raise ValueError("unknown format")
            if key_id != self.key_id:
                raise ValueError("encrypted with a different key")
            blob = base64.b64decode(body)
            return self._aead.decrypt(blob[:12], blob[12:], self._aad(tenant_id, run_id, field)).decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            raise DecryptionError(str(exc)) from exc


def get_cipher(waiver_loader: Optional[Callable[[], Optional[Waiver]]] = None) -> Optional[Cipher]:
    """The cipher when encryption is on and a key exists; None only under a recorded waiver; KeyMissing when required but absent.

    `waiver_loader` exists so tests can point at a waiver file of their own; production code uses the default loader.
    """
    if not encryption_required(waiver_loader):
        return None
    key = _load_key()
    if key is None or AESGCM is None:
        raise KeyMissing("encryption is required but no usable AIOPS_AGENT_DATA_KEY / AIOPS_AGENT_DATA_KEY_FILE is available")
    return Cipher(key)
