from __future__ import annotations

import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp4chatgpt.jobs import manager, tool_api
from mcp4chatgpt.jobs.store import TERMINAL_STATES


def make_config(tmp: Path) -> SimpleNamespace:
    root_a = tmp / "a"
    root_b = tmp / "b"
    root_a.mkdir()
    root_b.mkdir()
    return SimpleNamespace(
        data_dir=tmp / "data",
        allowed_roots=[root_a, root_b],
        audit_log=tmp / "logs" / "audit.jsonl",
        max_output_chars=10_000,
        local_auth_disabled=False,
        personal_full_access=True,
    )


def wait_terminal(config: SimpleNamespace, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager.job_status(config, job_id)
        if state["state"] in TERMINAL_STATES:
            return state
        time.sleep(0.05)
    pytest.fail(f"job {job_id} did not reach terminal state")


def test_same_scope_replays_and_operation_lookup_recovers_job() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = make_config(Path(d))
        cwd = str(config.allowed_roots[0])
        args = {
            "operation_id": "scoped-replay",
            "command": "printf scoped",
            "cwd": cwd,
            "timeout_sec": 10,
        }

        first = tool_api.start(config, args, "client-a")
        replay = tool_api.start(config, args, "client-a")
        lookup = tool_api.operation_status(
            config,
            {"operation_id": "scoped-replay", "cwd": cwd},
            "client-a",
        )

        assert first["launched"] is True
        assert replay["job_id"] == first["job_id"]
        assert replay["replayed"] is True
        assert lookup["job_id"] == first["job_id"]
        assert lookup["launched"] is False
        wait_terminal(config, first["job_id"])


def test_same_operation_id_is_independent_across_principals() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = make_config(Path(d))
        cwd = str(config.allowed_roots[0])
        args = {
            "operation_id": "shared-name",
            "command": "printf principal",
            "cwd": cwd,
            "timeout_sec": 10,
        }

        first = tool_api.start(config, args, "client-a")
        second = tool_api.start(config, args, "client-b")

        assert first["job_id"] != second["job_id"]
        assert first["scope"]["principal_hash"] != second["scope"]["principal_hash"]
        with pytest.raises(ValueError, match="job_not_found"):
            tool_api.status(config, {"job_id": first["job_id"]}, "client-b")
        with pytest.raises(ValueError, match="job_not_found"):
            tool_api.logs(config, {"job_id": first["job_id"]}, "client-b")
        with pytest.raises(ValueError, match="job_not_found"):
            tool_api.cancel(config, {"job_id": first["job_id"]}, "client-b")
        with pytest.raises(ValueError, match="job_operation_not_found"):
            tool_api.operation_status(
                config,
                {"operation_id": "shared-name", "cwd": cwd},
                "client-c",
            )
        visible_to_b = {
            item["job_id"] for item in tool_api.list_jobs(config, {"limit": 50}, "client-b")["jobs"]
        }
        assert first["job_id"] not in visible_to_b
        assert second["job_id"] in visible_to_b

        wait_terminal(config, first["job_id"])
        wait_terminal(config, second["job_id"])


def test_same_operation_id_is_independent_across_workspaces() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = make_config(Path(d))
        args_a = {
            "operation_id": "workspace-shared",
            "command": "printf workspace",
            "cwd": str(config.allowed_roots[0]),
            "timeout_sec": 10,
        }
        args_b = {**args_a, "cwd": str(config.allowed_roots[1])}

        first = tool_api.start(config, args_a, "client-a")
        second = tool_api.start(config, args_b, "client-a")

        assert first["job_id"] != second["job_id"]
        assert first["scope"]["workspace_id"] != second["scope"]["workspace_id"]

        wait_terminal(config, first["job_id"])
        wait_terminal(config, second["job_id"])


def test_scoped_start_replays_matching_legacy_reservation() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = make_config(Path(d))
        cwd = str(config.allowed_roots[0])

        legacy = manager.start_job(
            config,
            operation_id="legacy-op",
            command="printf legacy",
            cwd=cwd,
            timeout_sec=10,
        )
        scoped = tool_api.start(
            config,
            {
                "operation_id": "legacy-op",
                "command": "printf legacy",
                "cwd": cwd,
                "timeout_sec": 10,
            },
            "client-a",
        )

        assert scoped["job_id"] == legacy["job_id"]
        assert scoped["replayed"] is True
        assert "scope" not in scoped
        wait_terminal(config, legacy["job_id"])


def test_scoped_start_conflicts_with_different_legacy_request() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = make_config(Path(d))
        cwd = str(config.allowed_roots[0])

        legacy = manager.start_job(
            config,
            operation_id="legacy-conflict",
            command="printf first",
            cwd=cwd,
            timeout_sec=10,
        )
        try:
            with pytest.raises(ValueError, match="idempotency_conflict"):
                tool_api.start(
                    config,
                    {
                        "operation_id": "legacy-conflict",
                        "command": "printf second",
                        "cwd": cwd,
                        "timeout_sec": 10,
                    },
                    "client-a",
                )
        finally:
            wait_terminal(config, legacy["job_id"])
