from __future__ import annotations

import json
import gzip
import shutil
import time
import os
import fcntl
import sys
import threading
import tempfile
from pathlib import Path
from typing import Any

_AUDIT_LOCK = threading.RLock()

class AuditLogger:
    def __init__(self, path: Path, rotate_bytes: int = 20 * 1024 * 1024, retention_days: int = 30):
        self.path = path
        self.rotate_bytes = max(1024 * 1024, rotate_bytes)
        self.retention_days = max(1, retention_days)

    def log(self, event: str, **fields: Any) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with _AUDIT_LOCK:
                fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    self._rotate_if_needed()
                    self._cleanup_old_archives()
                    record = {"ts": time.time(), "event": event, **fields}
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                finally:
                    os.close(fd)
        except OSError as exc:
            print(f"audit failure at {self.path}: {type(exc).__name__}", file=sys.stderr)

    def _rotate_if_needed(self) -> None:
        if not self.path.exists():
            return
        stat = self.path.stat()
        if stat.st_size <= 0:
            return

        current_day = time.strftime("%Y-%m-%d")
        file_day = time.strftime("%Y-%m-%d", time.localtime(stat.st_mtime))
        if stat.st_size < self.rotate_bytes and file_day == current_day:
            return

        stamp = time.strftime("%Y-%m-%d-%H%M%S", time.localtime(stat.st_mtime))
        rotated = self.path.with_name(f"{self.path.stem}.{stamp}{self.path.suffix}")
        counter = 1
        while rotated.exists() or rotated.with_suffix(rotated.suffix + ".gz").exists():
            rotated = self.path.with_name(f"{self.path.stem}.{stamp}.{counter}{self.path.suffix}")
            counter += 1

        self.path.rename(rotated)
        self._gzip_file(rotated)

    def _gzip_file(self, path: Path) -> None:
        gz_path = path.with_suffix(path.suffix + ".gz")
        fd, name = tempfile.mkstemp(prefix=".audit-", dir=path.parent)
        os.close(fd)
        try:
            with path.open("rb") as src, gzip.open(name, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.replace(name, gz_path)
            path.unlink(missing_ok=True)
        finally:
            Path(name).unlink(missing_ok=True)

    def _cleanup_old_archives(self) -> None:
        cutoff = time.time() - self.retention_days * 86400
        for path in self.path.parent.glob(f"{self.path.stem}.*{self.path.suffix}.gz"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
