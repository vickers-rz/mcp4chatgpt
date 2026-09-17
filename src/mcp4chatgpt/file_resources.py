"""Ephemeral MCP file resources for exposing allowed local files to clients.

A file is published explicitly through a tool call, then read lazily through
``resources/read``. The resource registry is intentionally in-memory: links are
session-scoped, while the underlying file remains protected by MCP_ALLOWED_ROOTS.
"""
from __future__ import annotations

import base64
import hashlib
import mimetypes
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .safety import resolve_allowed_path

_PREFIX = "mcp4chatgpt://files/"
_LOCK = threading.RLock()


@dataclass(frozen=True)
class FileResource:
    token: str
    path: Path
    name: str
    mime_type: str
    size: int
    mtime_ns: int
    sha256: str

    @property
    def uri(self) -> str:
        return f"{_PREFIX}{self.token}"

    def descriptor(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "name": self.name,
            "title": self.name,
            "description": f"Explicitly exposed local file ({self.size} bytes, sha256={self.sha256})",
            "mimeType": self.mime_type,
            "size": self.size,
            "annotations": {
                "lastModified": datetime.fromtimestamp(
                    self.mtime_ns / 1_000_000_000, tz=timezone.utc
                ).isoformat()
            },
        }


_BY_TOKEN: dict[str, FileResource] = {}
_BY_IDENTITY: dict[tuple[str, int, int, str], str] = {}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _mime_type(path: Path) -> str:
    guessed = mimetypes.guess_type(path.name)[0]
    return guessed or "application/octet-stream"


def expose(config: Config, path: str) -> tuple[FileResource, dict[str, Any]]:
    target = resolve_allowed_path(path, config.allowed_roots, must_exist=True)
    if not target.is_file():
        raise ValueError(f"Not a file: {target}")
    stat = target.stat()
    digest = _sha256_file(target)
    identity = (str(target), stat.st_mtime_ns, stat.st_size, digest)
    token = hashlib.sha256("\n".join(map(str, identity)).encode("utf-8")).hexdigest()[:32]
    record = FileResource(
        token=token,
        path=target,
        name=target.name,
        mime_type=_mime_type(target),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=digest,
    )
    with _LOCK:
        existing = _BY_IDENTITY.get(identity)
        if existing and existing in _BY_TOKEN:
            record = _BY_TOKEN[existing]
        else:
            _BY_TOKEN[token] = record
            _BY_IDENTITY[identity] = token

    result = {
        "content": [
            {
                "type": "resource_link",
                "uri": record.uri,
                "name": record.name,
                "title": record.name,
                "description": "Local file exposed by MCP4ChatGPT for direct client-side reading.",
                "mimeType": record.mime_type,
                "size": record.size,
            }
        ],
        "structuredContent": {
            "uri": record.uri,
            "name": record.name,
            "path": str(record.path),
            "mimeType": record.mime_type,
            "size": record.size,
            "sha256": record.sha256,
        },
    }
    return record, result


def list_resources() -> list[dict[str, Any]]:
    with _LOCK:
        stale = [token for token, record in _BY_TOKEN.items() if not record.path.is_file()]
        for token in stale:
            record = _BY_TOKEN.pop(token)
            identity = (str(record.path), record.mtime_ns, record.size, record.sha256)
            _BY_IDENTITY.pop(identity, None)
        records = list(_BY_TOKEN.values())
    records.sort(key=lambda item: item.name.lower())
    return [record.descriptor() for record in records]


def _lookup(uri: str) -> FileResource:
    if not uri.startswith(_PREFIX):
        raise ValueError(f"Unknown file resource: {uri}")
    token = uri.removeprefix(_PREFIX)
    if not token or "/" in token:
        raise ValueError(f"Malformed file resource URI: {uri}")
    with _LOCK:
        record = _BY_TOKEN.get(token)
    if not record:
        raise ValueError("File resource is no longer registered; expose the file again.")
    return record


def read_resource(config: Config, uri: str) -> dict[str, Any]:
    record = _lookup(uri)
    target = resolve_allowed_path(str(record.path), config.allowed_roots, must_exist=True)
    if not target.is_file():
        raise ValueError(f"Not a file: {target}")
    stat = target.stat()
    data = target.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if stat.st_mtime_ns != record.mtime_ns or stat.st_size != record.size or digest != record.sha256:
        raise ValueError("File changed after it was exposed; expose it again to obtain a fresh resource link.")
    return {
        "contents": [
            {
                "uri": record.uri,
                "mimeType": record.mime_type,
                "blob": base64.b64encode(data).decode("ascii"),
            }
        ]
    }


def is_file_resource_uri(uri: str) -> bool:
    return uri.startswith(_PREFIX)
