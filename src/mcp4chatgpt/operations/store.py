"""Local operation ledger. External effects always happen outside SQL transactions.

Opening an existing store performs no recovery or migration. Explicit create()
initializes an empty database. DELETE journal is intentional: the baseline's
SQLite 3.50.4 has a known WAL-reset defect. This module never enables WAL.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator


TRANSITIONS = {
    "accepted": {"prepared", "failed"},
    "prepared": {"running", "cancelled", "needs_reconciliation"},
    "running": {"verifying", "needs_reconciliation", "failed", "cancelled"},
    "verifying": {"succeeded", "failed", "needs_reconciliation"},
    "needs_reconciliation": {"verifying", "failed", "compensated"},
    "succeeded": {"compensating"},
    "failed": {"compensating"},
    "cancelled": {"compensating"},
    "compensating": {"compensated", "compensation_failed", "needs_reconciliation"},
    "compensated": set(),
    "compensation_failed": set(),
}
EFFECTS = {"none", "committed", "partial", "unknown", "compensated"}


class OperationConflict(ValueError):
    """An operation identity, revision or transition does not match."""


def canonical_json(value: Any) -> str:
    # JSON object keys must be strings; reject coercion and non-finite floats.
    def check(item: Any) -> None:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("non_string_json_key")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError("non_json_value")
    check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def request_fingerprint(*, kind: str, parameters: dict, contract_version: str,
                        target_identity: dict, expected_version: Any,
                        configuration_version: str) -> str:
    """Hash all behavior-defining input; raw parameters are not stored."""
    payload = dict(kind=kind, parameters=parameters, contract_version=contract_version,
                   target_identity=target_identity, expected_version=expected_version,
                   configuration_version=configuration_version)
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


@dataclass(frozen=True)
class OperationKey:
    installation_id: str
    principal_id: str
    workspace_id: str
    operation_id: str

    def encoded(self) -> str:
        parts = [self.installation_id, self.principal_id,
                 self.workspace_id, self.operation_id]
        if any(not isinstance(part, str) or not part.strip() for part in parts):
            raise ValueError("empty_operation_identity")
        return canonical_json(parts)


class OperationStore:
    def __init__(self, path: Path):
        self.path = Path(path).absolute()

    @classmethod
    def create(cls, path: Path) -> OperationStore:
        """Initialize only a new file, leaving existing databases untouched."""
        store = cls(path)
        fd = os.open(store.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with store._connection(write=True, validate=False) as db:
            if db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise ValueError("unsupported_journal_mode")
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE operations (key TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                       "fingerprint TEXT NOT NULL, record TEXT NOT NULL)")
            db.execute("CREATE TABLE events (key TEXT NOT NULL REFERENCES operations(key), "
                       "revision INTEGER NOT NULL, record TEXT NOT NULL, "
                       "PRIMARY KEY(key, revision))")
            db.execute("PRAGMA user_version=1")
            db.commit()
        # Persist directory entry as well as the database contents.
        fd = os.open(store.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return store

    @contextmanager
    def _connection(self, *, write: bool = False,
                    validate: bool = True) -> Iterator[sqlite3.Connection]:
        mode = "rw" if write else "ro"
        db = sqlite3.connect(self.path.as_uri() + f"?mode={mode}", uri=True,
                             timeout=5, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("PRAGMA fullfsync=ON")
            if validate:
                if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise ValueError("unsupported_operation_schema")
                if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                    raise ValueError("unsupported_journal_mode")
            yield db
        finally:
            db.close()  # rolls back an unfinished transaction

    @staticmethod
    def _read(db: sqlite3.Connection, key: str) -> dict | None:
        row = db.execute("SELECT record FROM operations WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _event(db: sqlite3.Connection, key: str, record: dict) -> None:
        # Events contain the structured result, never original request parameters.
        db.execute("INSERT INTO events VALUES (?, ?, ?)",
                   (key, record["revision"], canonical_json(record)))

    def recover_database(self) -> None:
        """Explicit SQLite hot-journal recovery; never reconciles operations.

        Read-only queries can fail while a hot journal needs recovery. Call this
        during explicit startup maintenance, before exposing observation APIs.
        """
        with self._connection(write=True) as db:
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("operation_database_integrity_failure")

    def get(self, key: OperationKey) -> dict | None:
        with self._connection() as db:
            return self._read(db, key.encoded())

    def binding(self, key: OperationKey) -> dict | None:
        """Read the immutable request binding without changing operation state."""
        with self._connection() as db:
            row = db.execute("SELECT kind, fingerprint FROM operations WHERE key=?",
                             (key.encoded(),)).fetchone()
            return {"kind": row[0], "fingerprint": row[1]} if row else None

    def events(self, key: OperationKey) -> list[dict]:
        with self._connection() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT record FROM events WHERE key=? ORDER BY revision", (key.encoded(),))]

    def submit(self, key: OperationKey, *, kind: str, fingerprint: str) -> dict:
        identity = key.encoded()
        if not kind or len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("invalid_operation_request")
        with self._connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT kind, fingerprint FROM operations WHERE key=?", (identity,)).fetchone()
            if row:
                if row != (kind, fingerprint):
                    raise OperationConflict("idempotency_conflict")
                record = self._read(db, identity)
                db.commit()
                return {**record, "replayed": True}
            record = dict(operation_id=key.operation_id, state="accepted", effect="none",
                          replayed=False, retry_policy="query_existing", evidence_refs=[],
                          recovery_action=None, revision=0, result=None)
            db.execute("INSERT INTO operations VALUES (?, ?, ?, ?)",
                       (identity, kind, fingerprint, canonical_json(record)))
            self._event(db, identity, record)
            db.commit()
            return record

    def transition(self, key: OperationKey, *, expected_revision: int, state: str,
                   effect: str, evidence_refs: list[str], recovery_action: str | None = None,
                   result: Any = None) -> dict:
        """Executor-only CAS, not proof of resource ownership or external success.

        The caller must establish worker/resource identity and verify effects.
        Recovery and compensation require their own executor and operation identity.
        No automatic takeover, heartbeat expiry, execution or replay is provided.
        """
        if effect not in EFFECTS or not isinstance(evidence_refs, list) or any(
                not isinstance(ref, str) or not ref for ref in evidence_refs):
            raise ValueError("invalid_operation_evidence")
        identity = key.encoded()
        with self._connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._read(db, identity)
            if record is None:
                raise KeyError("operation_not_found")
            if type(expected_revision) is not int or record["revision"] != expected_revision:
                raise OperationConflict("revision_conflict")
            if state not in TRANSITIONS[record["state"]]:
                raise OperationConflict("invalid_state_transition")
            if state == "prepared" and effect != "none":
                raise ValueError("prepared_has_effect")
            if record["state"] == "accepted" and state == "failed" and effect != "none":
                raise ValueError("preparation_failure_has_effect")
            if state == "succeeded" and (effect != "committed" or not evidence_refs):
                raise ValueError("success_requires_commit_evidence")
            if state == "compensated" and (effect != "compensated" or not evidence_refs):
                raise ValueError("compensation_requires_evidence")
            record.update(state=state, effect=effect, evidence_refs=evidence_refs,
                          recovery_action=recovery_action, result=result,
                          revision=expected_revision + 1)
            db.execute("UPDATE operations SET record=? WHERE key=?", (canonical_json(record), identity))
            self._event(db, identity, record)
            db.commit()
            return record
