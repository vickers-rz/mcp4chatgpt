from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from mcp4chatgpt.operations.store import (
    OperationConflict, OperationKey, OperationStore, request_fingerprint,
)

KEY = OperationKey("installation", "principal", "workspace", "op-1")
FP = "a" * 64


@pytest.fixture
def store(tmp_path):
    return OperationStore.create(tmp_path / "operations.db")


def submit(store, key=KEY, fingerprint=FP):
    return store.submit(key, kind="file.write", fingerprint=fingerprint)


def move(store, revision, state, effect="unknown", refs=None):
    return store.transition(KEY, expected_revision=revision, state=state,
                            effect=effect, evidence_refs=refs or [])


def test_fingerprint_covers_contract_and_canonicalizes():
    args = dict(kind="write", parameters={"b": 2, "a": 1}, contract_version="1",
                target_identity={"path": "/a"}, expected_version=None, configuration_version="1")
    baseline = request_fingerprint(**args)
    assert baseline == request_fingerprint(**{**args, "parameters": {"a": 1, "b": 2}})
    for field, value in dict(kind="patch", parameters={}, contract_version="2",
                             target_identity={}, expected_version="sha", configuration_version="2").items():
        assert baseline != request_fingerprint(**{**args, field: value})
    for invalid in ({1: "coercion"}, {"a": float("nan")}, {"a": (1, 2)}):
        with pytest.raises(ValueError):
            request_fingerprint(**{**args, "parameters": invalid})


def test_identity_replay_conflict_and_scopes(store):
    first = submit(store)
    assert first["effect"] == "none"
    assert submit(store) == {**first, "replayed": True}
    with pytest.raises(OperationConflict, match="idempotency_conflict"):
        submit(store, fingerprint="b" * 64)
    with pytest.raises(OperationConflict, match="idempotency_conflict"):
        store.submit(KEY, kind="patch", fingerprint=FP)
    for field in ("installation_id", "principal_id", "workspace_id", "operation_id"):
        assert not submit(store, replace(KEY, **{field: "another"}))["replayed"]
    assert len(store.events(KEY)) == 1
    assert OperationStore(store.path).get(KEY) == first


def test_state_machine_cas_and_terminal_replay(store):
    submit(store)
    with pytest.raises(OperationConflict, match="invalid_state_transition"):
        move(store, 0, "succeeded", "committed", ["proof"])
    move(store, 0, "prepared", "none")
    with pytest.raises(OperationConflict, match="revision_conflict"):
        move(store, 0, "running")
    move(store, 1, "running")
    move(store, 2, "verifying")
    with pytest.raises(ValueError, match="success_requires_commit_evidence"):
        move(store, 3, "succeeded", "committed")
    completed = move(store, 3, "succeeded", "committed", ["artifact:after"])
    assert submit(store) == {**completed, "replayed": True}
    assert [event["revision"] for event in store.events(KEY)] == list(range(5))
    with pytest.raises(OperationConflict):
        move(store, 4, "running")


def test_unknown_failure_and_explicit_reconciliation(store):
    submit(store)
    move(store, 0, "prepared", "none")
    move(store, 1, "running")
    move(store, 2, "needs_reconciliation")
    before = store.path.read_bytes()
    assert store.get(KEY)["effect"] == "unknown"
    assert len(store.events(KEY)) == 4
    assert submit(store)["state"] == "needs_reconciliation"
    assert store.path.read_bytes() == before
    move(store, 3, "failed", "partial", ["artifact:partial"])
    move(store, 4, "compensating", "partial")
    move(store, 5, "compensation_failed", "unknown")
    assert store.get(KEY)["effect"] == "unknown"


def test_atomic_event_and_record_on_exception(store, monkeypatch):
    submit(store)
    def fail(*args):
        raise RuntimeError("injected event write failure")
    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(RuntimeError):
        move(store, 0, "prepared", "none")
    assert store.get(KEY)["revision"] == 0
    assert len(store.events(KEY)) == 1
    other = replace(KEY, operation_id="failed-submit")
    with pytest.raises(RuntimeError):
        submit(store, other)
    assert store.get(other) is None
    assert store.events(other) == []


def test_concurrent_submit_and_cas(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda _: submit(OperationStore(store.path)), range(16)))
    assert sum(not row["replayed"] for row in rows) == 1
    def claim(_):
        try:
            move(OperationStore(store.path), 0, "prepared", "none")
            return True
        except OperationConflict:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(claim, range(16))) == 1
    assert len(store.events(KEY)) == 2


def test_observation_does_not_create_or_mutate(tmp_path, store):
    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        OperationStore(missing).get(KEY)
    assert not missing.exists()
    submit(store)
    before = store.path.read_bytes()
    assert store.get(KEY)["state"] == "accepted"
    assert len(store.events(KEY)) == 1
    assert store.path.read_bytes() == before
    with pytest.raises(FileExistsError):
        OperationStore.create(store.path)
    assert store.path.read_bytes() == before
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="unsupported_operation_schema"):
        store.get(KEY)


# Actual process death, including a file replacement outside the metadata transaction.
# This is a protocol fixture, not an integrated file executor or power-loss test.
CHILD = '''
import os, sys
from pathlib import Path
from mcp4chatgpt.operations.store import OperationStore, OperationKey
from mcp4chatgpt.workspace.recovery import atomic_replace_bytes
store = OperationStore(Path(sys.argv[1]))
key = OperationKey("installation", "principal", "workspace", "op-1")
point = sys.argv[2]
def die_during_transaction(db, *args):
    # Force dirty pages onto disk so this exercises hot-journal rollback too.
    db.execute("PRAGMA cache_size=1")
    db.execute("CREATE TABLE crash_spill (payload BLOB)")
    db.execute("INSERT INTO crash_spill VALUES (zeroblob(2000000))")
    os._exit(73)
if point == "submit_uncommitted":
    store._event = die_during_transaction
store.submit(key, kind="file.write", fingerprint="a" * 64)
if point == "accepted": os._exit(73)
if point == "transition_uncommitted":
    store._event = die_during_transaction
store.transition(key, expected_revision=0, state="prepared", effect="none", evidence_refs=[])
if point == "prepared": os._exit(73)
store.transition(key, expected_revision=1, state="running", effect="unknown", evidence_refs=[])
if point == "running": os._exit(73)
atomic_replace_bytes(Path(sys.argv[3]), b"after", 0o600)
if point == "replaced": os._exit(73)
store.transition(key, expected_revision=2, state="verifying", effect="unknown", evidence_refs=[])
store.transition(key, expected_revision=3, state="succeeded", effect="committed", evidence_refs=["fixture:after"])
os._exit(73)
'''


@pytest.mark.parametrize("point,state,content", [
    ("submit_uncommitted", None, b"before"),
    ("accepted", "accepted", b"before"),
    ("transition_uncommitted", "accepted", b"before"),
    ("prepared", "prepared", b"before"),
    ("running", "running", b"before"),
    ("replaced", "running", b"after"),
    ("completed", "succeeded", b"after"),
])
def test_process_crash_boundaries(store, tmp_path, point, state, content):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    proc = subprocess.run([sys.executable, "-c", CHILD, str(store.path), point, str(target)],
                          env=env, capture_output=True, timeout=10)
    assert proc.returncode == 73, proc.stderr.decode()
    reopened = OperationStore(store.path)
    # Writable opening lets SQLite recover its own hot rollback journal. This
    # database recovery never invokes an application executor or replays effects.
    reopened.recover_database()
    with sqlite3.connect(store.path) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='crash_spill'").fetchall()
    record = reopened.get(KEY)
    if state is None:
        assert record is None
        assert reopened.events(KEY) == []
    else:
        assert record["state"] == state
        assert reopened.events(KEY)[-1] == record
        assert submit(reopened)["replayed"]
    assert target.read_bytes() == content


def test_independent_processes_share_reservation(store):
    code = '''
from pathlib import Path
import sys
from mcp4chatgpt.operations.store import OperationStore, OperationKey
store = OperationStore(Path(sys.argv[1]))
row = store.submit(OperationKey("installation", "principal", "workspace", "op-1"),
                   kind="file.write", fingerprint="a" * 64)
print(int(row["replayed"]))
'''
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    children = [subprocess.Popen([sys.executable, "-c", code, str(store.path)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(8)]
    outputs = []
    try:
        for child in children:
            out, err = child.communicate(timeout=10)
            assert child.returncode == 0, err.decode()
            outputs.append(out.strip())
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()
    assert outputs.count(b"0") == 1
    assert outputs.count(b"1") == 7
    assert len(store.events(KEY)) == 1
