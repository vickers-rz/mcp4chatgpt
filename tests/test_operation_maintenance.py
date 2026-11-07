"""Retention must never erase evidence needed for an uncertain operation."""
import time
import shutil

from mcp4chatgpt.operations.files import FileExecutor
from mcp4chatgpt.operations.maintenance import prune_images
from mcp4chatgpt.operations.store import OperationKey, OperationStore
from mcp4chatgpt.workspace import recovery


KEY = OperationKey("install", "principal", "workspace", "operation")


def test_terminal_image_pruning_is_explicit_and_idempotent(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    target = workspace / "note.txt"
    target.write_text("before")
    store = OperationStore.create(state / "operations.db")
    executor = FileExecutor(store, state_dir=state, workspace=workspace)
    row = executor.patch(KEY, target, "before", "after",
                         expected_sha256=recovery.sha256_bytes(b"before"))
    assert row["state"] == "succeeded"
    future = time.time() + 91 * 86400
    assert not prune_images(store, state, KEY, now=future)["pruned"]
    assert (executor._artifacts(KEY) / "before").exists()
    result = prune_images(store, state, KEY, now=future, apply=True)
    assert result["pruned"] and result["bytes"] == len(b"before") + len(b"after")
    assert not (executor._artifacts(KEY) / "before").exists()
    assert (executor._artifacts(KEY) / "prepared.json").exists()
    assert (executor._artifacts(KEY) / "images-pruned.json").exists()
    assert prune_images(store, state, KEY, now=future, apply=True)["pruned"]
    assert executor.reconcile(KEY)["state"] == "succeeded"
    assert executor.patch(KEY, target, "before", "after",
                          expected_sha256=recovery.sha256_bytes(b"before"))["replayed"]


def test_backup_restores_ledger_and_evidence_together(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    target = workspace / "note.txt"
    target.write_text("before")
    store = OperationStore.create(state / "operations.db")
    executor = FileExecutor(store, state_dir=state, workspace=workspace)
    committed = executor.patch(KEY, target, "before", "after",
                               expected_sha256=recovery.sha256_bytes(b"before"))
    backup = tmp_path / "backup"
    shutil.copytree(state, backup)
    shutil.rmtree(state)
    shutil.copytree(backup, state)
    restored = FileExecutor(OperationStore(state / "operations.db"), state_dir=state,
                            workspace=workspace)
    restored.store.recover_database()
    assert restored.reconcile(KEY) == committed
    assert restored.patch(KEY, target, "before", "after",
                          expected_sha256=recovery.sha256_bytes(b"before"))["replayed"]
