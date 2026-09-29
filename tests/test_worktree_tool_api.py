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


def test_repo_root_workspace_scoping_prevents_collision() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        root = tmp / "allowed_root"
        root.mkdir()
        repo_a = root / "project_a"
        repo_b = root / "project_b"
        repo_a.mkdir()
        repo_b.mkdir()
        init_repo(repo_a)
        init_repo(repo_b)

        config = SimpleNamespace(
            data_dir=tmp / "data",
            allowed_roots=[root],
            audit_log=tmp / "logs" / "audit.jsonl",
            max_output_chars=10_000,
            local_auth_disabled=False,
            tool_exposure="compact",
            computer_mode="off",
        )

        # Same client, same operation_id, but different repositories under same allowed root
        op_id = "task-migrate-1"
        wt_a = worktree_tool_api.create(
            config,
            {"operation_id": op_id, "cwd": str(repo_a)},
            client_id="client-1",
        )
        wt_b = worktree_tool_api.create(
            config,
            {"operation_id": op_id, "cwd": str(repo_b)},
            client_id="client-1",
        )

        assert wt_a["created"] is True
        assert wt_b["created"] is True
        assert wt_a["worktree_id"] != wt_b["worktree_id"]
        # Different workspace identities
        assert wt_a["scope"]["workspace_id"] != wt_b["scope"]["workspace_id"]


def test_cwd_workspace_authorization_and_filtering() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        root = tmp / "allowed_root"
        root.mkdir()
        repo_a = root / "project_a"
        repo_b = root / "project_b"
        repo_a.mkdir()
        repo_b.mkdir()
        init_repo(repo_a)
        init_repo(repo_b)

        config = SimpleNamespace(
            data_dir=tmp / "data",
            allowed_roots=[root],
            audit_log=tmp / "logs" / "audit.jsonl",
            max_output_chars=10_000,
            local_auth_disabled=False,
            tool_exposure="compact",
            computer_mode="off",
        )

        wt_a = worktree_tool_api.create(
            config,
            {"operation_id": "op-a", "cwd": str(repo_a)},
            client_id="client-1",
        )
        wt_b = worktree_tool_api.create(
            config,
            {"operation_id": "op-b", "cwd": str(repo_b)},
            client_id="client-1",
        )

        wt_a_id = wt_a["worktree_id"]
        wt_b_id = wt_b["worktree_id"]

        # Status with matching cwd succeeds
        st = worktree_tool_api.status(
            config,
            {"worktree_id": wt_a_id, "cwd": str(repo_a)},
            client_id="client-1",
        )
        assert st["worktree_id"] == wt_a_id

        # Status with mismatching cwd is rejected
        with pytest.raises(ValueError, match="worktree_not_found"):
            worktree_tool_api.status(
                config,
                {"worktree_id": wt_a_id, "cwd": str(repo_b)},
                client_id="client-1",
            )

        # Remove with mismatching cwd is rejected
        with pytest.raises(ValueError, match="worktree_not_found"):
            worktree_tool_api.remove(
                config,
                {"worktree_id": wt_a_id, "cwd": str(repo_b)},
                client_id="client-1",
            )

        # List filtering by cwd
        list_all = worktree_tool_api.list_worktrees(config, {}, client_id="client-1")
        all_ids = [w["worktree_id"] for w in list_all["worktrees"]]
        assert wt_a_id in all_ids and wt_b_id in all_ids

        list_repo_a = worktree_tool_api.list_worktrees(
            config,
            {"cwd": str(repo_a)},
            client_id="client-1",
        )
        a_ids = [w["worktree_id"] for w in list_repo_a["worktrees"]]
        assert wt_a_id in a_ids
        assert wt_b_id not in a_ids


def test_legacy_unscoped_records_isolated_from_mcp_api() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        root = tmp / "allowed_root"
        root.mkdir()
        repo = root / "repo"
        repo.mkdir()
        init_repo(repo)

        config = SimpleNamespace(
            data_dir=tmp / "data",
            allowed_roots=[root],
            audit_log=tmp / "logs" / "audit.jsonl",
            max_output_chars=10_000,
            local_auth_disabled=False,
            tool_exposure="compact",
            computer_mode="off",
        )

        from mcp4chatgpt.workspace.worktree_store import WorktreeStore
        from mcp4chatgpt.workspace import worktrees

        store = WorktreeStore(Path(config.data_dir) / "managed-worktrees")
        legacy_id = "wt_" + "0" * 24
        store.write_record({
            "schema_version": 1,
            "worktree_id": legacy_id,
            "operation_id": "legacy-op",
            "fingerprint": "fp",
            "source_repo": str(repo),
            "path": str(tmp / "wt_legacy"),
            "base_commit": "abc",
            "base_ref": "HEAD",
            "state": "active",
            "created_at": "2026-09-01T00:00:00Z",
            # No 'scope' field
        })

        # Authenticated MCP API rejects legacy unscoped record
        with pytest.raises(ValueError, match="worktree_not_found"):
            worktree_tool_api.status(config, {"worktree_id": legacy_id}, client_id="client-1")

        with pytest.raises(ValueError, match="worktree_not_found"):
            worktree_tool_api.remove(config, {"worktree_id": legacy_id}, client_id="client-1")

        # Excluded from list in authenticated API
        list_res = worktree_tool_api.list_worktrees(config, {}, client_id="client-1")
        assert not any(w["worktree_id"] == legacy_id for w in list_res["worktrees"])

        # Low-level internal function can still inspect it for maintenance/migration
        low_level_st = worktrees.worktree_status(config, legacy_id, strict_scope=False)
        assert low_level_st["worktree_id"] == legacy_id


def test_cursor_pagination_contract() -> None:
    with tempfile.TemporaryDirectory() as d:
        config = make_config(Path(d))
        repo_path = str(config.allowed_roots[0] / "repo")

        # Create 3 worktrees
        wt1 = worktree_tool_api.create(config, {"operation_id": "page-1", "cwd": repo_path}, client_id="client-1")
        wt2 = worktree_tool_api.create(config, {"operation_id": "page-2", "cwd": repo_path}, client_id="client-1")
        wt3 = worktree_tool_api.create(config, {"operation_id": "page-3", "cwd": repo_path}, client_id="client-1")

        # Page 1: limit = 2
        p1 = worktree_tool_api.list_worktrees(config, {"limit": 2}, client_id="client-1")
        assert len(p1["worktrees"]) == 2
        assert p1["has_more"] is True
        assert p1["next_cursor"] is not None

        # Page 2: limit = 2 with cursor
        p2 = worktree_tool_api.list_worktrees(
            config,
            {"limit": 2, "cursor": p1["next_cursor"]},
            client_id="client-1",
        )
        assert len(p2["worktrees"]) == 1
        assert p2["has_more"] is False
        assert p2["next_cursor"] is None

        # Total IDs across pages are distinct and match wt1, wt2, wt3
        all_paged_ids = [w["worktree_id"] for w in p1["worktrees"]] + [w["worktree_id"] for w in p2["worktrees"]]
        assert len(set(all_paged_ids)) == 3
        expected_ids = {wt1["worktree_id"], wt2["worktree_id"], wt3["worktree_id"]}
        assert set(all_paged_ids) == expected_ids

        # Invalid cursor raises error
        with pytest.raises(ValueError, match="invalid_cursor"):
            worktree_tool_api.list_worktrees(
                config,
                {"cursor": "wt_nonexistent0000000000"},
                client_id="client-1",
            )

