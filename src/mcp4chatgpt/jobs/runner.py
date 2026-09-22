"""Detached supervisor process for one durable local job."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..safety import redact
from . import process
from .store import JobStore, TERMINAL_STATES


_cancel_requested = False


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _handle_signal(_signum: int, _frame: Any) -> None:
    global _cancel_requested
    _cancel_requested = True


def _terminal_update(
    store: JobStore,
    job_id: str,
    *,
    state: str,
    exit_code: int | None,
    started_monotonic: float,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    updates: dict[str, Any] = {
        "state": state,
        "finished_at": _iso_now(),
        "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
        "exit_code": exit_code,
    }
    if extra:
        updates.update(extra)
    return store.update_metadata(
        job_id,
        updates,
        if_states={"prepared", "starting", "running"},
    )


def _terminate_child(
    child: subprocess.Popen[bytes],
    *,
    grace_sec: float = 1.0,
) -> int | None:
    """Terminate the whole child process group and reap the direct child."""

    existing = child.poll()
    if existing is not None:
        return existing

    pgid = child.pid
    if not process.signal_process_group(pgid, signal.SIGTERM):
        try:
            child.terminate()
        except OSError:
            pass

    try:
        return child.wait(timeout=max(0.1, grace_sec))
    except subprocess.TimeoutExpired:
        if not process.signal_process_group(pgid, signal.SIGKILL):
            try:
                child.kill()
            except OSError:
                pass
        try:
            return child.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            return child.poll()


def run_job(store_root: Path, job_id: str) -> int:
    store = JobStore(store_root)
    request = store.read_request(job_id)
    started_monotonic = time.monotonic()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    store.update_metadata(
        job_id,
        {
            "state": "starting",
            "supervisor_pid": os.getpid(),
            "supervisor_pgid": os.getpgrp(),
            "started_at": _iso_now(),
        },
        if_states={"prepared", "starting"},
    )

    if store.cancel_requested(job_id) or _cancel_requested:
        _terminal_update(
            store,
            job_id,
            state="cancelled",
            exit_code=None,
            started_monotonic=started_monotonic,
        )
        return 0

    command = str(request["command"])
    cwd = str(request["cwd"])
    timeout_sec = max(1, int(request["timeout_sec"]))
    shell = str(request.get("shell") or os.environ.get("SHELL", "/bin/zsh"))

    child: subprocess.Popen[bytes] | None = None
    stdout_path = store.log_path(job_id, "stdout")
    stderr_path = store.log_path(job_id, "stderr")

    try:
        with stdout_path.open("ab", buffering=0) as stdout_fh, stderr_path.open(
            "ab",
            buffering=0,
        ) as stderr_fh:
            if store.cancel_requested(job_id) or _cancel_requested:
                _terminal_update(
                    store,
                    job_id,
                    state="cancelled",
                    exit_code=None,
                    started_monotonic=started_monotonic,
                )
                return 0

            child = subprocess.Popen(
                command,
                cwd=cwd,
                shell=True,
                executable=shell,
                stdin=subprocess.DEVNULL,
                stdout=stdout_fh,
                stderr=stderr_fh,
                process_group=0,
            )
            child_pgid = child.pid

            metadata = store.update_metadata(
                job_id,
                {
                    "state": "running",
                    "child_pid": child.pid,
                    "child_pgid": child_pgid,
                },
                if_states={"prepared", "starting"},
            )
            if metadata.get("state") in TERMINAL_STATES:
                _terminate_child(child, grace_sec=0.5)
                return 0

            deadline = time.monotonic() + timeout_sec
            while True:
                exit_code = child.poll()
                if exit_code is not None:
                    state = "succeeded" if exit_code == 0 else "failed"
                    _terminal_update(
                        store,
                        job_id,
                        state=state,
                        exit_code=exit_code,
                        started_monotonic=started_monotonic,
                    )
                    return exit_code

                if _cancel_requested or store.cancel_requested(job_id):
                    exit_code = _terminate_child(child, grace_sec=1.0)
                    _terminal_update(
                        store,
                        job_id,
                        state="cancelled",
                        exit_code=exit_code,
                        started_monotonic=started_monotonic,
                    )
                    return 0

                if time.monotonic() >= deadline:
                    exit_code = _terminate_child(child, grace_sec=1.0)
                    _terminal_update(
                        store,
                        job_id,
                        state="timed_out",
                        exit_code=exit_code,
                        started_monotonic=started_monotonic,
                        extra={"timeout_sec": timeout_sec},
                    )
                    return 124

                time.sleep(0.1)
    except BaseException as exc:
        if child is not None and child.poll() is None:
            _terminate_child(child, grace_sec=0.5)
        current = store.read_metadata(job_id)
        if current.get("state") not in TERMINAL_STATES:
            _terminal_update(
                store,
                job_id,
                state="failed",
                exit_code=child.poll() if child is not None else None,
                started_monotonic=started_monotonic,
                extra={"error": redact(str(exc))},
            )
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-root", required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    return run_job(Path(args.store_root).resolve(), args.job_id)


if __name__ == "__main__":
    raise SystemExit(main())
