from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from mcp4chatgpt import local_ops
from mcp4chatgpt.jobs import manager, process, runner
from mcp4chatgpt.jobs.store import TERMINAL_STATES


def make_config(tmp: Path) -> SimpleNamespace:
    root = tmp / "root"
    root.mkdir()
    return SimpleNamespace(
        data_dir=tmp / "data",
        allowed_roots=[root],
        audit_log=tmp / "logs" / "audit.jsonl",
        max_output_chars=10_000,
    )


def wait_terminal(config: SimpleNamespace, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager.job_status(config, job_id)
        if state["state"] in TERMINAL_STATES:
            return state
        time.sleep(0.05)
    pytest.fail(f"job {job_id} did not reach terminal state")


def wait_running(config: SimpleNamespace, job_id: str, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager.job_status(config, job_id)
        if state["state"] == "running":
            return state
        if state["state"] in TERMINAL_STATES:
            return state
        time.sleep(0.05)
    pytest.fail(f"job {job_id} did not start")


def test_start_replay_is_exactly_once_and_logs_use_explicit_cursors() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = make_config(Path(d))
        cwd = str(config.allowed_roots[0])

        first = manager.start_job(
            config,
            operation_id="replay-test-1",
            command="printf abcdef",
            cwd=cwd,
            timeout_sec=10,
        )
        job_id = first["job_id"]
        assert first["launched"] is True
        assert first["replayed"] is False

        with mock.patch.object(
            manager.subprocess,
            "Popen",
            side_effect=AssertionError("replay must not spawn"),
        ):
            replay = manager.start_job(
                config,
                operation_id="replay-test-1",
                command="printf abcdef",
                cwd=cwd,
                timeout_sec=10,
            )
        assert replay["job_id"] == job_id
        assert replay["launched"] is False
        assert replay["replayed"] is True

        terminal = wait_terminal(config, job_id)
        assert terminal["state"] == "succeeded"
        assert terminal["exit_code"] == 0
        assert terminal["process_alive"] is False, terminal

        first_chunk = manager.job_logs(
            config,
            job_id,
            stdout_offset=0,
            stderr_offset=0,
            max_bytes=3,
        )
        assert first_chunk["stdout"]["text"] == "abc"
        assert first_chunk["stdout"]["next_offset"] == 3
        assert first_chunk["stdout"]["eof"] is False

        second_chunk = manager.job_logs(
            config,
            job_id,
            stdout_offset=first_chunk["stdout"]["next_offset"],
            stderr_offset=0,
            max_bytes=3,
        )
        assert second_chunk["stdout"]["text"] == "def"
        assert second_chunk["stdout"]["eof"] is True


def test_reserved_uncertain_start_never_replays_spawn() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = make_config(Path(d))
        cwd = config.allowed_roots[0].resolve()
        operation_id = "reserved-test-1"
        command = "printf never"
        timeout_sec = 10
        shell = os.environ.get("SHELL", "/bin/zsh")
        fingerprint = manager._request_fingerprint(
            command=command,
            cwd=cwd,
            timeout_sec=timeout_sec,
            shell=shell,
        )
        job_id = "job_" + ("a" * 24)
        store = manager._store(config)

        with store.operation_lock(operation_id):
            store.reserve_operation(
                operation_id=operation_id,
                fingerprint=fingerprint,
                job_id=job_id,
            )

        with mock.patch.object(
            manager.subprocess,
            "Popen",
            side_effect=AssertionError("reserved replay must not spawn"),
        ):
            replay = manager.start_job(
                config,
                operation_id=operation_id,
                command=command,
                cwd=str(cwd),
                timeout_sec=timeout_sec,
            )

        assert replay["job_id"] == job_id
        assert replay["state"] == "reserved"
        assert replay["replayed"] is True
        assert replay["launched"] is False
        assert replay["orphaned"] is True
        assert replay["recovery"] == "explicit_repair_required"


def test_operation_id_conflict_never_starts_second_request() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = make_config(Path(d))
        cwd = str(config.allowed_roots[0])

        first = manager.start_job(
            config,
            operation_id="conflict-test-1",
            command="printf first",
            cwd=cwd,
            timeout_sec=10,
        )
        try:
            with mock.patch.object(
                manager.subprocess,
                "Popen",
                side_effect=AssertionError("conflict must not spawn"),
            ):
                with pytest.raises(ValueError, match="idempotency_conflict"):
                    manager.start_job(
                        config,
                        operation_id="conflict-test-1",
                        command="printf second",
                        cwd=cwd,
                        timeout_sec=10,
                    )
        finally:
            wait_terminal(config, first["job_id"])


def test_cancel_terminates_running_job() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = make_config(Path(d))
        cwd = str(config.allowed_roots[0])

        started = manager.start_job(
            config,
            operation_id="cancel-test-1",
            command="sleep 10",
            cwd=cwd,
            timeout_sec=30,
        )
        state = wait_running(config, started["job_id"])
        assert state["state"] == "running"

        cancelled = manager.cancel_job(
            config,
            started["job_id"],
            grace_sec=2.0,
        )
        assert cancelled["state"] == "cancelled", (
            cancelled.get("error"),
            manager.job_logs(config, started["job_id"]),
        )

        terminal = wait_terminal(config, started["job_id"])
        assert terminal["state"] == "cancelled"
        assert terminal["process_alive"] is False


def test_sync_command_rejects_long_timeout_before_spawning() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = make_config(Path(d))
        cwd = str(config.allowed_roots[0])

        with mock.patch.object(
            local_ops,
            "_run_process",
            side_effect=AssertionError("long synchronous command must not spawn"),
        ):
            with pytest.raises(ValueError, match="long_command_requires_job"):
                local_ops.run_command(
                    config,
                    "sleep 1",
                    cwd=cwd,
                    timeout_sec=31,
                )


def test_runner_enforces_job_timeout_independent_of_tool_timeout() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = make_config(Path(d))
        cwd = str(config.allowed_roots[0])

        started = manager.start_job(
            config,
            operation_id="timeout-test-1",
            command="sleep 5",
            cwd=cwd,
            timeout_sec=1,
        )
        terminal = wait_terminal(config, started["job_id"], timeout=5.0)
        assert terminal["state"] == "timed_out", (
            terminal.get("error"),
            manager.job_logs(config, started["job_id"]),
        )
        assert terminal["timeout_sec"] == 1
        assert terminal["process_alive"] is False


def test_cancel_kills_grandchild_that_ignores_sigterm(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess,sys,time,pathlib; "
        "child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)"
    )
    parent = subprocess.Popen([sys.executable, "-c", code, str(pid_file)], start_new_session=True)
    deadline = time.monotonic() + 3
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert pid_file.exists()
    runner._terminate_child(parent, grace_sec=.15)
    assert not process.process_group_alive(parent.pid)


def test_cancel_does_not_signal_supervisor_when_saved_identity_mismatches():
    with tempfile.TemporaryDirectory() as d:
        config = make_config(Path(d))
        started = manager.start_job(
            config, operation_id="stale-identity", command="sleep 5",
            cwd=str(config.allowed_roots[0]), timeout_sec=10,
        )
        wait_running(config, started["job_id"])
        store = manager._store(config)
        store.update_metadata(started["job_id"], {"supervisor_identity": "reused-pid"})
        result = manager.cancel_job(config, started["job_id"])
        assert result["state"] == "unknown"
        assert result["error"] == "process_identity_unverified"
        assert result["cancel_requested"] is False
        # The durable cancellation marker is observed by the live supervisor;
        # this test only forbids signaling an unverified PID.
        terminal = wait_terminal(config, started["job_id"])
        assert terminal["state"] == "cancelled"


def test_signal_process_group_permission_error_falls_back_to_group_leader() -> None:
    with (
        mock.patch.object(
            process.os,
            "killpg",
            side_effect=PermissionError("group signalling denied"),
        ),
        mock.patch.object(process.os, "kill") as kill_pid,
    ):
        assert process.signal_process_group(43210, signal.SIGTERM) is True

    kill_pid.assert_called_once_with(43210, signal.SIGTERM)
