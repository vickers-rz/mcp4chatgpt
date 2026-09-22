"""Managed Git worktrees for isolated concurrent coding sessions."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Config
from ..safety import redact, resolve_allowed_path
from .worktree_store import WorktreeStore


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store(config: Config) -> WorktreeStore:
    return WorktreeStore(Path(config.data_dir) / "managed-worktrees")


def _validate_operation_id(operation_id: str) -> str:
    value = str(operation_id).strip()
    if not value:
        raise ValueError("operation_id_required")
    if len(value) > 128:
        raise ValueError("operation_id_too_long")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("operation_id_contains_control_character")
    return value


def _git(
    cwd: Path,
    *args: str,
    timeout: int = 30,
) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed.stdout.strip()


def _source_repo(config: Config, cwd: str) -> Path:
    resolved = resolve_allowed_path(cwd, config.allowed_roots, must_exist=True)
    if not resolved.is_dir():
        raise ValueError(f"Not a directory: {resolved}")
    try:
        root = Path(_git(resolved, "rev-parse", "--show-toplevel")).resolve()
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"not_a_git_repository: {resolved}") from exc

    # The repository root itself must be within configured authority. A caller
    # cannot point at an allowed subdirectory merely to gain authority over the
    # parent repository.
    return resolve_allowed_path(
        str(root),
        config.allowed_roots,
        must_exist=True,
    )


def _resolve_commit(repo: Path, base_ref: str) -> str:
    ref = str(base_ref or "HEAD").strip() or "HEAD"
    try:
        commit = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"invalid_base_ref: {ref}") from exc
    if len(commit) != 40 or any(ch not in "0123456789abcdefABCDEF" for ch in commit):
        raise ValueError(f"invalid_resolved_commit: {commit}")
    return commit.lower()


def _status_porcelain(repo: Path) -> str:
    return _git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
    )


def _worktree_entries(repo: Path) -> dict[Path, dict[str, str]]:
    output = _git(repo, "worktree", "list", "--porcelain")
    entries: dict[Path, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in output.splitlines() + [""]:
        if not line:
            path = current.get("worktree")
            if path:
                entries[Path(path).resolve()] = dict(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return entries


def _managed_root(repo: Path) -> Path:
    repo_hash = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:12]
    return repo.parent / ".mcp4chatgpt-worktrees" / f"{repo.name}-{repo_hash}"


def _fingerprint(repo: Path, base_commit: str) -> str:
    payload = json.dumps(
        {
            "source_repo": str(repo),
            "base_commit": base_commit,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _observed_record(
    record: dict[str, Any],
) -> dict[str, Any]:
    result = dict(record)
    repo = Path(str(record["source_repo"])).resolve()
    destination = Path(str(record["path"])).resolve()
    registered = False
    actual_head = None
    dirty = None

    try:
        entry = _worktree_entries(repo).get(destination)
    except (OSError, subprocess.SubprocessError):
        entry = None
    if entry is not None:
        registered = True
        actual_head = entry.get("HEAD")
        try:
            dirty = bool(_status_porcelain(destination))
        except (OSError, subprocess.SubprocessError):
            dirty = None

    result.update(
        {
            "registered": registered,
            "exists": destination.exists(),
            "actual_head": actual_head,
            "base_matches": actual_head == record.get("base_commit"),
            "dirty": dirty,
        }
    )
    return result


def create_worktree(
    config: Config,
    *,
    operation_id: str,
    cwd: str,
    base_ref: str = "HEAD",
) -> dict[str, Any]:
    """Create one detached managed worktree from an exact clean-source commit."""

    operation_id = _validate_operation_id(operation_id)
    repo = _source_repo(config, cwd)
    base_commit = _resolve_commit(repo, base_ref)
    fingerprint = _fingerprint(repo, base_commit)
    store = _store(config)

    with store.operation_lock(operation_id):
        existing = store.read_operation(operation_id)
        if existing is not None:
            if existing.get("fingerprint") != fingerprint:
                raise ValueError(
                    "idempotency_conflict: operation_id already belongs to "
                    "a different worktree request"
                )

            worktree_id = str(existing["worktree_id"])
            try:
                record = store.read_record(worktree_id)
            except ValueError:
                destination = Path(str(existing["destination"])).resolve()
                entry = _worktree_entries(repo).get(destination)
                if entry is not None and entry.get("HEAD") == base_commit:
                    record = {
                        "schema_version": 1,
                        "worktree_id": worktree_id,
                        "operation_id": operation_id,
                        "fingerprint": fingerprint,
                        "source_repo": str(repo),
                        "path": str(destination),
                        "base_commit": base_commit,
                        "base_ref": base_ref,
                        "state": "active",
                        "created_at": existing.get("created_at") or _iso_now(),
                        "recovered_from_reserved": True,
                    }
                    store.write_record(record)
                    store.update_operation(
                        operation_id,
                        {"state": "active", "recovered_at": _iso_now()},
                    )
                    return {
                        **_observed_record(record),
                        "replayed": True,
                        "created": False,
                        "recovered": True,
                    }

                return {
                    "schema_version": 1,
                    "worktree_id": worktree_id,
                    "operation_id": operation_id,
                    "fingerprint": fingerprint,
                    "source_repo": str(repo),
                    "path": str(destination),
                    "base_commit": base_commit,
                    "state": "reserved",
                    "replayed": True,
                    "created": False,
                    "recovered": False,
                    "recovery": "explicit_repair_required",
                }

            return {
                **_observed_record(record),
                "replayed": True,
                "created": False,
                "recovered": False,
            }

        dirty = _status_porcelain(repo)
        if dirty:
            raise ValueError(
                "source_checkout_dirty: managed worktrees are based on committed "
                "state only; commit/checkpoint the source checkout first"
            )

        worktree_id = "wt_" + uuid.uuid4().hex[:24]
        destination = (_managed_root(repo) / worktree_id).resolve()
        created_at = _iso_now()
        store.reserve_operation(
            operation_id=operation_id,
            fingerprint=fingerprint,
            worktree_id=worktree_id,
            source_repo=repo,
            base_commit=base_commit,
            destination=destination,
        )
        store.update_operation(
            operation_id,
            {"created_at": created_at, "base_ref": base_ref},
        )

        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            _git(
                repo,
                "worktree",
                "add",
                "--detach",
                str(destination),
                base_commit,
                timeout=60,
            )
            entry = _worktree_entries(repo).get(destination)
            if entry is None or entry.get("HEAD") != base_commit:
                raise RuntimeError("managed_worktree_verification_failed")
        except Exception as exc:
            # Do not retry automatically after an uncertain Git mutation. The
            # durable reservation gives an explicit reconciliation handle.
            store.update_operation(
                operation_id,
                {
                    "state": "uncertain",
                    "error": redact(str(exc)),
                    "failed_at": _iso_now(),
                },
            )
            raise RuntimeError(
                f"managed_worktree_create_uncertain: {worktree_id}"
            ) from exc

        record = {
            "schema_version": 1,
            "worktree_id": worktree_id,
            "operation_id": operation_id,
            "fingerprint": fingerprint,
            "source_repo": str(repo),
            "path": str(destination),
            "base_commit": base_commit,
            "base_ref": base_ref,
            "state": "active",
            "created_at": created_at,
        }
        store.write_record(record)
        store.update_operation(
            operation_id,
            {"state": "active", "activated_at": _iso_now()},
        )
        return {
            **_observed_record(record),
            "replayed": False,
            "created": True,
            "recovered": False,
        }


def worktree_status(config: Config, worktree_id: str) -> dict[str, Any]:
    store = _store(config)
    return _observed_record(store.read_record(worktree_id))


def list_worktrees(config: Config, limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    store = _store(config)
    return {
        "worktrees": [
            _observed_record(record)
            for record in store.list_records()[:limit]
        ]
    }


def remove_worktree(
    config: Config,
    worktree_id: str,
) -> dict[str, Any]:
    """Remove one clean managed worktree; replay after removal is a no-op."""

    store = _store(config)
    worktree_id = store.validate_worktree_id(worktree_id)
    with store.worktree_lock(worktree_id):
        record = store.read_record(worktree_id)
        if record.get("state") == "removed":
            return {
                **_observed_record(record),
                "removed": False,
                "already_removed": True,
            }

        if record.get("state") != "active":
            raise ValueError(
                f"worktree_not_removable_in_state: {record.get('state')}"
            )

        repo = Path(str(record["source_repo"])).resolve()
        destination = Path(str(record["path"])).resolve()
        entries = _worktree_entries(repo)
        entry = entries.get(destination)

        if entry is None:
            if destination.exists():
                raise ValueError(
                    "worktree_registration_missing_but_path_exists: "
                    "explicit_repair_required"
                )
        else:
            dirty = _status_porcelain(destination)
            if dirty:
                raise ValueError(
                    "managed_worktree_dirty: commit or discard worktree changes "
                    "before removal"
                )
            _git(
                repo,
                "worktree",
                "remove",
                str(destination),
                timeout=60,
            )
            if destination in _worktree_entries(repo):
                raise RuntimeError("managed_worktree_remove_verification_failed")

        record.update(
            {
                "state": "removed",
                "removed_at": _iso_now(),
            }
        )
        store.write_record(record)
        return {
            **_observed_record(record),
            "removed": True,
            "already_removed": False,
        }
