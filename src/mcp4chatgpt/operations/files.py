"""Experimental strict text executor, deliberately not registered as an MCP tool.

Only existing, singly-linked ordinary files in one workspace are supported.
A process holds operation and shared workspace file locks throughout execution.
Reconciliation never modifies the target or resumes an interrupted write.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Iterator
import uuid

from .store import OperationKey, OperationStore, canonical_json, request_fingerprint
from ..workspace import recovery, transactions, validators, versioning


_MAX_BYTES = 16 * 1024 * 1024
_ACTIVE = {"prepared", "running", "verifying", "needs_reconciliation"}


def _identity(value: os.stat_result) -> dict:
    return dict(dev=value.st_dev, ino=value.st_ino)


def _signature(value: os.stat_result) -> dict:
    return dict(**_identity(value), size=value.st_size, mtime_ns=value.st_mtime_ns,
                ctime_ns=value.st_ctime_ns, mode=stat.S_IMODE(value.st_mode),
                uid=value.st_uid, gid=value.st_gid, nlink=value.st_nlink)


def _snapshot(parent_fd: int, name: str) -> tuple[bytes, dict]:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("unsupported_file_type_or_hardlink")
        if before.st_size > _MAX_BYTES:
            raise ValueError("file_too_large")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(_MAX_BYTES + 1)
        after = os.fstat(fd)
        if len(data) > _MAX_BYTES or _signature(before) != _signature(after):
            raise ValueError("file_changed_during_read")
        if _signature(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != _signature(after):
            raise ValueError("file_identity_changed")
        return data, dict(**_signature(after), sha256=recovery.sha256_bytes(data))
    finally:
        os.close(fd)


class FileExecutor:
    def __init__(self, store: OperationStore, *, state_dir: Path, workspace: Path):
        self.store = store
        self.state_dir = Path(state_dir).resolve()
        self.workspace = Path(workspace).resolve(strict=True)
        if store.path.resolve().parent != self.state_dir:
            raise ValueError("store_must_live_in_shared_state_directory")

    def _path(self, target: Path) -> Path:
        path = Path(os.path.abspath(target))
        path.relative_to(self.workspace)
        if path == self.workspace or path.is_relative_to(self.state_dir):
            raise ValueError("invalid_target_path")
        return path

    @contextmanager
    def _parent(self, path: Path) -> Iterator[int]:
        # Walk from / without following any symlink, including workspace parents.
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parent.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    def _artifacts(self, key: OperationKey) -> Path:
        name = hashlib.sha256(key.encoded().encode()).hexdigest()
        return self.state_dir / "operation-files" / name

    @contextmanager
    def _lock(self, key: OperationKey) -> Iterator[None]:
        locks = self.state_dir / "operation-locks"
        locks.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = hashlib.sha256(key.encoded().encode()).hexdigest()
        fd = os.open(locks / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def _checkpoint(self, phase: str) -> None:
        """Fault-injection seam; production execution does nothing here."""

    def _save(self, path: Path, value: dict) -> None:
        recovery.atomic_replace_bytes(path, canonical_json(value).encode(), 0o600)

    def _move(self, key: OperationKey, row: dict, state: str, effect: str,
              *, result: dict | None = None, action: str | None = None) -> dict:
        previous = row.get("result") or {}
        details = {name: previous[name] for name in ("attempt_id", "manifest_sha256") if name in previous}
        details.update(result or {})
        return self.store.transition(
            key, expected_revision=row["revision"], state=state, effect=effect,
            evidence_refs=[str(self._artifacts(key) / "prepared.json")]
            if state != "failed" or row["state"] != "accepted" else [],
            result=details, recovery_action=action,
        )

    def write(self, key: OperationKey, target: Path, content: str, *,
              expected_sha256: str, full_replace_token: str,
              allow_large_reduction: bool = False) -> dict:
        return self._execute(key, target, kind="write", parameters={
            "content": content, "full_replace_token": full_replace_token,
            "allow_large_reduction": allow_large_reduction,
        }, expected_sha256=expected_sha256)

    def patch(self, key: OperationKey, target: Path, old: str, new: str, *,
              expected_sha256: str) -> dict:
        if not old:
            raise ValueError("empty_patch_anchor")
        return self._execute(key, target, kind="patch", parameters={"old": old, "new": new},
                             expected_sha256=expected_sha256)

    def _execute(self, key: OperationKey, target: Path, *, kind: str,
                 parameters: dict, expected_sha256: str) -> dict:
        path = self._path(target)
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64 or any(
                char not in "0123456789abcdef" for char in expected_sha256):
            raise ValueError("expected_sha256_required")
        fingerprint = request_fingerprint(
            kind=f"strict_file.{kind}", parameters=parameters, contract_version="strict-file-v1",
            target_identity={"workspace": str(self.workspace), "path": str(path)},
            expected_version=expected_sha256, configuration_version="strict-file-v1-16MiB",
        )
        with ExitStack() as locks:
            try:
                locks.enter_context(self._lock(key))
            except BlockingIOError:
                # Once registered, a live attempt can be observed without its
                # execution lock. Never reserve an ID on behalf of its holder.
                if self.store.binding(key) is None:
                    raise
                return self.store.submit(key, kind=f"strict_file.{kind}", fingerprint=fingerprint)
            row = self.store.submit(key, kind=f"strict_file.{kind}", fingerprint=fingerprint)
            if row["replayed"]:
                return row
            self._checkpoint("accepted")
            # Failure to acquire this lock has no target effect. No lease takeover.
            try:
                with transactions.file_transaction_lock(self.state_dir, path, blocking=False):
                    with self._parent(path) as parent:
                        return self._run(key, row, path, parent, kind, parameters, expected_sha256)
            except Exception as exc:
                current = self.store.get(key)
                if current["state"] == "accepted":
                    return self._move(key, current, "failed", "none",
                                      result={"reason": recovery.redacted_error(exc)}, action="new_operation_after_review")
                # Replacement may already be durable. Never attempt an implicit rollback.
                if current["state"] in {"prepared", "running", "verifying"}:
                    return self._move(key, current, "needs_reconciliation", "unknown",
                                      result={"reason": recovery.redacted_error(exc)}, action="reconcile")
                raise

    def _run(self, key, row, path, parent, kind, parameters, expected_sha256):
        before, before_info = _snapshot(parent, path.name)
        if before_info["uid"] != os.geteuid() or before_info["mode"] & 0o7000:
            raise ValueError("unsupported_ownership_or_special_mode")
        transactions.assert_expected_sha(current_sha256=before_info["sha256"],
                                         expected_sha256=expected_sha256)
        if kind == "write":
            token_key = versioning.load_or_create_key(self.state_dir)
            if not versioning.verify_full_replace_token(
                token_key, parameters["full_replace_token"], target=path,
                sha256=before_info["sha256"], size=len(before),
            ):
                raise ValueError("full_replace_requires_complete_read")
            after = parameters["content"].encode("utf-8")
        else:
            text = before.decode("utf-8")
            if parameters["old"] not in text:
                raise ValueError("patch_anchor_missing")
            after = text.replace(parameters["old"], parameters["new"], 1).encode("utf-8")
        if len(after) > _MAX_BYTES:
            raise ValueError("file_too_large")
        validation = validators.validate_candidate(
            path, before=before, after=after,
            allow_large_reduction=kind == "patch" or parameters["allow_large_reduction"],
        )
        artifact = self._artifacts(key)
        artifact.mkdir(parents=True, exist_ok=False, mode=0o700)
        recovery.fsync_directory(artifact.parent)
        recovery.fsync_directory(artifact.parent.parent)
        recovery.atomic_replace_bytes(artifact / "before", before, 0o600)
        recovery.atomic_replace_bytes(artifact / "after", after, 0o600)
        attempt = uuid.uuid4().hex
        staged_name = f".mcp4-operation-{attempt}"
        stage = os.open(staged_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                        0o600, dir_fd=parent)
        staged_identity = _identity(os.fstat(stage))
        try:
            with os.fdopen(stage, "wb") as stream:
                stream.write(after)
                stream.flush()
                os.fchown(stream.fileno(), before_info["uid"], before_info["gid"])
                os.fchmod(stream.fileno(), before_info["mode"])
                os.fsync(stream.fileno())
            os.fsync(parent)
            _, staged_info = _snapshot(parent, staged_name)
            manifest = dict(key=key.encoded(), attempt_id=attempt, path=str(path),
                            workspace=str(self.workspace), parent=_identity(os.fstat(parent)),
                            before=before_info, after=staged_info, staged_name=staged_name,
                            worker={"pid": os.getpid(), "attempt_id": attempt}, validation=validation)
            self._save(artifact / "prepared.json", manifest)
            row = self._move(key, row, "prepared", "none", result={
                "attempt_id": attempt,
                "manifest_sha256": recovery.sha256_bytes(canonical_json(manifest).encode()),
            })
            self._checkpoint("prepared")
            row = self._move(key, row, "running", "unknown", result={"attempt_id": attempt})
            self._checkpoint("running")
            # Repeat both checks immediately before rename. A dirfd keeps the
            # syscall anchored if an uncooperative actor renames a parent.
            with self._parent(path) as current_parent:
                if _identity(os.fstat(current_parent)) != manifest["parent"]:
                    raise ValueError("parent_identity_changed")
            if _snapshot(parent, path.name)[1] != before_info:
                raise ValueError("target_changed_before_replace")
            if _snapshot(parent, staged_name)[1] != staged_info:
                raise ValueError("staged_candidate_changed")
            os.replace(staged_name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
            self._checkpoint("replaced")
            _, committed = _snapshot(parent, path.name)
            if any(committed[field] != staged_info[field] for field in
                   ("dev", "ino", "sha256", "size", "mode")):
                raise ValueError("target_changed_after_replace")
            receipt = dict(key=key.encoded(), attempt_id=attempt, parent=manifest["parent"],
                           target=committed)
            self._save(artifact / "committed.json", receipt)
            self._checkpoint("receipt")
            row = self._move(key, row, "verifying", "committed", result={"attempt_id": attempt})
            self._checkpoint("verifying")
            return self._reconcile_locked(key, row, path, parent)
        finally:
            # Remove only our uncommitted staging entry; leave all recovery evidence.
            try:
                staged_now = os.stat(staged_name, dir_fd=parent, follow_symlinks=False)
                if _identity(staged_now) == staged_identity:
                    os.unlink(staged_name, dir_fd=parent)
                    os.fsync(parent)
            except FileNotFoundError:
                pass

    def reconcile(self, key: OperationKey) -> dict:
        """Explicit metadata reconciliation, never writes or replays a target."""
        with self._lock(key):
            row = self.store.get(key)
            if row is None:
                raise KeyError("operation_not_found")
            binding = self.store.binding(key)
            if binding["kind"] not in {"strict_file.write", "strict_file.patch"}:
                raise ValueError("not_a_strict_file_operation")
            if row["state"] == "accepted":
                return self._move(key, row, "failed", "none", result={"reason": "interrupted_preparation"})
            if row["state"] not in _ACTIVE:
                return row
            try:
                manifest = json.loads((self._artifacts(key) / "prepared.json").read_text())
                path = self._path(Path(manifest["path"]))
                with transactions.file_transaction_lock(self.state_dir, path, blocking=False):
                    with self._parent(path) as parent:
                        return self._reconcile_locked(key, row, path, parent)
            except BlockingIOError:
                raise  # A live holder still owns the resource; do not change its state.
            except (OSError, ValueError, KeyError, TypeError):
                return self._uncertain(key, row, "missing_or_invalid_evidence")

    def _uncertain(self, key, row, reason):
        if row["state"] == "needs_reconciliation":
            return row
        return self._move(key, row, "needs_reconciliation", "unknown",
                          result={"reason": reason}, action="manual_review")

    def _reconcile_locked(self, key, row, path, parent):
        artifact = self._artifacts(key)
        raw_manifest = (artifact / "prepared.json").read_bytes()
        if recovery.sha256_bytes(raw_manifest) != (row.get("result") or {}).get("manifest_sha256"):
            return self._uncertain(key, row, "manifest_changed")
        manifest = json.loads(raw_manifest)
        with self._parent(path) as current_parent:
            if _identity(os.fstat(current_parent)) != _identity(os.fstat(parent)):
                return self._uncertain(key, row, "parent_identity_changed")
        if (manifest["key"] != key.encoded() or manifest["path"] != str(path)
                or manifest["workspace"] != str(self.workspace)
                or manifest["parent"] != _identity(os.fstat(parent))):
            return self._uncertain(key, row, "identity_changed")
        for image in ("before", "after"):
            if recovery.sha256_bytes((artifact / image).read_bytes()) != manifest[image]["sha256"]:
                return self._uncertain(key, row, "image_corrupt")
        _, current = _snapshot(parent, path.name)
        receipt_path = artifact / "committed.json"
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if (receipt["key"] == key.encoded() and receipt["attempt_id"] == manifest["attempt_id"]
                    and receipt["parent"] == manifest["parent"] and receipt["target"] == current
                    and all(current[field] == manifest["after"][field] for field in
                            ("dev", "ino", "sha256", "size", "mode"))):
                if row["state"] == "prepared":
                    return self._uncertain(key, row, "receipt_inconsistent_with_state")
                if row["state"] != "verifying":
                    row = self._move(key, row, "verifying", "committed")
                return self._move(key, row, "succeeded", "committed",
                                  result={"attempt_id": manifest["attempt_id"],
                                          "sha256": current["sha256"], "receipt": str(receipt_path)})
            return self._uncertain(key, row, "receipt_or_target_changed")
        if current == manifest["before"]:
            if row["state"] != "needs_reconciliation":
                row = self._move(key, row, "needs_reconciliation", "unknown")
            return self._move(key, row, "failed", "none", result={"reason": "not_applied"},
                              action="new_operation_after_review")
        # Matching after bytes alone cannot prove this attempt committed. Even a
        # matching inode without a durable receipt stays uncertain across crashes.
        return self._uncertain(key, row, "unconfirmed_after_or_external_change")
