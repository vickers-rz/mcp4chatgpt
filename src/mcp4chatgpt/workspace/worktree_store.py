"""Durable metadata for managed Git worktrees."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import recovery


_WORKTREE_ID_RE = re.compile(r"^wt_[a-f0-9]{24}$")


class WorktreeStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.records_dir = root / "records"
        self.operations_dir = root / "operations"
        self.locks_dir = root / "locks"
        for path in (self.records_dir, self.operations_dir, self.locks_dir):
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def validate_worktree_id(worktree_id: str) -> str:
        value = str(worktree_id)
        if not _WORKTREE_ID_RE.fullmatch(value):
            raise ValueError(f"invalid_worktree_id: {value}")
        return value

    @staticmethod
    def _operation_key(operation_id: str) -> str:
        return hashlib.sha256(operation_id.encode("utf-8")).hexdigest()

    def _record_path(self, worktree_id: str) -> Path:
        return self.records_dir / f"{self.validate_worktree_id(worktree_id)}.json"

    def _operation_path(self, operation_id: str) -> Path:
        return self.operations_dir / f"{self._operation_key(operation_id)}.json"

    @contextmanager
    def _lock(self, namespace: str, value: str) -> Iterator[None]:
        digest = hashlib.sha256(f"{namespace}:{value}".encode("utf-8")).hexdigest()
        fd = os.open(self.locks_dir / f"{digest}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def operation_lock(self, operation_id: str):
        return self._lock("operation", operation_id)

    def worktree_lock(self, worktree_id: str):
        return self._lock("worktree", self.validate_worktree_id(worktree_id))

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(value, dict):
            raise ValueError(f"invalid_worktree_store_object: {path}")
        return value

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        payload = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        recovery.atomic_replace_bytes(path, payload, 0o600)

    def read_operation(self, operation_id: str) -> dict[str, Any] | None:
        return self._read_json(self._operation_path(operation_id))

    def reserve_operation(
        self,
        *,
        operation_id: str,
        fingerprint: str,
        worktree_id: str,
        source_repo: Path,
        base_commit: str,
        destination: Path,
    ) -> dict[str, Any]:
        path = self._operation_path(operation_id)
        if path.exists():
            raise ValueError("worktree_operation_already_reserved")
        value = {
            "operation_id": operation_id,
            "fingerprint": fingerprint,
            "worktree_id": self.validate_worktree_id(worktree_id),
            "source_repo": str(source_repo),
            "base_commit": base_commit,
            "destination": str(destination),
            "state": "reserved",
        }
        self._write_json(path, value)
        return value

    def update_operation(
        self,
        operation_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        value = self.read_operation(operation_id)
        if value is None:
            raise ValueError(f"worktree_operation_not_found: {operation_id}")
        value.update(updates)
        self._write_json(self._operation_path(operation_id), value)
        return value

    def read_record(self, worktree_id: str) -> dict[str, Any]:
        value = self._read_json(self._record_path(worktree_id))
        if value is None:
            raise ValueError(f"worktree_not_found: {worktree_id}")
        return value

    def write_record(self, record: dict[str, Any]) -> dict[str, Any]:
        worktree_id = self.validate_worktree_id(str(record["worktree_id"]))
        self._write_json(self._record_path(worktree_id), record)
        return record

    def update_record(
        self,
        worktree_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        with self.worktree_lock(worktree_id):
            value = self.read_record(worktree_id)
            value.update(updates)
            self._write_json(self._record_path(worktree_id), value)
            return value

    def list_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in self.records_dir.glob("wt_*.json"):
            try:
                value = self._read_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if value is not None:
                records.append(value)
        records.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return records
