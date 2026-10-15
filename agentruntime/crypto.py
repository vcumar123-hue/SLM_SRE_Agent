"""
Application-level encryption for stored run data (design 11.2, owner decision 3).

AES-256-GCM with a key that lives outside the database: AIOPS_AGENT_DATA_KEY (base64, 32 bytes) or AIOPS_AGENT_DATA_KEY_FILE.
Ciphertexts are bound to their tenant, run and field through the authenticated data, so a ciphertext moved to another tenant or
field does not decrypt. Encryption is on by default; the only way off is a valid, recorded waiver (see Waiver).
If encryption is required and no key is available, persistence is refused and the mode resolves to `off`.
Plaintext storage needs a recorded Waiver file (who, why, the policy and threat-model reference); an environment variable alone is not one.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:  # pragma: no cover - exercised where the package is installed
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:  # noqa: BLE001
    AESGCM = None  # type: ignore[assignment]

PREFIX = "v1"


class KeyMissing(Exception):
    """Encryption is required but no usable key is configured."""


class DecryptionError(Exception):
    """A ciphertext failed authentication (wrong key, wrong tenant, wrong field, or tampering)."""


@dataclass(frozen=True)
class Waiver:
    """An auditable decision to store run data without application-level encryption (owner decision 3).

    It must name who approved it, why, and the client policy and threat-model references it rests on. An environment variable alone
    is never a waiver.
    """
    approved_by: str
    reason: str
    reference: str            # the client policy or threat-model document that permits infrastructure encryption alone
    approved_at: str

    def valid(self) -> bool:
        return all(isinstance(v, str) and v.strip() for v in (self.approved_by, self.reason, self.reference, self.approved_at))


def waiver_path() -> Path:
    return Path(os.getenv("AIOPS_AGENT_DATA_WAIVER_FILE", "config/agent_data_encryption_waiver.json"))


def load_waiver(path: Optional[Path] = None) -> Optional[Waiver]:
    """The recorded waiver, or None when there is none or it is incomplete (incomplete means not waived)."""
    p = path or waiver_path()
    try:
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        waiver = Waiver(str(data.get("approved_by", "")), str(data.get("reason", "")), str(data.get("reference", "")),
                        str(data.get("approved_at", "")))
        return waiver if waiver.valid() and data.get("threat_model_reviewed") is True else None
    except (OSError, ValueError, AttributeError):
        return None


def encryption_required() -> bool:
    return load_waiver() is None


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


def get_cipher() -> Optional[Cipher]:
    """The cipher when encryption is on and a key exists; None when encryption is waived; KeyMissing when required but absent."""
    if not encryption_required():
        return None
    key = _load_key()
    if key is None or AESGCM is None:
        raise KeyMissing("encryption is required but no usable AIOPS_AGENT_DATA_KEY / AIOPS_AGENT_DATA_KEY_FILE is available")
    return Cipher(key)
