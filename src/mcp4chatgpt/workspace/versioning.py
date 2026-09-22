"""Version tokens that prove a caller observed a complete file image."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
from pathlib import Path

from . import recovery


_TOKEN_DOMAIN = b"mcp4chatgpt/full-replace/v1\0"


def load_or_create_key(state_dir: Path) -> bytes:
    """Return the stable local key used only for file-replacement tokens."""

    state_dir.mkdir(parents=True, exist_ok=True)
    key_path = state_dir / "mutation-token.key"
    lock_path = state_dir / "mutation-token.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            key = key_path.read_bytes()
        except FileNotFoundError:
            key = os.urandom(32)
            recovery.atomic_replace_bytes(key_path, key, 0o600)
        if len(key) < 32:
            raise ValueError("mutation_token_key_invalid")
        return key
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def full_replace_token(
    key: bytes,
    *,
    target: Path,
    sha256: str,
    size: int,
) -> str:
    """Create a stateless token bound to path, content hash and byte size."""

    if len(key) < 32:
        raise ValueError("mutation_token_key_invalid")
    payload = (
        _TOKEN_DOMAIN
        + str(target.resolve()).encode("utf-8")
        + b"\0"
        + sha256.encode("ascii")
        + b"\0"
        + str(int(size)).encode("ascii")
    )
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_full_replace_token(
    key: bytes,
    token: str | None,
    *,
    target: Path,
    sha256: str,
    size: int,
) -> bool:
    if not token:
        return False
    try:
        expected = full_replace_token(
            key,
            target=target,
            sha256=sha256,
            size=size,
        )
    except ValueError:
        return False
    return hmac.compare_digest(expected, str(token))
