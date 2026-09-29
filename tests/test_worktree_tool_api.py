from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp4chatgpt.audit import AuditLogger
from mcp4chatgpt.tools import ToolRegistry
from mcp4chatgpt.workspace import worktree_tool_api


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def init_repo(path: Path) -> Path:
    _git(path, "init")
    _git(path, "config", "user.name", "Tester")
    _git(path, "config", "user.email", "tester@example.com")
    (path / "file.txt").write_text("initial\n", encoding="utf-8")
    _git(path, "add", "file.txt")
    _git(path, "commit", "-m", "initial")
    return path


def make_config(tmp: Path) -> SimpleNamespace:
    root = tmp / "root"
    root.mkdir()
    repo = root / "repo"
    repo.mkdir()
    init_repo(repo)
    return SimpleNamespace(
        data_dir=tmp / "data",
        allowed_roots=[root],
        audit_log=tmp / "logs" / "audit.jsonl",
        max_output_chars=10_000,
        local_auth_disabled=False,
        tool_exposure="compact",
        computer_mode="off",
    )


def test_scoped_worktree_replay_and_principal_isolation() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = make_config(Path(d))
        repo_path = str(config.allowed_roots[0] / "repo")
        args = {
            "operation_id": "wt-scope-test",
            "cwd": repo_path,
            "base_ref": "HEAD",
        }

        # 1. Client A creates worktree
        wt_a = worktree_tool_api.create(config, args, "client-a")
        assert wt_a["created"] is True
        assert wt_a["replayed"] is False
        assert "scope" in wt_a
        worktree_id = wt_a["worktree_id"]

        # 2. Client A replays identical request
        wt_replay = worktree_tool_api.create(config, args, "client-a")
        assert wt_replay["replayed"] is True
        assert wt_replay["created"] is False
        assert wt_replay["worktree_id"] == worktree_id

        # 3. Status checks
        st_a = worktree_tool_api.status(config, {"worktree_id": worktree_id}, "client-a")
        assert st_a["worktree_id"] == worktree_id
        assert st_a["state"] == "active"

        # Client B cannot view Client A's worktree
        with pytest.raises(ValueError, match="worktree_not_found"):
            worktree_tool_api.status(config, {"worktree_id": worktree_id}, "client-b")

        # 4. List checks
        list_a = worktree_tool_api.list_worktrees(config, {}, "client-a")
        assert any(w["worktree_id"] == worktree_id for w in list_a["worktrees"])

        list_b = worktree_tool_api.list_worktrees(config, {}, "client-b")
        assert not any(w["worktree_id"] == worktree_id for w in list_b["worktrees"])

        # 5. Remove checks
        with pytest.raises(ValueError, match="worktree_not_found"):
            worktree_tool_api.remove(config, {"worktree_id": worktree_id}, "client-b")

        rm_a = worktree_tool_api.remove(config, {"worktree_id": worktree_id}, "client-a")
        assert rm_a["removed"] is True
        assert rm_a["state"] == "removed"


def test_different_principals_can_reuse_public_operation_id() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = make_config(Path(d))
        repo_path = str(config.allowed_roots[0] / "repo")
        args = {
            "operation_id": "shared-op-id",
            "cwd": repo_path,
        }

        wt_a = worktree_tool_api.create(config, args, "client-a")
        wt_b = worktree_tool_api.create(config, args, "client-b")

        assert wt_a["created"] is True
        assert wt_b["created"] is True
        assert wt_a["worktree_id"] != wt_b["worktree_id"]
        assert wt_a["path"] != wt_b["path"]


def test_registry_call_tool_and_discovery_integration() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = make_config(Path(d))
        repo_path = str(config.allowed_roots[0] / "repo")
        registry = ToolRegistry(config, AuditLogger(config.audit_log))

        # 1. Capability search for worktree
        search_res = registry.call_tool(
            "capability_search",
            {"query": "worktree"},
            client_id="client-test",
        )
        search_data = json.loads(search_res["content"][0]["text"])
        matches = [m["name"] for m in search_data["matches"]]
        assert "local_worktree_create" in matches
        assert "local_worktree_status" in matches
        assert "local_worktree_list" in matches
        assert "local_worktree_remove" in matches

        # 2. Capability get
        get_res = registry.call_tool(
            "capability_get",
            {"name": "local_worktree_create"},
            client_id="client-test",
        )
        get_data = json.loads(get_res["content"][0]["text"])
        assert get_data["canonical_name"] == "local_worktree_create"
        assert get_data["category"] == "workspace"
        assert bool(get_data["capability_revision"])

        # 3. Create worktree via call_tool
        create_res = registry.call_tool(
            "local_worktree_create",
            {"operation_id": "reg-test-1", "cwd": repo_path},
            client_id="client-test",
        )
        create_data = json.loads(create_res["content"][0]["text"])
        assert create_data["created"] is True
        wt_id = create_data["worktree_id"]

        # 4. Status via call_tool
        status_res = registry.call_tool(
            "local_worktree_status",
            {"worktree_id": wt_id},
            client_id="client-test",
        )
        status_data = json.loads(status_res["content"][0]["text"])
        assert status_data["worktree_id"] == wt_id
        assert status_data["state"] == "active"

        # 5. List via call_tool
        list_res = registry.call_tool(
            "local_worktree_list",
            {},
            client_id="client-test",
        )
        list_data = json.loads(list_res["content"][0]["text"])
        assert any(w["worktree_id"] == wt_id for w in list_data["worktrees"])

        # 6. Remove via call_tool
        rm_res = registry.call_tool(
            "local_worktree_remove",
            {"worktree_id": wt_id},
            client_id="client-test",
        )
        rm_data = json.loads(rm_res["content"][0]["text"])
        assert rm_data["removed"] is True
