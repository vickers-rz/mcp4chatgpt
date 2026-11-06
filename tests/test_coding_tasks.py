from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp4chatgpt.audit import AuditLogger
from mcp4chatgpt.tools import ToolRegistry
from mcp4chatgpt.workspace import coding_task_tool_api
from mcp4chatgpt.workspace.worktree_store import WorktreeStore


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    _git(path, "init")
    _git(path, "config", "user.name", "Tester")
    _git(path, "config", "user.email", "tester@example.com")
    (path / "app.txt").write_text("initial\n", encoding="utf-8")
    (path / "other.txt").write_text("other\n", encoding="utf-8")
    _git(path, "add", "app.txt", "other.txt")
    _git(path, "commit", "-m", "initial")
    return path


def _config(tmp: Path) -> SimpleNamespace:
    root = tmp / "root"
    root.mkdir()
    repo = root / "repo"
    repo.mkdir()
    _init_repo(repo)
    return SimpleNamespace(
        data_dir=tmp / "data",
        allowed_roots=[root],
        audit_log=tmp / "logs" / "audit.jsonl",
        max_output_chars=100_000,
        local_auth_disabled=False,
        personal_full_access=True,
        tool_exposure="compact",
        computer_mode="off",
    )


def _create_task(config: SimpleNamespace, task_id: str = "task-1") -> dict:
    repo = str(config.allowed_roots[0] / "repo")
    return coding_task_tool_api.create(
        config,
        {
            "task_id": task_id,
            "cwd": repo,
            "base_ref": "HEAD",
            "paths": ["app.txt"],
        },
        client_id="client-a",
    )


def test_coding_task_create_validate_publish_happy_path() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = _config(Path(d))
        repo = Path(config.allowed_roots[0]) / "repo"
        task = _create_task(config)
        worktree = Path(task["path"])

        assert task["created"] is True
        assert task["state"] == "editing"
        assert task["base_commit"] == _git(repo, "rev-parse", "HEAD")
        assert (worktree / "app.txt").read_text(encoding="utf-8") == "initial\n"

        (worktree / "app.txt").write_text("changed\n", encoding="utf-8")
        validated = coding_task_tool_api.validate(
            config,
            {
                "task_id": "task-1",
                "cwd": str(repo),
                "operation_id": "validate-task-1",
                "command": 'test "$(cat app.txt)" = changed',
                "timeout_sec": 30,
            },
            client_id="client-a",
        )

        assert validated["state"] == "validated"
        assert validated["validation"]["validated"] is True
        assert validated["validation_current"] is True
        assert validated["validation"]["candidate_commit"] != validated["base_commit"]
        assert validated["validation"]["candidate_tree"]
        assert validated["validation"]["committed_paths"] == ["app.txt"]
        assert Path(validated["validation"]["evidence_path"]).exists()
        assert _git(worktree, "status", "--porcelain=v1") == ""

        published = coding_task_tool_api.publish(
            config,
            {"task_id": "task-1", "cwd": str(repo)},
            client_id="client-a",
        )
        candidate = validated["validation"]["candidate_commit"]
        assert published["published"] is True
        assert published["replayed"] is False
        assert published["ref_target"] == candidate
        assert _git(repo, "rev-parse", task["task_ref"]) == candidate

        replay = coding_task_tool_api.publish(
            config,
            {"task_id": "task-1", "cwd": str(repo)},
            client_id="client-a",
        )
        assert replay["published"] is True
        assert replay["replayed"] is True

        # Publishing an artifact ref never mutates the user's source checkout.
        assert (repo / "app.txt").read_text(encoding="utf-8") == "initial\n"
        assert _git(repo, "status", "--porcelain=v1") == ""

        integrated = coding_task_tool_api.integrate(
            config,
            {
                "task_id": "task-1",
                "cwd": str(repo),
                "expected_source_commit": task["base_commit"],
            },
            client_id="client-a",
        )
        assert integrated["integrated"] is True
        assert integrated["replayed"] is False
        assert integrated["integration_current"] is True
        assert _git(repo, "rev-parse", "HEAD") == candidate
        assert (repo / "app.txt").read_text(encoding="utf-8") == "changed\n"
        assert _git(repo, "status", "--porcelain=v1") == ""

        integration_replay = coding_task_tool_api.integrate(
            config,
            {
                "task_id": "task-1",
                "cwd": str(repo),
                # A retry keeps the original expected source version. The
                # already-integrated candidate is reconciled before this check.
                "expected_source_commit": task["base_commit"],
            },
            client_id="client-a",
        )
        assert integration_replay["replayed"] is True
        assert integration_replay["integration_current"] is True


def test_coding_task_validation_becomes_stale_after_code_change() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = _config(Path(d))
        repo = Path(config.allowed_roots[0]) / "repo"
        task = _create_task(config, "stale-task")
        worktree = Path(task["path"])
        (worktree / "app.txt").write_text("changed\n", encoding="utf-8")

        validated = coding_task_tool_api.validate(
            config,
            {
                "task_id": "stale-task",
                "cwd": str(repo),
                "operation_id": "validate-stale-task",
                "command": "true",
            },
            client_id="client-a",
        )
        assert validated["validation_current"] is True

        (worktree / "app.txt").write_text("changed-after-validation\n", encoding="utf-8")
        inspected = coding_task_tool_api.inspect(
            config,
            {"task_id": "stale-task", "cwd": str(repo)},
            client_id="client-a",
        )
        assert inspected["validation_current"] is False

        with pytest.raises(ValueError, match="validation_stale_or_missing"):
            coding_task_tool_api.publish(
                config,
                {"task_id": "stale-task", "cwd": str(repo)},
                client_id="client-a",
            )


def test_coding_task_rejects_undeclared_changes() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = _config(Path(d))
        repo = Path(config.allowed_roots[0]) / "repo"
        task = _create_task(config, "declared-only")
        worktree = Path(task["path"])

        (worktree / "app.txt").write_text("allowed\n", encoding="utf-8")
        (worktree / "other.txt").write_text("not declared\n", encoding="utf-8")

        with pytest.raises(ValueError, match="coding_task_undeclared_changes"):
            coding_task_tool_api.validate(
                config,
                {
                    "task_id": "declared-only",
                    "cwd": str(repo),
                    "operation_id": "validate-declared-only",
                    "command": "true",
                },
                client_id="client-a",
            )

        assert _git(worktree, "rev-parse", "HEAD") == task["base_commit"]


def test_coding_task_publish_detects_ref_drift() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = _config(Path(d))
        repo = Path(config.allowed_roots[0]) / "repo"
        task = _create_task(config, "ref-conflict")
        worktree = Path(task["path"])
        (worktree / "app.txt").write_text("candidate\n", encoding="utf-8")

        validated = coding_task_tool_api.validate(
            config,
            {
                "task_id": "ref-conflict",
                "cwd": str(repo),
                "operation_id": "validate-ref-conflict",
                "command": "true",
            },
            client_id="client-a",
        )
        assert validated["validation_current"] is True

        # Simulate another writer publishing the task ref first.
        _git(
            repo,
            "update-ref",
            task["task_ref"],
            task["base_commit"],
        )

        with pytest.raises(ValueError, match="coding_task_publish_conflict"):
            coding_task_tool_api.publish(
                config,
                {"task_id": "ref-conflict", "cwd": str(repo)},
                client_id="client-a",
            )


def test_coding_task_create_replay_and_request_conflict() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = _config(Path(d))
        repo = str(config.allowed_roots[0] / "repo")

        first = coding_task_tool_api.create(
            config,
            {
                "task_id": "replay-task",
                "cwd": repo,
                "paths": ["app.txt"],
            },
            client_id="client-a",
        )
        replay = coding_task_tool_api.create(
            config,
            {
                "task_id": "replay-task",
                "cwd": repo,
                "paths": ["app.txt"],
            },
            client_id="client-a",
        )
        assert first["created"] is True
        assert replay["created"] is False
        assert replay["replayed"] is True
        assert replay["worktree_id"] == first["worktree_id"]

        with pytest.raises(ValueError, match="idempotency_conflict"):
            coding_task_tool_api.create(
                config,
                {
                    "task_id": "replay-task",
                    "cwd": repo,
                    "paths": ["other.txt"],
                },
                client_id="client-a",
            )


def test_validation_operation_replay_does_not_execute_twice() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        config = _config(tmp)
        repo = Path(config.allowed_roots[0]) / "repo"
        task = _create_task(config, "validation-replay")
        worktree = Path(task["path"])
        (worktree / "app.txt").write_text("candidate\n", encoding="utf-8")
        counter = tmp / "validation-counter.txt"
        command = f"echo run >> {shlex.quote(str(counter))}"

        first = coding_task_tool_api.validate(
            config,
            {
                "task_id": "validation-replay",
                "cwd": str(repo),
                "operation_id": "validation-op-1",
                "command": command,
            },
            client_id="client-a",
        )
        assert first["validation_replayed"] is False
        assert counter.read_text(encoding="utf-8").splitlines() == ["run"]

        # Drift after the first response: replay must return the original evidence
        # instead of executing the command against a new workspace state.
        (worktree / "app.txt").write_text("drifted\n", encoding="utf-8")
        replay = coding_task_tool_api.validate(
            config,
            {
                "task_id": "validation-replay",
                "cwd": str(repo),
                "operation_id": "validation-op-1",
                "command": command,
            },
            client_id="client-a",
        )
        assert replay["validation_replayed"] is True
        assert replay["validation"]["validation_id"] == first["validation"]["validation_id"]
        assert replay["validation_current"] is False
        assert counter.read_text(encoding="utf-8").splitlines() == ["run"]

        with pytest.raises(ValueError, match="idempotency_conflict"):
            coding_task_tool_api.validate(
                config,
                {
                    "task_id": "validation-replay",
                    "cwd": str(repo),
                    "operation_id": "validation-op-1",
                    "command": "printf different",
                },
                client_id="client-a",
            )


def test_validation_command_workspace_mutation_invalidates_evidence() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = _config(Path(d))
        repo = Path(config.allowed_roots[0]) / "repo"
        task = _create_task(config, "validation-mutates")
        worktree = Path(task["path"])
        (worktree / "app.txt").write_text("candidate\n", encoding="utf-8")

        result = coding_task_tool_api.validate(
            config,
            {
                "task_id": "validation-mutates",
                "cwd": str(repo),
                "operation_id": "validation-mutates-op",
                "command": "printf mutated-after-test > app.txt",
            },
            client_id="client-a",
        )
        assert result["validation"]["exit_code"] == 0
        assert result["validation"]["workspace_unchanged"] is False
        assert result["validation"]["validated"] is False
        assert result["state"] == "validation_failed"
        assert result["validation_current"] is False

        with pytest.raises(ValueError, match="validation_stale_or_missing"):
            coding_task_tool_api.publish(
                config,
                {"task_id": "validation-mutates", "cwd": str(repo)},
                client_id="client-a",
            )


def test_publish_reconciles_ref_written_before_response_record() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = _config(Path(d))
        repo = Path(config.allowed_roots[0]) / "repo"
        task = _create_task(config, "publish-reconcile")
        worktree = Path(task["path"])
        (worktree / "app.txt").write_text("candidate\n", encoding="utf-8")

        validated = coding_task_tool_api.validate(
            config,
            {
                "task_id": "publish-reconcile",
                "cwd": str(repo),
                "operation_id": "validate-publish-reconcile",
                "command": "true",
            },
            client_id="client-a",
        )
        candidate = validated["validation"]["candidate_commit"]

        # Simulate update-ref succeeding while the durable task record was not
        # updated because the caller/server lost the response at the commit point.
        _git(
            repo,
            "update-ref",
            task["task_ref"],
            candidate,
        )
        reconciled = coding_task_tool_api.publish(
            config,
            {"task_id": "publish-reconcile", "cwd": str(repo)},
            client_id="client-a",
        )
        assert reconciled["published"] is True
        assert reconciled["reconciled"] is True
        assert reconciled["ref_target"] == candidate

        replay = coding_task_tool_api.publish(
            config,
            {"task_id": "publish-reconcile", "cwd": str(repo)},
            client_id="client-a",
        )
        assert replay["replayed"] is True


def test_integration_rejects_dirty_and_source_version_drift() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = _config(Path(d))
        repo = Path(config.allowed_roots[0]) / "repo"
        task = _create_task(config, "integration-conflict")
        worktree = Path(task["path"])
        (worktree / "app.txt").write_text("candidate\n", encoding="utf-8")

        validated = coding_task_tool_api.validate(
            config,
            {
                "task_id": "integration-conflict",
                "cwd": str(repo),
                "operation_id": "validate-integration-conflict",
                "command": "true",
            },
            client_id="client-a",
        )
        coding_task_tool_api.publish(
            config,
            {"task_id": "integration-conflict", "cwd": str(repo)},
            client_id="client-a",
        )

        (repo / "other.txt").write_text("dirty\n", encoding="utf-8")
        with pytest.raises(ValueError, match="source_checkout_dirty"):
            coding_task_tool_api.integrate(
                config,
                {
                    "task_id": "integration-conflict",
                    "cwd": str(repo),
                    "expected_source_commit": task["base_commit"],
                },
                client_id="client-a",
            )

        (repo / "other.txt").write_text("source drift\n", encoding="utf-8")
        _git(repo, "add", "other.txt")
        _git(repo, "commit", "-m", "source drift")
        drift_head = _git(repo, "rev-parse", "HEAD")
        assert drift_head != task["base_commit"]

        with pytest.raises(ValueError, match="coding_task_integration_conflict"):
            coding_task_tool_api.integrate(
                config,
                {
                    "task_id": "integration-conflict",
                    "cwd": str(repo),
                    "expected_source_commit": task["base_commit"],
                },
                client_id="client-a",
            )
        assert _git(repo, "rev-parse", "HEAD") == drift_head
        assert (repo / "app.txt").read_text(encoding="utf-8") == "initial\n"
        assert validated["validation"]["candidate_commit"] != drift_head


def test_integration_reconciles_external_fast_forward() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = _config(Path(d))
        repo = Path(config.allowed_roots[0]) / "repo"
        task = _create_task(config, "integration-reconcile")
        worktree = Path(task["path"])
        (worktree / "app.txt").write_text("candidate\n", encoding="utf-8")

        validated = coding_task_tool_api.validate(
            config,
            {
                "task_id": "integration-reconcile",
                "cwd": str(repo),
                "operation_id": "validate-integration-reconcile",
                "command": "true",
            },
            client_id="client-a",
        )
        candidate = validated["validation"]["candidate_commit"]
        coding_task_tool_api.publish(
            config,
            {"task_id": "integration-reconcile", "cwd": str(repo)},
            client_id="client-a",
        )

        # Simulate the source fast-forward succeeding while the task-record
        # update/response was lost.
        _git(
            repo,
            "-c",
            "core.hooksPath=/dev/null",
            "merge",
            "--ff-only",
            "--no-edit",
            candidate,
        )
        reconciled = coding_task_tool_api.integrate(
            config,
            {
                "task_id": "integration-reconcile",
                "cwd": str(repo),
                "expected_source_commit": task["base_commit"],
            },
            client_id="client-a",
        )
        assert reconciled["integrated"] is True
        assert reconciled["reconciled"] is True
        assert reconciled["replayed"] is False
        assert reconciled["integration_current"] is True
        assert _git(repo, "rev-parse", "HEAD") == candidate


def test_two_tasks_from_same_baseline_detect_integration_conflict() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = _config(Path(d))
        repo = Path(config.allowed_roots[0]) / "repo"
        baseline = _git(repo, "rev-parse", "HEAD")
        first = _create_task(config, "parallel-first")
        second = coding_task_tool_api.create(
            config,
            {
                "task_id": "parallel-second",
                "cwd": str(repo),
                "base_ref": baseline,
                "paths": ["other.txt"],
            },
            client_id="client-a",
        )
        assert first["worktree_id"] != second["worktree_id"]
        (Path(first["path"]) / "app.txt").write_text("first\n", encoding="utf-8")
        (Path(second["path"]) / "other.txt").write_text(
            "second\n", encoding="utf-8"
        )

        for task in (first, second):
            validated = coding_task_tool_api.validate(
                config,
                {
                    "task_id": task["task_id"],
                    "cwd": str(repo),
                    "operation_id": f"validate-{task['task_id']}",
                    "command": "true",
                },
                client_id="client-a",
            )
            assert validated["validation_current"] is True
            published = coding_task_tool_api.publish(
                config,
                {"task_id": task["task_id"], "cwd": str(repo)},
                client_id="client-a",
            )
            assert published["publication_current"] is True

        integrated = coding_task_tool_api.integrate(
            config,
            {
                "task_id": first["task_id"],
                "cwd": str(repo),
                "expected_source_commit": baseline,
            },
            client_id="client-a",
        )
        assert integrated["integration_current"] is True
        head_after_first = _git(repo, "rev-parse", "HEAD")

        with pytest.raises(ValueError, match="coding_task_integration_conflict"):
            coding_task_tool_api.integrate(
                config,
                {
                    "task_id": second["task_id"],
                    "cwd": str(repo),
                    "expected_source_commit": baseline,
                },
                client_id="client-a",
            )
        assert _git(repo, "rev-parse", "HEAD") == head_after_first
        assert (repo / "other.txt").read_text(encoding="utf-8") == "other\n"


def test_coding_task_principal_isolation() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = _config(Path(d))
        repo = str(config.allowed_roots[0] / "repo")
        task_a = _create_task(config, "principal-task")

        with pytest.raises(ValueError, match="coding_task_not_found"):
            coding_task_tool_api.inspect(
                config,
                {"task_id": "principal-task", "cwd": repo},
                client_id="client-b",
            )

        task_b = coding_task_tool_api.create(
            config,
            {
                "task_id": "principal-task",
                "cwd": repo,
                "paths": ["app.txt"],
            },
            client_id="client-b",
        )
        assert task_b["created"] is True
        assert task_b["worktree_id"] != task_a["worktree_id"]
        assert (
            task_b["scope"]["principal_hash"]
            != task_a["scope"]["principal_hash"]
        )


def test_validation_reconciliation_waits_for_process_and_abandons_operation() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = _config(Path(d))
        repo = str(config.allowed_roots[0] / "repo")
        task = _create_task(config, "interrupted-validation")
        store = WorktreeStore(Path(config.data_dir) / "managed-worktrees")
        operation_id = "interrupted-op"
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        try:
            record = store.read_task(task["task_key"])
            assert record is not None
            record["validation_attempt"] = {
                "operation_id": operation_id,
                "state": "running",
                "pid": process.pid,
            }
            record["state"] = "validation_running"
            store.write_task(task["task_key"], record)

            args = {
                "task_id": task["task_id"],
                "cwd": repo,
                "operation_id": operation_id,
                "resolution": "abandon",
                "confirm_effects_reviewed": True,
            }
            with pytest.raises(ValueError, match="process_may_still_be_running"):
                coding_task_tool_api.reconcile_validation(
                    config, args, client_id="client-a"
                )

            process.terminate()
            process.wait(timeout=5)
            reconciled = coding_task_tool_api.reconcile_validation(
                config, args, client_id="client-a"
            )
            assert reconciled["reconciled"] is True
            assert reconciled["abandoned_attempt"]["state"] == "abandoned"
            assert reconciled["validation_attempt"] is None

            with pytest.raises(ValueError, match="validation_operation_abandoned"):
                coding_task_tool_api.validate(
                    config,
                    {
                        "task_id": task["task_id"],
                        "cwd": repo,
                        "operation_id": operation_id,
                        "command": "true",
                    },
                    client_id="client-a",
                )
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def test_coding_task_capability_discovery_and_registry_contract() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = _config(Path(d))
        repo = str(config.allowed_roots[0] / "repo")
        registry = ToolRegistry(config, AuditLogger(config.audit_log))

        for query, expected in (
            ("coding task create worktree baseline", "coding_task_create"),
            ("coding task validate candidate evidence", "coding_task_validate"),
            ("coding task reconcile validation uncertain", "coding_task_reconcile_validation"),
            ("coding task publish git ref cas", "coding_task_publish"),
            ("coding task integrate fast-forward source checkout", "coding_task_integrate"),
        ):
            search = registry.call_tool(
                "capability_search",
                {"query": query},
                client_id="client-a",
            )
            payload = json.loads(search["content"][0]["text"])
            names = {match["name"] for match in payload["matches"]}
            assert expected in names

        get_result = registry.call_tool(
            "capability_get",
            {"name": "coding_task_validate"},
            client_id="client-a",
        )
        definition = get_result["structuredContent"]
        assert definition["category"] == "workspace"
        assert set(definition["tool"]["inputSchema"]["required"]) == {
            "task_id",
            "cwd",
            "operation_id",
            "command",
        }

        reconcile_definition = registry.call_tool(
            "capability_get",
            {"name": "coding_task_reconcile_validation"},
            client_id="client-a",
        )["structuredContent"]
        assert set(reconcile_definition["tool"]["inputSchema"]["required"]) == {
            "task_id",
            "cwd",
            "operation_id",
            "resolution",
            "confirm_effects_reviewed",
        }

        created = registry.call_tool(
            "coding_task_create",
            {
                "task_id": "registry-task",
                "cwd": repo,
                "paths": ["app.txt"],
            },
            client_id="client-a",
        )
        created_payload = json.loads(created["content"][0]["text"])
        assert created_payload["created"] is True


def test_registry_task_file_workflow_and_scope(tmp_path: Path) -> None:
    config = _config(tmp_path)
    repo = config.allowed_roots[0] / "repo"
    registry = ToolRegistry(config, AuditLogger(config.audit_log))
    scope = {"cwd": str(repo), "task_id": "files"}

    def call(name, args=None, client="client-a"):
        return registry.call_tool(
            f"coding_task_{name}", {**scope, **(args or {})}, client_id=client,
        )["structuredContent"]

    task = call("create", {"paths": ["app.txt"]})
    checkout = Path(task["path"])
    assert not checkout.is_relative_to(repo.parent.resolve())
    file = {"path": "app.txt"}
    read = call("read_file", file)
    assert read["text"] == "initial\n"
    assert call("read_file", {"path": "other.txt"})["text"] == "other\n"
    with pytest.raises(ValueError, match="not_found"):
        call("read_file", file, client="client-b")
    other_repo = repo.parent / "second"
    other_repo.mkdir()
    _init_repo(other_repo)
    with pytest.raises(ValueError, match="not_found"):
        call("read_file", {**file, "cwd": str(other_repo)})
    link = checkout / "link"
    link.symlink_to(repo / "app.txt")
    for path in ("../app.txt", str(repo / "app.txt"), ".git", ".GIT/config", "link"):
        with pytest.raises(ValueError, match="invalid_coding_task_file_path"):
            call("read_file", {"path": path})
    link.unlink()
    with pytest.raises(ValueError, match="file_not_declared"):
        call("write_file", {"path": "other.txt", "content": "blocked"})
    call("write_file", {
        **file, "content": "edited\n", "overwrite": True,
        "expected_sha256": read["sha256"], "full_replace_token": read["full_replace_token"],
    })
    patch = {**file, "old": "edited", "new": "changed"}
    with pytest.raises(ValueError, match="file_changed_since_read"):
        call("apply_patch", {**patch, "expected_sha256": read["sha256"]})
    call("apply_patch", {**patch, "expected_sha256": call("read_file", file)["sha256"]})
    assert call("validate", {
        "operation_id": "validate", "command": "grep -qx changed app.txt",
    })["validation_current"]
    assert call("publish")["published"]
    assert call("integrate", {"expected_source_commit": task["base_commit"]})["integrated"]
    assert (repo / "app.txt").read_text() == "changed\n"


@pytest.mark.parametrize(("command", "timeout", "valid", "timed_out"), [
    ("(sleep 0.25; true) & exit 0", 10, True, False),
    ("(sleep 0.25; printf 'late change' > app.txt) & exit 0", 10, False, False),
    ("(trap '' TERM; while true; do sleep 1; done) & exit 0", 1, False, True),
])
def test_validation_waits_for_background_processes(tmp_path, command, timeout, valid, timed_out):
    from mcp4chatgpt.workspace import coding_tasks

    config = _config(tmp_path)
    task = _create_task(config)
    Path(task["path"], "app.txt").write_text("changed\n")
    result = coding_task_tool_api.validate(config, {
        "cwd": task["source_repo"], "task_id": task["task_id"],
        "operation_id": "background", "command": command, "timeout_sec": timeout,
    }, client_id="client-a")
    assert result["validation"]["validated"] is valid
    assert result["validation"]["timed_out"] is timed_out
    assert not coding_tasks._validation_group_alive(result["validation_attempt_history"][-1]["pgid"])
    if not valid:
        with pytest.raises(ValueError, match="validation_stale_or_missing"):
            coding_task_tool_api.publish(config, {
                "cwd": task["source_repo"], "task_id": task["task_id"],
            }, client_id="client-a")


def test_unresolved_validation_blocks_previous_publication(tmp_path, monkeypatch):
    from mcp4chatgpt.workspace import coding_tasks

    config = _config(tmp_path)
    task = _create_task(config)
    scope = {"cwd": task["source_repo"], "task_id": task["task_id"]}
    Path(task["path"], "app.txt").write_text("changed\n")
    args = {**scope, "operation_id": "first", "command": "true"}
    coding_task_tool_api.validate(config, args, client_id="client-a")
    coding_task_tool_api.publish(config, scope, client_id="client-a")

    def unknown(_pgid):
        raise PermissionError("process status unavailable")

    monkeypatch.setattr(coding_tasks, "_validation_group_alive", unknown)
    retry = {**args, "operation_id": "second"}
    with pytest.raises(RuntimeError, match="validation_uncertain"):
        coding_task_tool_api.validate(config, retry, client_id="client-a")
    with pytest.raises(ValueError, match="validation_uncertain"):
        coding_task_tool_api.validate(config, retry, client_id="client-a")
    view = coding_task_tool_api.inspect(config, scope, client_id="client-a")
    assert view["validation_attempt"]["state"] == "uncertain"
    assert not view["validation_current"]
    with pytest.raises(ValueError, match="validation_unresolved"):
        coding_task_tool_api.write_file(config, {
            **scope, "path": "app.txt", "content": "blocked",
        }, client_id="client-a")
    with pytest.raises(ValueError, match="validation_unresolved"):
        coding_task_tool_api.publish(config, scope, client_id="client-a")
    with pytest.raises(ValueError, match="validation_unresolved"):
        coding_task_tool_api.integrate(config, {
            **scope, "expected_source_commit": task["base_commit"],
        }, client_id="client-a")
    with pytest.raises(ValueError, match="process_may_still_be_running"):
        coding_task_tool_api.reconcile_validation(config, {
            **scope, "operation_id": "second", "resolution": "abandon",
            "confirm_effects_reviewed": True,
        }, client_id="client-a")


def test_task_checkout_protected_before_task_record_and_on_legacy_replay(tmp_path, monkeypatch):
    from mcp4chatgpt.workspace import worktree_tool_api

    config = _config(tmp_path)
    repo = str(config.allowed_roots[0] / "repo")
    store = WorktreeStore(config.data_dir / "managed-worktrees")
    with monkeypatch.context() as patch:
        def interrupted(*_args):
            raise OSError("task record write interrupted")

        patch.setattr(WorktreeStore, "write_task", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            _create_task(config)
    record, = store.list_records()
    args = {"cwd": repo, "worktree_id": record["worktree_id"]}
    with pytest.raises(ValueError, match="worktree_in_use_by_coding_task"):
        worktree_tool_api.remove(config, args, client_id="client-a")
    task = _create_task(config)
    assert task["worktree_id"] == record["worktree_id"]
    assert task["worktree_replayed"]
    record["scope"].pop("coding_task_key")
    store.write_record(record)
    with pytest.raises(ValueError, match="worktree_in_use_by_coding_task"):
        worktree_tool_api.remove(config, args, client_id="client-a")
    _create_task(config)
    assert store.read_record(record["worktree_id"])["scope"]["coding_task_key"] == task["task_key"]


def test_remove_refused_while_validation_is_running(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from mcp4chatgpt.workspace import coding_tasks, worktree_tool_api

    config = _config(tmp_path)
    task = _create_task(config)
    Path(task["path"], "app.txt").write_text("changed\n")
    started, checked = Event(), Event()
    wait_for_group = coding_tasks._wait_validation_group

    def wait(process, deadline):
        started.set()
        assert checked.wait(timeout=5)
        return wait_for_group(process, deadline)

    monkeypatch.setattr(coding_tasks, "_wait_validation_group", wait)
    with ThreadPoolExecutor(max_workers=1) as pool:
        validation = pool.submit(coding_task_tool_api.validate, config, {
            "cwd": task["source_repo"], "task_id": task["task_id"],
            "operation_id": "validate", "command": "(sleep 0.5; true) & exit 0",
            "timeout_sec": 10,
        }, client_id="client-a")
        try:
            assert started.wait(timeout=5)
            with pytest.raises(ValueError, match="worktree_in_use_by_coding_task"):
                worktree_tool_api.remove(config, {
                    "cwd": task["source_repo"], "worktree_id": task["worktree_id"],
                }, client_id="client-a")
        finally:
            checked.set()
        assert validation.result(timeout=10)["validation_current"]
