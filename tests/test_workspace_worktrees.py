from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mcp4chatgpt.workspace import worktrees
from mcp4chatgpt.workspace.worktree_store import WorktreeStore


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def init_test_repo(path: Path) -> Path:
    _git(path, "init")
    _git(path, "config", "user.name", "Tester")
    _git(path, "config", "user.email", "tester@example.com")
    file = path / "README.md"
    file.write_text("# Test Repo\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "Initial commit")
    return path


class WorkspaceWorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp_dir.name)
        self.repo_dir = self.tmp / "repo"
        self.repo_dir.mkdir()
        init_test_repo(self.repo_dir)
        self.config = SimpleNamespace(
            allowed_roots=[self.tmp],
            data_dir=self.tmp / "data",
            local_auth_disabled=True,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_worktree_store_validation(self) -> None:
        store = WorktreeStore(self.tmp / "store")
        valid_id = "wt_" + "a" * 24
        self.assertEqual(store.validate_worktree_id(valid_id), valid_id)
        with self.assertRaises(ValueError):
            store.validate_worktree_id("invalid_id")
        with self.assertRaises(ValueError):
            store.validate_worktree_id("wt_123")

    def test_create_worktree_and_status(self) -> None:
        res = worktrees.create_worktree(
            self.config,
            operation_id="op_wt_1",
            cwd=str(self.repo_dir),
        )
        self.assertTrue(res["created"])
        self.assertFalse(res["replayed"])
        self.assertEqual(res["state"], "active")
        self.assertTrue(res["registered"])
        self.assertTrue(res["exists"])
        self.assertFalse(res["dirty"])
        worktree_id = res["worktree_id"]
        wt_path = Path(res["path"])
        self.assertTrue(wt_path.exists())
        self.assertTrue((wt_path / "README.md").exists())

        # Check status
        st = worktrees.worktree_status(self.config, worktree_id)
        self.assertEqual(st["worktree_id"], worktree_id)
        self.assertEqual(st["state"], "active")
        self.assertFalse(st["dirty"])

        # Edit file in worktree -> dirty
        (wt_path / "new_file.txt").write_text("edit\n", encoding="utf-8")
        st_dirty = worktrees.worktree_status(self.config, worktree_id)
        self.assertTrue(st_dirty["dirty"])

    def test_create_worktree_idempotent_replay_and_conflict(self) -> None:
        res1 = worktrees.create_worktree(
            self.config,
            operation_id="op_wt_replay",
            cwd=str(self.repo_dir),
        )
        self.assertTrue(res1["created"])

        # Replay with same operation_id
        res2 = worktrees.create_worktree(
            self.config,
            operation_id="op_wt_replay",
            cwd=str(self.repo_dir),
        )
        self.assertTrue(res2["replayed"])
        self.assertFalse(res2["created"])
        self.assertEqual(res2["worktree_id"], res1["worktree_id"])

        # Create another commit in repo
        (self.repo_dir / "file2.txt").write_text("commit2\n", encoding="utf-8")
        _git(self.repo_dir, "add", "file2.txt")
        _git(self.repo_dir, "commit", "-m", "Second commit")

        # Conflict on same operation_id but pointing to new HEAD
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            worktrees.create_worktree(
                self.config,
                operation_id="op_wt_replay",
                cwd=str(self.repo_dir),
            )

    def test_dirty_source_repo_rejected(self) -> None:
        (self.repo_dir / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "source_checkout_dirty"):
            worktrees.create_worktree(
                self.config,
                operation_id="op_wt_dirty",
                cwd=str(self.repo_dir),
            )

    def test_remove_worktree_clean_vs_dirty(self) -> None:
        res = worktrees.create_worktree(
            self.config,
            operation_id="op_wt_rm",
            cwd=str(self.repo_dir),
        )
        worktree_id = res["worktree_id"]
        wt_path = Path(res["path"])

        # Dirty worktree removal rejected
        (wt_path / "dirty.txt").write_text("change\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "managed_worktree_dirty"):
            worktrees.remove_worktree(self.config, worktree_id)

        # Discard change
        (wt_path / "dirty.txt").unlink()

        # Clean removal succeeds
        rm = worktrees.remove_worktree(self.config, worktree_id)
        self.assertTrue(rm["removed"])
        self.assertFalse(rm["already_removed"])
        self.assertEqual(rm["state"], "removed")
        self.assertFalse(wt_path.exists())

        # Repeated removal is idempotent no-op
        rm2 = worktrees.remove_worktree(self.config, worktree_id)
        self.assertFalse(rm2["removed"])
        self.assertTrue(rm2["already_removed"])

    def test_concurrent_worktrees_isolation(self) -> None:
        wt1 = worktrees.create_worktree(
            self.config,
            operation_id="op_wt_concurrent_1",
            cwd=str(self.repo_dir),
        )
        wt2 = worktrees.create_worktree(
            self.config,
            operation_id="op_wt_concurrent_2",
            cwd=str(self.repo_dir),
        )
        self.assertNotEqual(wt1["worktree_id"], wt2["worktree_id"])
        path1 = Path(wt1["path"])
        path2 = Path(wt2["path"])
        self.assertNotEqual(path1, path2)

        # Edit in wt1
        (path1 / "agent1.txt").write_text("agent 1 change\n", encoding="utf-8")
        self.assertTrue(worktrees.worktree_status(self.config, wt1["worktree_id"])["dirty"])
        self.assertFalse(worktrees.worktree_status(self.config, wt2["worktree_id"])["dirty"])
        self.assertFalse((path2 / "agent1.txt").exists())
        self.assertFalse((self.repo_dir / "agent1.txt").exists())

        # List worktrees
        listed = worktrees.list_worktrees(self.config)
        ids = [w["worktree_id"] for w in listed["worktrees"]]
        self.assertIn(wt1["worktree_id"], ids)
        self.assertIn(wt2["worktree_id"], ids)


if __name__ == "__main__":
    unittest.main()
