"""Encrypted PII mapping I/O.

Uses Fernet (AES-128-CBC + HMAC-SHA256) for authenticated encryption.
Key derived from document hash via HKDF (SHA-256, 32-byte key).

File format (pii_map.enc):
  First 16 bytes: HKDF salt (used to derive the Fernet key)
  Remaining bytes: Fernet token (encrypted JSON)
"""

import contextlib
import json
import os
import secrets
from pathlib import Path
from typing import Any, cast

from openreview_cli.pii.encryption import (
    InvalidToken,
    decrypt_pii_mapping,
    derive_key,
    encrypt_pii_mapping,
)

SALT_LENGTH = 16


def write_pii_mapping(
    mapping: dict[str, str],
    review_dir: Path,
    encryption_key: str,
) -> Path:
    review_dir.mkdir(parents=True, exist_ok=True)

    salt = secrets.token_bytes(SALT_LENGTH)
    fernet = derive_key(encryption_key, salt)

    payload = {"version": 2, "entries": mapping}
    plaintext = json.dumps(payload, sort_keys=True).encode("utf-8")
    token = encrypt_pii_mapping(plaintext, fernet)

    path = review_dir / "pii_map.enc"
    tmp_path = review_dir / f".pii_map.enc.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(salt + token)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
    return path


def read_pii_mapping(
    review_dir: Path,
    encryption_key: str,
) -> dict[str, str]:
    path = review_dir / "pii_map.enc"
    if not path.exists():
        raise FileNotFoundError(f"PII mapping not found: {path}")

    data = path.read_bytes()
    salt = data[:SALT_LENGTH]
    token = data[SALT_LENGTH:]

    fernet = derive_key(encryption_key, salt)
    decrypted = decrypt_pii_mapping(token, fernet)
    payload: dict[str, Any] = json.loads(decrypted.decode("utf-8"))
    return cast("dict[str, str]", payload["entries"])


def ensure_encryption_key(config: dict[str, Any], config_path: Path) -> str:
    if "privacy" in config and "pii_encryption_key" in config["privacy"]:
        return str(config["privacy"]["pii_encryption_key"])
    key = secrets.token_urlsafe(32)[:32]
    from openreview_cli.config.loader import set_config_value

    set_config_value(config_path, "privacy.pii_encryption_key", key)
    return key


def delete_pii_mapping(review_dir: Path) -> None:
    for name in ("pii_map.enc", "pii_audit.json"):
        path = review_dir / name
        if path.exists():
            path.unlink()


__all__ = [
    "InvalidToken",
    "delete_pii_mapping",
    "ensure_encryption_key",
    "read_pii_mapping",
    "write_pii_mapping",
]
