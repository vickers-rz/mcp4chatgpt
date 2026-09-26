"""Idempotent durable local job manager."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Config
from ..safety import redact, resolve_allowed_path, validate_command
from . import process
from .store import JobStore, TERMINAL_STATES


_ACTIVE_STATES = {"prepared", "starting", "running"}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store(config: Config) -> JobStore:
    return JobStore(Path(config.data_dir) / "local-jobs")


def _validate_operation_id(operation_id: str) -> str:
    value = str(operation_id).strip()
    if not value:
        raise ValueError("operation_id_required")
    if len(value) > 128:
        raise ValueError("operation_id_too_long")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("operation_id_contains_control_character")
    return value


def _clamp_timeout(timeout_sec: int) -> int:
    return max(1, min(int(timeout_sec), 86_400))


def _request_fingerprint(
    *,
    command: str,
    cwd: Path,
    timeout_sec: int,
    shell: str,
) -> str:
    payload = json.dumps(
        {
            "command": command,
            "cwd": str(cwd),
            "timeout_sec": timeout_sec,
            "shell": shell,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _augment_status(store: JobStore, metadata: dict[str, Any]) -> dict[str, Any]:
    result = dict(metadata)
    state = str(result.get("state", "unknown"))

    supervisor_pid = result.get("supervisor_pid")
    if supervisor_pid is None:
        supervisor_pid = store.read_supervisor_pid(str(result["job_id"]))
        if supervisor_pid is not None:
            result["supervisor_pid"] = supervisor_pid

    child_pgid = result.get("child_pgid")
    if state not in TERMINAL_STATES:
        expected = result.get("supervisor_identity")
        if expected is None:
            result["state"] = "unknown"
            result["process_identity_verified"] = False
            result["supervisor_alive"] = False
            result["child_alive"] = False
            result["process_alive"] = False
            return result
        result["process_identity_verified"] = None
    if state in TERMINAL_STATES:
        # Terminal metadata is authoritative. Detached supervisors may remain
        # briefly visible as zombies until their parent reaps them; reporting
        # those PIDs as a live job would be a semantic false positive.
        supervisor_alive = False
        child_alive = bool(result.get("cleanup_incomplete") and process.process_group_alive(
            int(child_pgid) if child_pgid is not None else None
        ))
    else:
        supervisor_alive = process.pid_alive(
            int(supervisor_pid) if supervisor_pid is not None else None
        )
        child_alive = process.process_group_alive(
            int(child_pgid) if child_pgid is not None else None
        )

    result["supervisor_alive"] = supervisor_alive
    result["child_alive"] = child_alive
    result["process_alive"] = supervisor_alive or child_alive
    result["orphaned"] = state in _ACTIVE_STATES and not result["process_alive"]

    for stream in ("stdout", "stderr"):
        try:
            size = store.log_path(str(result["job_id"]), stream).stat().st_size
        except FileNotFoundError:
            size = 0
        result[f"{stream}_bytes"] = size

    return result


def start_job(
    config: Config,
    *,
    operation_id: str,
    command: str,
    cwd: str | None = None,
    timeout_sec: int = 900,
) -> dict[str, Any]:
    """Start once for one operation_id/fingerprint pair.

    If the caller retries an uncertain start with the same request, the existing
    job is returned and no second process is spawned.
    """

    operation_id = _validate_operation_id(operation_id)
    command = validate_command(command)
    resolved_cwd = resolve_allowed_path(
        cwd or ".",
        config.allowed_roots,
        must_exist=True,
    )
    if not resolved_cwd.is_dir():
        raise ValueError(f"Not a directory: {resolved_cwd}")

    timeout_sec = _clamp_timeout(timeout_sec)
    shell = os.environ.get("SHELL", "/bin/zsh")
    fingerprint = _request_fingerprint(
        command=command,
        cwd=resolved_cwd,
        timeout_sec=timeout_sec,
        shell=shell,
    )
    store = _store(config)

    with store.operation_lock(operation_id):
        existing = store.read_operation(operation_id)
        if existing is not None:
            if existing.get("fingerprint") != fingerprint:
                raise ValueError(
                    "idempotency_conflict: operation_id already belongs to "
                    "a different job request"
                )

            existing_job_id = str(existing["job_id"])
            try:
                metadata = store.read_metadata(existing_job_id)
            except ValueError as exc:
                if not str(exc).startswith("job_not_found:"):
                    raise
                # A crash may have happened after the durable operation
                # reservation but before job metadata was committed. Treat
                # that as an uncertain start and never spawn implicitly.
                return {
                    "schema_version": 1,
                    "job_id": existing_job_id,
                    "operation_id": operation_id,
                    "fingerprint": fingerprint,
                    "state": "reserved",
                    "operation_state": existing.get("state", "reserved"),
                    "replayed": True,
                    "launched": False,
                    "supervisor_alive": False,
                    "child_alive": False,
                    "process_alive": False,
                    "orphaned": True,
                    "recovery": "explicit_repair_required",
                }

            return {
                **_augment_status(store, metadata),
                "replayed": True,
                "launched": False,
            }

        job_id = "job_" + uuid.uuid4().hex[:24]
        created_at = _iso_now()
        metadata: dict[str, Any] = {
            "schema_version": 1,
            "job_id": job_id,
            "operation_id": operation_id,
            "fingerprint": fingerprint,
            "state": "prepared",
            "command": redact(command),
            "cwd": str(resolved_cwd),
            "timeout_sec": timeout_sec,
            "created_at": created_at,
        }
        request = {
            "command": command,
            "cwd": str(resolved_cwd),
            "timeout_sec": timeout_sec,
            "shell": shell,
        }
        store.create_job(
            job_id=job_id,
            operation_id=operation_id,
            fingerprint=fingerprint,
            metadata=metadata,
            request=request,
        )

        runner_env = os.environ.copy()
        src_root = str(Path(__file__).resolve().parents[2])
        inherited_pythonpath = runner_env.get("PYTHONPATH", "")
        runner_env["PYTHONPATH"] = (
            src_root
            if not inherited_pythonpath
            else src_root + os.pathsep + inherited_pythonpath
        )

        try:
            supervisor_stderr_path = store.log_path(job_id, "stderr")
            with supervisor_stderr_path.open("ab", buffering=0) as supervisor_stderr:
                supervisor = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "mcp4chatgpt.jobs.runner",
                        "--store-root",
                        str(store.root),
                        "--job-id",
                        job_id,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=supervisor_stderr,
                    start_new_session=True,
                    close_fds=True,
                    env=runner_env,
                )
        except Exception as exc:
            metadata = store.update_metadata(
                job_id,
                {
                    "state": "failed",
                    "finished_at": _iso_now(),
                    "error": redact(str(exc)),
                },
                if_states={"prepared"},
            )
            raise RuntimeError(f"job_supervisor_start_failed: {job_id}") from exc

        # This PID file is an emergency cancellation/recovery handle. The
        # supervisor also records its own PID in metadata as soon as it starts.
        store.write_supervisor_pid(job_id, supervisor.pid)
        metadata = store.update_metadata(
            job_id,
            {
                "supervisor_pid": supervisor.pid,
                "supervisor_identity": process.process_identity(supervisor.pid),
            },
        )

    return {
        **_augment_status(store, metadata),
        "replayed": False,
        "launched": True,
    }


def job_status(config: Config, job_id: str) -> dict[str, Any]:
    """Observe one job without launching, retrying, or cancelling anything."""

    store = _store(config)
    return _augment_status(store, store.read_metadata(job_id))


def job_logs(
    config: Config,
    job_id: str,
    *,
    stdout_offset: int = 0,
    stderr_offset: int = 0,
    max_bytes: int = 16_384,
) -> dict[str, Any]:
    """Read retained logs from explicit cursors without mutating job state."""

    store = _store(config)
    metadata = store.read_metadata(job_id)
    return {
        "job_id": job_id,
        "state": metadata.get("state"),
        "stdout": store.read_log_chunk(
            job_id,
            "stdout",
            offset=stdout_offset,
            max_bytes=max_bytes,
        ),
        "stderr": store.read_log_chunk(
            job_id,
            "stderr",
            offset=stderr_offset,
            max_bytes=max_bytes,
        ),
    }


def list_jobs(config: Config, limit: int = 50) -> dict[str, Any]:
    store = _store(config)
    return {
        "jobs": [
            _augment_status(store, metadata)
            for metadata in store.list_jobs(limit)
        ]
    }


def cancel_job(
    config: Config,
    job_id: str,
    *,
    grace_sec: float = 2.0,
) -> dict[str, Any]:
    """Explicitly request cancellation; status/log observation never calls this."""

    store = _store(config)
    metadata = store.read_metadata(job_id)
    if metadata.get("state") in TERMINAL_STATES:
        return {
            **_augment_status(store, metadata),
            "cancel_requested": False,
            "already_terminal": True,
        }

    store.request_cancel(job_id)
    metadata = store.update_metadata(
        job_id,
        {"cancel_requested_at": _iso_now()},
        if_states=_ACTIVE_STATES,
    )

    supervisor_pid = metadata.get("supervisor_pid")
    if supervisor_pid is None:
        supervisor_pid = store.read_supervisor_pid(job_id)

    expected_identity = metadata.get("supervisor_identity")
    if (
        supervisor_pid is None
        or expected_identity is None
        or process.process_identity(int(supervisor_pid)) != expected_identity
    ):
        return {**_augment_status(store, metadata), "state": "unknown", "error": "process_identity_unverified", "cancel_requested": False}

    # The supervisor handles SIGTERM by terminating its child process group and
    # writing a terminal cancelled state.
    process.signal_pid(
        int(supervisor_pid) if supervisor_pid is not None else None,
        signal.SIGTERM,
    )

    deadline = time.monotonic() + max(0.0, grace_sec)
    while time.monotonic() < deadline:
        current = store.read_metadata(job_id)
        if current.get("state") in TERMINAL_STATES:
            return {
                **_augment_status(store, current),
                "cancel_requested": True,
                "already_terminal": False,
            }
        time.sleep(0.05)

    current = store.read_metadata(job_id)
    child_pgid = current.get("child_pgid")
    if child_pgid is not None:
        child_identity = current.get("child_identity")
        if child_identity is None or process.process_identity(int(child_pgid)) != child_identity:
            return {**_augment_status(store, current), "state": "unknown", "error": "child_identity_unverified", "cancel_requested": True}
        process.terminate_process_group(int(child_pgid), grace_sec=0.5)
    if supervisor_pid is not None and process.process_identity(int(supervisor_pid)) == expected_identity:
        process.signal_pid(int(supervisor_pid), signal.SIGKILL)

    current = store.update_metadata(
        job_id,
        {
            "state": "cancelled",
            "finished_at": _iso_now(),
            "cancel_forced": True,
        },
        if_states=_ACTIVE_STATES,
    )
    return {
        **_augment_status(store, current),
        "cancel_requested": True,
        "already_terminal": False,
    }
