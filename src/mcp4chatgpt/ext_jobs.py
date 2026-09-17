from __future__ import annotations

"""Asynchronous, checkpointed JavaScript jobs for the Chrome Extension channel.

A job is intentionally *not* one long ``run_js`` request.  The caller supplies a
JavaScript function expression that is executed in bounded batches.  Each batch
receives the previous JSON checkpoint and its zero-based batch index and returns
an object with this protocol::

    {
      "done": false,
      "checkpoint": {...},
      "records": [...],
      "progress": 0.25,
      "counters": {"dirs": 10},
      "next_delay_ms": 100
    }

The final batch sets ``done=true`` and may additionally return ``result``.
Records are appended to JSONL and checkpoints/state are persisted after every
batch.  An upstream MCP request therefore only starts/polls/cancels a job; it
does not have to remain open for the lifetime of the work.
"""

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import ext_bridge
from .config import Config
from .safety import redact

log = logging.getLogger(__name__)

TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted"}
ACTIVE_STATES = {"queued", "running"}
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_CODE_CHARS = 200_000
_MAX_BATCH_TIMEOUT_SEC = 25.0
_MAX_BATCHES = 10_000
_MAX_CHECKPOINT_BYTES = 2 * 1024 * 1024
_MAX_RECORD_BYTES = 1 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
_MAX_ERROR_CHARS = 2_000
_DEFAULT_TTL_SEC = 24 * 60 * 60
_MAX_CONCURRENT_JOBS = 2


class ExtJobError(RuntimeError):
    pass


class ExtJobInterrupted(ExtJobError):
    pass


@dataclass
class _JobRuntime:
    job_id: str
    code: str
    tab_id: int
    initial_url: str
    origin: str
    extension_connected_at: float | None
    max_batches: int
    batch_timeout_sec: float
    state: dict[str, Any]
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class ExtensionJobManager:
    def __init__(
        self,
        data_dir: Path,
        *,
        ttl_sec: int = _DEFAULT_TTL_SEC,
        max_concurrent_jobs: int = _MAX_CONCURRENT_JOBS,
    ) -> None:
        self.root = (data_dir / "jobs").expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_sec = max(60, int(ttl_sec))
        self._lock = threading.RLock()
        self._slots = threading.Semaphore(max(1, int(max_concurrent_jobs)))
        self._jobs: dict[str, _JobRuntime] = {}
        self._recover_interrupted_jobs()
        self.cleanup_expired()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_js_job(
        self,
        *,
        code: str,
        tab_id: int | None = None,
        initial_checkpoint: Any = None,
        max_batches: int = 1_000,
        batch_timeout_sec: float = 20.0,
    ) -> dict[str, Any]:
        self.cleanup_expired()
        self._validate_start_args(code, initial_checkpoint, max_batches, batch_timeout_sec)
        identity = self._resolve_identity(tab_id)
        conn = ext_bridge.connection_info()
        if not conn.get("connected"):
            raise ExtJobError("Chrome extension is not connected")

        job_id = uuid.uuid4().hex
        now = time.time()
        state: dict[str, Any] = {
            "job_id": job_id,
            "state": "queued",
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "progress": None,
            "counters": {},
            "batch_index": 0,
            "record_count": 0,
            "error": None,
            "cancel_requested": False,
            "tab_id": identity["tab_id"],
            "origin": identity["origin"],
            "initial_url": identity["url"],
        }
        runtime = _JobRuntime(
            job_id=job_id,
            code=code,
            tab_id=identity["tab_id"],
            initial_url=identity["url"],
            origin=identity["origin"],
            extension_connected_at=conn.get("connected_at"),
            max_batches=int(max_batches),
            batch_timeout_sec=float(batch_timeout_sec),
            state=state,
        )

        with self._lock:
            self._jobs[job_id] = runtime
            self._write_json_atomic(self._state_path(job_id), state)
            self._write_json_atomic(self._checkpoint_path(job_id), initial_checkpoint)

        thread = threading.Thread(
            target=self._run_job,
            args=(runtime,),
            name=f"ext-job-{job_id[:8]}",
            daemon=True,
        )
        runtime.thread = thread
        thread.start()
        return self._public_state(state)

    def get_job(self, job_id: str) -> dict[str, Any]:
        self.cleanup_expired()
        state = self._load_state(job_id)
        return self._public_state(state)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        self.cleanup_expired()
        job_id = self._validate_job_id(job_id)
        with self._lock:
            runtime = self._jobs.get(job_id)
            state = self._load_state_unlocked(job_id)
            if state["state"] in TERMINAL_STATES:
                return self._public_state(state)
            state["cancel_requested"] = True
            self._write_json_atomic(self._state_path(job_id), state)
            if runtime is not None:
                runtime.state = state
                runtime.cancel_event.set()
            elif state["state"] in ACTIVE_STATES:
                # No in-memory worker means this is an orphaned persisted job.
                self._finish_state_unlocked(
                    state,
                    "interrupted",
                    "Job worker is no longer available",
                )
            return self._public_state(state)

    def get_job_result(
        self,
        job_id: str,
        *,
        cursor: int = 0,
        limit: int = 100,
        max_chars: int = 20_000,
    ) -> dict[str, Any]:
        self.cleanup_expired()
        job_id = self._validate_job_id(job_id)
        cursor = max(0, int(cursor))
        limit = max(1, min(int(limit), 200))
        max_chars = max(500, min(int(max_chars), 100_000))
        state = self._load_state(job_id)

        records: list[Any] = []
        used_chars = 0
        next_cursor = cursor
        has_more = False
        records_path = self._records_path(job_id)
        if records_path.exists():
            with records_path.open("r", encoding="utf-8") as fh:
                for idx, line in enumerate(fh):
                    if idx < cursor:
                        continue
                    if len(records) >= limit:
                        has_more = True
                        break
                    line = line.rstrip("\n")
                    if records and used_chars + len(line) > max_chars:
                        has_more = True
                        break
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        record = {"unparsed": line[:max_chars]}
                    records.append(record)
                    used_chars += len(line)
                    next_cursor = idx + 1

        result: dict[str, Any] = {
            "job": self._public_state(state),
            "cursor": cursor,
            "next_cursor": next_cursor if has_more else None,
            "records": records,
            "has_more": has_more,
        }
        if records_path.exists():
            result["records_artifact_path"] = str(records_path)
            result["records_size_bytes"] = records_path.stat().st_size

        result_path = self._result_path(job_id)
        if result_path.exists():
            size = result_path.stat().st_size
            result["result_artifact_path"] = str(result_path)
            result["result_size_bytes"] = size
            text = result_path.read_text(encoding="utf-8", errors="replace")
            if len(text) <= max_chars:
                try:
                    result["result"] = json.loads(text)
                except json.JSONDecodeError:
                    result["result"] = text
                result["result_truncated"] = False
            else:
                result["result_preview"] = text[:max_chars]
                result["result_truncated"] = True
        return result

    def cleanup_expired(self) -> int:
        now = time.time()
        removed = 0
        for child in list(self.root.iterdir()):
            if not child.is_dir() or not _JOB_ID_RE.fullmatch(child.name):
                continue
            state_path = child / "state.json"
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if state.get("state") not in TERMINAL_STATES:
                continue
            finished_at = float(state.get("finished_at") or state.get("created_at") or now)
            if now - finished_at <= self.ttl_sec:
                continue
            self._remove_tree(child)
            with self._lock:
                self._jobs.pop(child.name, None)
            removed += 1
        return removed

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _run_job(self, runtime: _JobRuntime) -> None:
        acquired = self._slots.acquire(timeout=60)
        if not acquired:
            self._finish(runtime, "failed", "Timed out waiting for a job worker slot")
            return
        try:
            if runtime.cancel_event.is_set():
                self._finish(runtime, "cancelled")
                return
            self._update(runtime, state="running", started_at=time.time())
            checkpoint = self._read_json(self._checkpoint_path(runtime.job_id))

            for batch_index in range(runtime.max_batches):
                if runtime.cancel_event.is_set():
                    self._finish(runtime, "cancelled")
                    return
                self._assert_bound_context(runtime)

                wrapped_code = self._wrap_batch_code(runtime.code, checkpoint, batch_index)
                try:
                    raw = ext_bridge.send_command(
                        "run_js",
                        {"code": wrapped_code, "tabId": runtime.tab_id},
                        timeout=runtime.batch_timeout_sec,
                    )
                except Exception as exc:
                    if runtime.cancel_event.is_set():
                        self._finish(runtime, "cancelled")
                        return
                    if not ext_bridge.is_connected():
                        raise ExtJobInterrupted("Extension disconnected") from exc
                    raise

                if runtime.cancel_event.is_set():
                    self._finish(runtime, "cancelled")
                    return
                payload = self._decode_batch_result(raw)
                if payload.get("error"):
                    raise ExtJobError(str(payload["error"]))

                records = payload.get("records") or []
                if not isinstance(records, list):
                    raise ExtJobError("Batch result 'records' must be an array")
                self._append_records(runtime.job_id, records)

                if "checkpoint" in payload:
                    checkpoint = payload["checkpoint"]
                    self._validate_json_size(checkpoint, _MAX_CHECKPOINT_BYTES, "checkpoint")
                    self._write_json_atomic(self._checkpoint_path(runtime.job_id), checkpoint)

                progress = payload.get("progress")
                if progress is not None:
                    try:
                        progress = max(0.0, min(float(progress), 1.0))
                    except (TypeError, ValueError):
                        raise ExtJobError("Batch result 'progress' must be numeric")
                counters = payload.get("counters")
                if counters is not None and not isinstance(counters, dict):
                    raise ExtJobError("Batch result 'counters' must be an object")

                with self._lock:
                    current = runtime.state
                    current["batch_index"] = batch_index + 1
                    current["record_count"] = int(current.get("record_count") or 0) + len(records)
                    if progress is not None:
                        current["progress"] = progress
                    if counters is not None:
                        current["counters"] = counters
                    self._write_json_atomic(self._state_path(runtime.job_id), current)

                if bool(payload.get("done", False)):
                    if "result" in payload:
                        self._validate_json_size(payload["result"], _MAX_ARTIFACT_BYTES, "result")
                        self._write_json_atomic(self._result_path(runtime.job_id), payload["result"])
                    self._finish(runtime, "completed")
                    return

                delay_ms = payload.get("next_delay_ms", 0)
                try:
                    delay_ms = max(0, min(int(delay_ms), 5_000))
                except (TypeError, ValueError):
                    raise ExtJobError("Batch result 'next_delay_ms' must be an integer")
                if delay_ms:
                    if runtime.cancel_event.wait(delay_ms / 1000.0):
                        self._finish(runtime, "cancelled")
                        return

            self._finish(runtime, "failed", f"Job exceeded max_batches={runtime.max_batches}")
        except ExtJobInterrupted as exc:
            self._finish(runtime, "interrupted", str(exc))
        except Exception as exc:
            self._finish(runtime, "failed", str(exc))
        finally:
            self._slots.release()

    # ------------------------------------------------------------------
    # Context binding / protocol
    # ------------------------------------------------------------------

    def _resolve_identity(self, tab_id: int | None) -> dict[str, Any]:
        if not ext_bridge.is_connected():
            raise ExtJobError("Chrome extension is not connected")
        if tab_id is None:
            result = ext_bridge.send_command(
                "get_active_tab",
                {"includeText": False, "includeSelection": False, "includeMeta": False},
                timeout=10,
            )
        else:
            result = ext_bridge.send_command("get_selection", {"tabId": int(tab_id)}, timeout=10)
        resolved_tab = result.get("tabId")
        url = str(result.get("url") or "")
        if resolved_tab is None or not url:
            raise ExtJobError("Could not resolve target tab identity")
        return {"tab_id": int(resolved_tab), "url": url, "origin": self._origin_key(url)}

    def _assert_bound_context(self, runtime: _JobRuntime) -> None:
        conn = ext_bridge.connection_info()
        if not conn.get("connected"):
            raise ExtJobInterrupted("Extension disconnected")
        if conn.get("connected_at") != runtime.extension_connected_at:
            raise ExtJobInterrupted("Extension session changed")
        try:
            identity = self._resolve_identity(runtime.tab_id)
        except Exception as exc:
            raise ExtJobInterrupted(f"Target tab is unavailable: {exc}") from exc
        if identity["tab_id"] != runtime.tab_id:
            raise ExtJobInterrupted("Target tab identity changed")
        if identity["origin"] != runtime.origin:
            raise ExtJobInterrupted(
                f"Target tab origin changed from {runtime.origin!r} to {identity['origin']!r}"
            )
        # Non-web URLs have opaque/special origins; require exact URL stability.
        if not runtime.initial_url.startswith(("http://", "https://")) and identity["url"] != runtime.initial_url:
            raise ExtJobInterrupted("Target tab URL changed for a non-web origin")

    @staticmethod
    def _origin_key(url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"}:
            return f"{parsed.scheme}://{parsed.netloc.lower()}"
        return f"{parsed.scheme}:{parsed.netloc}"

    @staticmethod
    def _wrap_batch_code(code: str, checkpoint: Any, batch_index: int) -> str:
        checkpoint_json = json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":"))
        return (
            "(async () => {"
            f"const __mcpJobFn = ({code});"
            f"return await __mcpJobFn({checkpoint_json}, {batch_index});"
            "})()"
        )

    @staticmethod
    def _decode_batch_result(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ExtJobError("Extension returned a non-object run_js response")
        if raw.get("error"):
            raise ExtJobError(str(raw.get("error")))
        value = raw.get("result")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ExtJobError("Batch JavaScript must return a JSON object") from exc
        if not isinstance(value, dict):
            raise ExtJobError("Batch JavaScript must return an object")
        return value

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _recover_interrupted_jobs(self) -> None:
        for child in list(self.root.iterdir()):
            if not child.is_dir() or not _JOB_ID_RE.fullmatch(child.name):
                continue
            state_path = child / "state.json"
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if state.get("state") in ACTIVE_STATES:
                self._finish_state_unlocked(
                    state,
                    "interrupted",
                    "Service restarted before job completed; automatic resume is disabled",
                )

    def _load_state(self, job_id: str) -> dict[str, Any]:
        job_id = self._validate_job_id(job_id)
        with self._lock:
            return self._load_state_unlocked(job_id)

    def _load_state_unlocked(self, job_id: str) -> dict[str, Any]:
        path = self._state_path(job_id)
        if not path.exists():
            raise ExtJobError(f"Unknown job_id: {job_id}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ExtJobError(f"Job state is unreadable: {job_id}") from exc
        if not isinstance(data, dict):
            raise ExtJobError(f"Job state is invalid: {job_id}")
        return data

    def _update(self, runtime: _JobRuntime, **fields: Any) -> None:
        with self._lock:
            runtime.state.update(fields)
            self._write_json_atomic(self._state_path(runtime.job_id), runtime.state)

    def _finish(self, runtime: _JobRuntime, state_name: str, error: str | None = None) -> None:
        with self._lock:
            self._finish_state_unlocked(runtime.state, state_name, error)
            self._jobs.pop(runtime.job_id, None)

    def _finish_state_unlocked(
        self,
        state: dict[str, Any],
        state_name: str,
        error: str | None = None,
    ) -> None:
        state["state"] = state_name
        state["finished_at"] = time.time()
        state["error"] = self._bounded_error(error)
        if state_name == "completed" and state.get("progress") is None:
            state["progress"] = 1.0
        self._write_json_atomic(self._state_path(str(state["job_id"])), state)

    def _append_records(self, job_id: str, records: list[Any]) -> None:
        if not records:
            return
        path = self._records_path(job_id)
        current_size = path.stat().st_size if path.exists() else 0
        encoded: list[str] = []
        for record in records:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            size = len(line.encode("utf-8"))
            if size > _MAX_RECORD_BYTES:
                raise ExtJobError(f"A job record exceeds {_MAX_RECORD_BYTES} bytes")
            current_size += size + 1
            if current_size > _MAX_ARTIFACT_BYTES:
                raise ExtJobError(f"Job records exceed {_MAX_ARTIFACT_BYTES} bytes")
            encoded.append(line)
        with path.open("a", encoding="utf-8") as fh:
            for line in encoded:
                fh.write(line + "\n")

    def _job_dir(self, job_id: str) -> Path:
        job_id = self._validate_job_id(job_id)
        path = (self.root / job_id).resolve()
        if path.parent != self.root:
            raise ExtJobError("Unsafe job path")
        return path

    def _state_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "state.json"

    def _checkpoint_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "checkpoint.json"

    def _records_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "records.jsonl"

    def _result_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "result.json"

    @staticmethod
    def _write_json_atomic(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp.replace(path)

    @staticmethod
    def _read_json(path: Path) -> Any:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _remove_tree(path: Path) -> None:
        for child in path.iterdir():
            if child.is_dir():
                ExtensionJobManager._remove_tree(child)
            else:
                child.unlink(missing_ok=True)
        path.rmdir()

    # ------------------------------------------------------------------
    # Validation / public representation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_job_id(job_id: str) -> str:
        job_id = str(job_id)
        if not _JOB_ID_RE.fullmatch(job_id):
            raise ExtJobError("Invalid job_id")
        return job_id

    @staticmethod
    def _validate_json_size(value: Any, maximum: int, label: str) -> None:
        try:
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ExtJobError(f"{label} must be JSON-serializable") from exc
        if len(payload) > maximum:
            raise ExtJobError(f"{label} exceeds {maximum} bytes")

    def _validate_start_args(
        self,
        code: str,
        initial_checkpoint: Any,
        max_batches: int,
        batch_timeout_sec: float,
    ) -> None:
        if not ext_bridge.is_connected():
            raise ExtJobError("Chrome extension is not connected")
        if not code or not code.strip():
            raise ValueError("JS job code cannot be empty")
        if len(code) > _MAX_CODE_CHARS:
            raise ValueError(f"JS job code is too long (max {_MAX_CODE_CHARS} chars)")
        if not 1 <= int(max_batches) <= _MAX_BATCHES:
            raise ValueError(f"max_batches must be between 1 and {_MAX_BATCHES}")
        if not 1.0 <= float(batch_timeout_sec) <= _MAX_BATCH_TIMEOUT_SEC:
            raise ValueError(
                f"batch_timeout_sec must be between 1 and {_MAX_BATCH_TIMEOUT_SEC:g}"
            )
        self._validate_json_size(initial_checkpoint, _MAX_CHECKPOINT_BYTES, "initial_checkpoint")

    @staticmethod
    def _bounded_error(error: str | None) -> str | None:
        if error is None:
            return None
        return str(error)[:_MAX_ERROR_CHARS]

    @staticmethod
    def _public_state(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": state.get("job_id"),
            "state": state.get("state"),
            "created_at": state.get("created_at"),
            "started_at": state.get("started_at"),
            "finished_at": state.get("finished_at"),
            "progress": state.get("progress"),
            "counters": state.get("counters") or {},
            "batch_index": state.get("batch_index", 0),
            "record_count": state.get("record_count", 0),
            "error": state.get("error"),
            "cancel_requested": bool(state.get("cancel_requested", False)),
            "tab_id": state.get("tab_id"),
            "origin": state.get("origin"),
            "initial_url": redact(str(state.get("initial_url") or "")),
        }


_MANAGERS: dict[Path, ExtensionJobManager] = {}
_MANAGERS_LOCK = threading.Lock()


def get_job_manager(config: Config) -> ExtensionJobManager:
    key = config.data_dir.expanduser().resolve()
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(key)
        if manager is None:
            manager = ExtensionJobManager(key)
            _MANAGERS[key] = manager
        return manager


def _reset_job_managers_for_tests() -> None:
    with _MANAGERS_LOCK:
        _MANAGERS.clear()
