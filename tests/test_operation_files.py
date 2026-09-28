from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from mcp4chatgpt.operations.files import FileExecutor
from mcp4chatgpt.operations.store import OperationKey, OperationStore, OperationConflict
from mcp4chatgpt.workspace import recovery, transactions, versioning

KEY = OperationKey("install", "principal", "workspace", "file-1")
BEFORE = b"hello world\n"
AFTER = b"hello there\n"


@pytest.fixture
def setup(tmp_path):
    root = tmp_path.resolve()
    workspace = root / "workspace"
    workspace.mkdir()
    state = root / "state"
    state.mkdir()
    target = workspace / "hello.txt"
    target.write_bytes(BEFORE)
    target.chmod(0o640)
    store = OperationStore.create(state / "operations.db")
    executor = FileExecutor(store, state_dir=state, workspace=workspace)
    return executor, target


def patch(executor, target, key=KEY):
    return executor.patch(key, target, "world", "there", expected_sha256=recovery.sha256_bytes(BEFORE))


def token(executor, target):
    return versioning.full_replace_token(versioning.load_or_create_key(executor.state_dir),
                                        target=target, sha256=recovery.sha256_bytes(BEFORE), size=len(BEFORE))


def test_patch_commit_replay_and_conflict(setup):
    executor, target = setup
    row = patch(executor, target)
    assert row["state"] == "succeeded"
    assert row["effect"] == "committed"
    assert target.read_bytes() == AFTER
    assert target.stat().st_mode & 0o777 == 0o640
    inode = target.stat().st_ino
    assert patch(executor, target) == {**row, "replayed": True}
    assert target.stat().st_ino == inode
    with pytest.raises(OperationConflict):
        executor.patch(KEY, target, "world", "changed", expected_sha256=recovery.sha256_bytes(BEFORE))
    assert (executor._artifacts(KEY) / "before").read_bytes() == BEFORE
    assert (executor._artifacts(KEY) / "after").read_bytes() == AFTER
    assert executor.reconcile(KEY) == row


def test_write_requires_read_proof_and_validates(setup):
    executor, target = setup
    row = executor.write(KEY, target, AFTER.decode(), expected_sha256=recovery.sha256_bytes(BEFORE),
                         full_replace_token="invalid")
    assert (row["state"], row["effect"]) == ("failed", "none")
    assert target.read_bytes() == BEFORE
    good = executor.write(replace(KEY, operation_id="write-2"), target, AFTER.decode(),
                          expected_sha256=recovery.sha256_bytes(BEFORE), full_replace_token=token(executor, target))
    assert good["state"] == "succeeded"


@pytest.mark.parametrize("case", ["stale", "syntax", "symlink", "hardlink", "fifo", "parent_symlink"])
def test_unsupported_or_stale_never_replaces(setup, case):
    executor, target = setup
    if case == "stale":
        target.write_bytes(b"external")
    elif case == "syntax":
        target = target.with_suffix(".py")
        target.write_bytes(BEFORE)
    elif case == "symlink":
        dest = target.with_name("real.txt")
        target.rename(dest)
        target.symlink_to(dest)
    elif case == "hardlink":
        os.link(target, target.with_name("link"))
    elif case == "fifo":
        target.unlink()
        os.mkfifo(target)
    elif case == "parent_symlink":
        sub = target.parent / "alias"
        sub.symlink_to(target.parent, target_is_directory=True)
        target = sub / target.name
    row = patch(executor, target)
    assert (row["state"], row["effect"]) == ("failed", "none")
    assert not (executor._artifacts(KEY) / "committed.json").exists()


def test_intervening_external_write_is_preserved(setup, monkeypatch):
    executor, target = setup
    monkeypatch.setattr(executor, "_checkpoint", lambda phase: target.write_bytes(b"external")
                        if phase == "running" else None)
    row = patch(executor, target)
    assert row["state"] == "needs_reconciliation"
    assert target.read_bytes() == b"external"
    assert executor.reconcile(KEY)["state"] == "needs_reconciliation"
    assert target.read_bytes() == b"external"


CHILD = '''
import os, sys, time
from pathlib import Path
from mcp4chatgpt.operations.files import FileExecutor
from mcp4chatgpt.operations.store import OperationStore, OperationKey
from mcp4chatgpt.workspace.recovery import sha256_bytes
root = Path(sys.argv[1])
phase = sys.argv[2]
executor = FileExecutor(OperationStore(root / "state" / "operations.db"),
                        state_dir=root / "state", workspace=root / "workspace")
def checkpoint(current):
    if phase == current:
        os._exit(73)
    if phase == "hold" and current == "running":
        (root / "ready").write_text("ready")
        while not (root / "release").exists():
            time.sleep(.01)
executor._checkpoint = checkpoint
executor.patch(OperationKey("install", "principal", "workspace", "file-1"),
               root / "workspace" / "hello.txt", "world", "there", expected_sha256=sha256_bytes(b"hello world\\n"))
'''


def child(executor, phase):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    return subprocess.Popen([sys.executable, "-c", CHILD, str(executor.state_dir.parent), phase],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def crash(executor, phase):
    process = child(executor, phase)
    _, err = process.communicate(timeout=10)
    assert process.returncode == 73, err.decode()
    executor.store.recover_database()


@pytest.mark.parametrize("phase,state,effect,content", [
    ("accepted", "failed", "none", BEFORE),
    ("prepared", "failed", "none", BEFORE),
    ("running", "failed", "none", BEFORE),
    ("replaced", "needs_reconciliation", "unknown", AFTER),
    ("receipt", "succeeded", "committed", AFTER),
    ("verifying", "succeeded", "committed", AFTER),
])
def test_crash_and_explicit_reconciliation(setup, phase, state, effect, content):
    executor, target = setup
    crash(executor, phase)
    record = executor.store.get(KEY)
    before_db = executor.store.path.read_bytes()
    assert patch(executor, target)["replayed"]
    assert executor.store.get(KEY) == record
    assert executor.store.path.read_bytes() == before_db
    row = executor.reconcile(KEY)
    assert (row["state"], row["effect"]) == (state, effect)
    assert target.read_bytes() == content
    assert executor.reconcile(KEY) == row


@pytest.mark.parametrize("change", ["third", "same_bytes_new_inode", "inplace_restore", "missing_backup",
                                    "corrupt_backup", "missing_manifest", "changed_manifest", "bad_receipt", "new_parent", "symlink"])
def test_recovery_preserves_external_changes_and_requires_evidence(setup, change):
    executor, target = setup
    crash(executor, "receipt")
    artifact = executor._artifacts(KEY)
    if change == "third":
        target.write_bytes(b"external")
    elif change == "same_bytes_new_inode":
        recovery.atomic_replace_bytes(target, AFTER, 0o640)
    elif change == "inplace_restore":
        target.write_bytes(b"external")
        target.write_bytes(AFTER)
    elif change == "missing_backup":
        (artifact / "before").unlink()
    elif change == "corrupt_backup":
        (artifact / "after").write_bytes(b"corrupt")
    elif change == "missing_manifest":
        (artifact / "prepared.json").unlink()
    elif change == "changed_manifest":
        manifest = json.loads((artifact / "prepared.json").read_text())
        manifest["worker"]["pid"] = -1
        (artifact / "prepared.json").write_text(json.dumps(manifest))
    elif change == "bad_receipt":
        receipt = json.loads((artifact / "committed.json").read_text())
        receipt["attempt_id"] = "other-attempt"
        (artifact / "committed.json").write_text(json.dumps(receipt))
    elif change == "new_parent":
        target.parent.rename(target.parent.with_name("old-workspace"))
        target.parent.mkdir()
        target.write_bytes(AFTER)
    elif change == "symlink":
        target.unlink()
        other = target.with_name("external")
        other.write_bytes(AFTER)
        target.symlink_to(other)
    before = target.read_bytes()
    row = executor.reconcile(KEY)
    assert (row["state"], row["effect"]) == ("needs_reconciliation", "unknown")
    assert target.read_bytes() == before


def test_same_after_without_receipt_is_not_proof(setup):
    executor, target = setup
    crash(executor, "running")
    target.write_bytes(AFTER)
    assert executor.reconcile(KEY)["state"] == "needs_reconciliation"


def test_live_worker_excludes_recovery_and_second_execution(setup):
    import time
    executor, target = setup
    process = child(executor, "hold")
    root = executor.state_dir.parent
    try:
        deadline = time.monotonic() + 5
        while not (root / "ready").exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.01)
        row = executor.store.get(KEY)
        with pytest.raises(BlockingIOError):
            executor.reconcile(KEY)
        assert patch(executor, target) == {**row, "replayed": True}
        with pytest.raises(OperationConflict, match="idempotency_conflict"):
            executor.patch(KEY, target, "world", "different", expected_sha256=recovery.sha256_bytes(BEFORE))
        other = patch(executor, target, replace(KEY, operation_id="competitor"))
        assert (other["state"], other["effect"]) == ("failed", "none")
        assert executor.store.get(KEY) == row
        assert target.read_bytes() == BEFORE
        (root / "release").write_text("go")
        _, err = process.communicate(timeout=10)
        assert process.returncode == 0, err.decode()
        assert executor.store.get(KEY)["state"] == "succeeded"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_recovery_respects_legacy_file_lock(setup):
    executor, target = setup
    crash(executor, "running")
    row = executor.store.get(KEY)
    with transactions.file_transaction_lock(executor.state_dir, target):
        with pytest.raises(BlockingIOError):
            executor.reconcile(KEY)
    assert executor.store.get(KEY) == row
    assert executor.reconcile(KEY)["state"] == "failed"


@pytest.mark.parametrize("failure", ["receipt", "verifying", "succeeded"])
def test_post_replace_failure_never_reports_no_effect(setup, monkeypatch, failure):
    executor, target = setup
    original_save = executor._save
    original_transition = executor.store.transition
    def save(path, value):
        if failure == "receipt" and path.name == "committed.json":
            raise OSError("injected receipt persistence failure")
        return original_save(path, value)
    def transition(*args, **kwargs):
        if kwargs["state"] == failure:
            raise OSError("injected state persistence failure")
        return original_transition(*args, **kwargs)
    monkeypatch.setattr(executor, "_save", save)
    monkeypatch.setattr(executor.store, "transition", transition)
    row = patch(executor, target)
    assert (row["state"], row["effect"]) == ("needs_reconciliation", "unknown")
    assert target.read_bytes() == AFTER
    assert patch(executor, target)["replayed"]
    monkeypatch.setattr(executor, "_save", original_save)
    monkeypatch.setattr(executor.store, "transition", original_transition)
    recovered = executor.reconcile(KEY)
    assert recovered["state"] == ("needs_reconciliation" if failure == "receipt" else "succeeded")
    if recovered["state"] == "succeeded":
        assert "reason" not in recovered["result"]


def test_non_file_operation_is_not_reconciled(setup):
    executor, _ = setup
    row = executor.store.submit(KEY, kind="job", fingerprint="a" * 64)
    with pytest.raises(ValueError, match="not_a_strict_file_operation"):
        executor.reconcile(KEY)
    assert executor.store.get(KEY) == row


def test_parent_swap_after_replace_is_not_success(setup, monkeypatch):
    executor, target = setup
    def checkpoint(phase):
        if phase == "replaced":
            target.parent.rename(target.parent.with_name("moved-workspace"))
            target.parent.mkdir()
            target.write_bytes(b"external")
    monkeypatch.setattr(executor, "_checkpoint", checkpoint)
    assert patch(executor, target)["state"] == "needs_reconciliation"
    assert target.read_bytes() == b"external"
    assert (target.parent.with_name("moved-workspace") / target.name).read_bytes() == AFTER



def test_busy_before_reservation_does_not_reserve_for_worker(setup):
    executor, target = setup
    with executor._lock(KEY):
        with pytest.raises(BlockingIOError):
            patch(executor, target)
        assert executor.store.get(KEY) is None
    assert patch(executor, target)["state"] == "succeeded"
