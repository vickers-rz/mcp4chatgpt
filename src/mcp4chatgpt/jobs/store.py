"""Durable on-disk state for local background jobs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..workspace import recovery


_JOB_ID_RE = re.compile(r"^job_[a-f0-9]{24}$")
TERMINAL_STATES = {"succeeded", "failed", "timed_out", "cancelled"}


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.jobs_dir = root / "jobs"
        self.operations_dir = root / "operations"
        self.locks_dir = root / "locks"
        for path in (self.jobs_dir, self.operations_dir, self.locks_dir):
            path.mkdir(parents=True, exist_ok=True)

    def _job_dir(self, job_id: str) -> Path:
        if not _JOB_ID_RE.fullmatch(job_id):
            raise ValueError(f"invalid_job_id: {job_id}")
        return self.jobs_dir / job_id

    def _operation_key(self, operation_id: str) -> str:
        return hashlib.sha256(operation_id.encode("utf-8")).hexdigest()

    def _operation_path(self, operation_id: str) -> Path:
        return self.operations_dir / f"{self._operation_key(operation_id)}.json"

    @contextmanager
    def _lock(self, name: str) -> Iterator[None]:
        path = self.locks_dir / f"{hashlib.sha256(name.encode('utf-8')).hexdigest()}.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def operation_lock(self, operation_id: str):
        return self._lock(f"operation:{operation_id}")

    def job_lock(self, job_id: str):
        return self._lock(f"job:{job_id}")

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"invalid_job_store_object: {path}")
        return value

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
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
        job_id: str,
    ) -> None:
        """Durably bind operation_id before any job process can be spawned."""

        path = self._operation_path(operation_id)
        if path.exists():
            raise ValueError("operation_already_reserved")
        self._write_json(
            path,
            {
                "operation_id": operation_id,
                "fingerprint": fingerprint,
                "job_id": job_id,
                "state": "reserved",
            },
        )

    def create_job(
        self,
        *,
        job_id: str,
        operation_id: str,
        fingerprint: str,
        metadata: dict[str, Any],
        request: dict[str, Any],
    ) -> None:
        # Reservation is intentionally the first durable mutation. If the
        # process crashes after this point, a replay sees the same job_id and
        # must not create or spawn a replacement job implicitly.
        self.reserve_operation(
            operation_id=operation_id,
            fingerprint=fingerprint,
            job_id=job_id,
        )

        job_dir = self._job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=False)
        os.chmod(job_dir, 0o700)
        self._write_json(job_dir / "metadata.json", metadata)
        self._write_json(job_dir / "request.json", request)
        self._write_json(
            self._operation_path(operation_id),
            {
                "operation_id": operation_id,
                "fingerprint": fingerprint,
                "job_id": job_id,
                "state": "prepared",
            },
        )

    def read_request(self, job_id: str) -> dict[str, Any]:
        value = self._read_json(self._job_dir(job_id) / "request.json")
        if value is None:
            raise ValueError(f"job_request_missing: {job_id}")
        return value

    def read_metadata(self, job_id: str) -> dict[str, Any]:
        value = self._read_json(self._job_dir(job_id) / "metadata.json")
        if value is None:
            raise ValueError(f"job_not_found: {job_id}")
        return value

    def update_metadata(
        self,
        job_id: str,
        updates: dict[str, Any],
        *,
        if_states: set[str] | None = None,
    ) -> dict[str, Any]:
        with self.job_lock(job_id):
            metadata = self.read_metadata(job_id)
            if if_states is not None and metadata.get("state") not in if_states:
                return metadata
            metadata.update(updates)
            self._write_json(self._job_dir(job_id) / "metadata.json", metadata)
            return metadata

    def write_supervisor_pid(self, job_id: str, pid: int) -> None:
        recovery.atomic_replace_bytes(
            self._job_dir(job_id) / "supervisor.pid",
            f"{int(pid)}\n".encode("ascii"),
            0o600,
        )

    def read_supervisor_pid(self, job_id: str) -> int | None:
        try:
            text = (self._job_dir(job_id) / "supervisor.pid").read_text(
                encoding="ascii"
            )
        except FileNotFoundError:
            return None
        try:
            return int(text.strip())
        except ValueError:
            return None

    def request_cancel(self, job_id: str) -> None:
        recovery.atomic_replace_bytes(
            self._job_dir(job_id) / "cancel.requested",
            b"1\n",
            0o600,
        )

    def cancel_requested(self, job_id: str) -> bool:
        return (self._job_dir(job_id) / "cancel.requested").exists()

    def log_path(self, job_id: str, stream: str) -> Path:
        if stream not in {"stdout", "stderr"}:
            raise ValueError(f"invalid_log_stream: {stream}")
        return self._job_dir(job_id) / f"{stream}.log"

    def read_log_chunk(
        self,
        job_id: str,
        stream: str,
        *,
        offset: int = 0,
        max_bytes: int = 16_384,
    ) -> dict[str, Any]:
        offset = max(0, int(offset))
        max_bytes = max(1, min(int(max_bytes), 1_000_000))
        path = self.log_path(job_id, stream)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0
        offset = min(offset, size)
        if size == 0:
            raw = b""
        else:
            with path.open("rb") as fh:
                fh.seek(offset)
                raw = fh.read(max_bytes)
        next_offset = offset + len(raw)
        return {
            "stream": stream,
            "offset": offset,
            "next_offset": next_offset,
            "size": size,
            "eof": next_offset >= size,
            "text": raw.decode("utf-8", errors="replace"),
        }

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        items: list[dict[str, Any]] = []
        for path in self.jobs_dir.iterdir():
            if not path.is_dir() or not _JOB_ID_RE.fullmatch(path.name):
                continue
            try:
                metadata = self.read_metadata(path.name)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            items.append(metadata)
        items.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return items[:limit]
