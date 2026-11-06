"""Validated coding-task lifecycle built on managed Git worktrees."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ..config import Config
from ..jobs.process import process_identity
from ..safety import redact, validate_command
from . import recovery, worktrees
from .worktree_store import WorktreeStore


_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ZERO_OID = "0" * 40


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store(config: Config) -> WorktreeStore:
    return WorktreeStore(Path(config.data_dir) / "managed-worktrees")


def validate_task_id(task_id: str) -> str:
    value = str(task_id).strip()
    if not _TASK_ID_RE.fullmatch(value):
        raise ValueError(
            "invalid_coding_task_id: use 1-64 ASCII letters, digits, '_' or '-', "
            "starting with a letter or digit"
        )
    return value


def _validate_oid(value: str, *, field: str) -> str:
    oid = str(value).strip().lower()
    if len(oid) != 40 or any(ch not in "0123456789abcdef" for ch in oid):
        raise ValueError(f"invalid_{field}: expected exact 40-hex Git commit")
    return oid


def normalize_declared_paths(paths: list[Any]) -> list[str]:
    if not isinstance(paths, list) or not paths or len(paths) > 200:
        raise ValueError("declared_paths_required: provide 1-200 repository-relative paths")
    normalized: set[str] = set()
    for raw in paths:
        if not isinstance(raw, str):
            raise ValueError("invalid_declared_path")
        value = raw.strip().replace("\\", "/")
        if not value or len(value) > 512:
            raise ValueError("invalid_declared_path")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"invalid_declared_path: {raw}")
        canonical = path.as_posix()
        if canonical in ("", ".", ".git") or canonical.startswith(".git/"):
            raise ValueError(f"invalid_declared_path: {raw}")
        normalized.add(canonical)
    return sorted(normalized)


def _task_fingerprint(
    *,
    task_id: str,
    repo: Path,
    base_commit: str,
    declared_paths: list[str],
) -> str:
    payload = json.dumps(
        {
            "task_id": task_id,
            "source_repo": str(repo),
            "base_commit": base_commit,
            "declared_paths": declared_paths,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validation_fingerprint(
    *,
    command: str,
    timeout_sec: int,
) -> str:
    payload = json.dumps(
        {
            "command": command,
            "timeout_sec": timeout_sec,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _task_key(
    store: WorktreeStore,
    *,
    principal_hash: str,
    workspace_id: str,
    task_id: str,
) -> str:
    return store.task_key(
        principal_hash=principal_hash,
        workspace_id=workspace_id,
        task_id=task_id,
    )


def _git_result(
    cwd: Path,
    *args: str,
    timeout: int = 30,
    env: dict[str, str] | None = None,
    input: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        timeout=timeout,
        env=env,
        input=input,
    )


def _git(cwd: Path, *args: str, timeout: int = 30, env: dict[str, str] | None = None) -> str:
    result = _git_result(cwd, *args, timeout=timeout, env=env)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git_command_failed: {' '.join(args)}: {redact(message)}")
    return result.stdout.decode("utf-8", errors="replace").strip()


def _git_commit_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "MCP4ChatGPT Coding Task",
            "GIT_AUTHOR_EMAIL": "coding-task@mcp4chatgpt.local",
            "GIT_COMMITTER_NAME": "MCP4ChatGPT Coding Task",
            "GIT_COMMITTER_EMAIL": "coding-task@mcp4chatgpt.local",
        }
    )
    return env


def _split_z(data: bytes) -> list[str]:
    return [os.fsdecode(part) for part in data.split(b"\0") if part]


def _changed_paths(worktree: Path, base_commit: str) -> list[str]:
    tracked = _git_result(
        worktree,
        "diff",
        "--no-renames",
        "--name-only",
        "-z",
        base_commit,
        "--",
    )
    if tracked.returncode != 0:
        raise RuntimeError("coding_task_diff_failed")
    untracked = _git_result(
        worktree,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    if untracked.returncode != 0:
        raise RuntimeError("coding_task_untracked_scan_failed")
    return sorted(set(_split_z(tracked.stdout) + _split_z(untracked.stdout)))


def _commit_changed_paths(repo: Path, base_commit: str, candidate_commit: str) -> list[str]:
    result = _git_result(
        repo,
        "diff",
        "--no-renames",
        "--name-only",
        "-z",
        base_commit,
        candidate_commit,
        "--",
    )
    if result.returncode != 0:
        raise RuntimeError("coding_task_candidate_diff_failed")
    return sorted(set(_split_z(result.stdout)))


def _ref_target(repo: Path, ref: str) -> str | None:
    result = _git_result(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if result.returncode != 0:
        return None
    value = result.stdout.decode("ascii", errors="ignore").strip().lower()
    return value if len(value) == 40 else None


def _assert_task_scope(
    task: dict[str, Any],
    *,
    principal_hash: str,
    workspace_id: str,
    repo: Path,
) -> None:
    scope = task.get("scope")
    if (
        not isinstance(scope, dict)
        or scope.get("principal_hash") != principal_hash
        or scope.get("workspace_id") != workspace_id
        or task.get("source_repo") != str(repo)
    ):
        raise ValueError(f"coding_task_not_found: {task.get('task_id', '')}")


def _read_task(
    store: WorktreeStore,
    task_key: str,
    *,
    task_id: str,
    principal_hash: str,
    workspace_id: str,
    repo: Path,
) -> dict[str, Any]:
    task = store.read_task(task_key)
    if task is None:
        raise ValueError(f"coding_task_not_found: {task_id}")
    _assert_task_scope(
        task,
        principal_hash=principal_hash,
        workspace_id=workspace_id,
        repo=repo,
    )
    return task


def _require_resolved_validation(task: dict[str, Any]) -> None:
    if task.get("validation_attempt") is not None:
        raise ValueError("coding_task_validation_unresolved: reconcile validation first")


@contextmanager
def task_file(
    config: Config,
    *,
    repo: Path,
    principal_hash: str,
    workspace_id: str,
    task_id: str,
    path: str,
    write: bool,
):
    task_id = validate_task_id(task_id)
    relative = PurePosixPath(path)
    if (
        not relative.parts
        or relative.is_absolute()
        or "\\" in path
        or any(part == ".." or part.casefold() == ".git" for part in relative.parts)
    ):
        raise ValueError("invalid_coding_task_file_path")
    store = _store(config)
    key = _task_key(store, principal_hash=principal_hash, workspace_id=workspace_id, task_id=task_id)
    with store.task_lock(key):
        task = _read_task(store, key, task_id=task_id, repo=repo,
                          principal_hash=principal_hash, workspace_id=workspace_id)
        if write:
            _require_resolved_validation(task)
            if relative.as_posix() not in task["declared_paths"]:
                raise ValueError("coding_task_file_not_declared")
        worktree_id = task["worktree_id"]
        with store.worktree_lock(worktree_id):
            record = store.read_record(worktree_id)
            scope = record.get("scope") or {}
            if (scope.get("coding_task_key", key) != key
                    or record["source_repo"] != str(repo)
                    or record["path"] != task["path"]):
                raise ValueError("coding_task_worktree_owner_mismatch")
            observed = worktrees.worktree_status(
                config, worktree_id, expected_principal_hash=principal_hash,
                expected_workspace_id=workspace_id, strict_scope=True,
            )
            checkout = Path(task["path"])
            if (observed["state"] != "active" or not observed["registered"]
                    or not observed["exists"] or checkout.resolve() != checkout):
                raise ValueError("coding_task_worktree_not_active")
            target = checkout
            for part in relative.parts:
                target = target / part
                if target.is_symlink():
                    raise ValueError("invalid_coding_task_file_path")
            if not target.resolve().is_relative_to(checkout):
                raise ValueError("invalid_coding_task_file_path")
            if target.exists() and not target.is_file():
                raise ValueError("invalid_coding_task_file_path")
            yield checkout, target


def _validation_is_current(
    task: dict[str, Any],
    observed: dict[str, Any],
    validation: dict[str, Any] | None,
) -> bool:
    if task.get("validation_attempt") is not None:
        return False
    if not isinstance(validation, dict) or validation.get("validated") is not True:
        return False
    candidate = validation.get("candidate_commit")
    tree = validation.get("candidate_tree")
    if (
        observed.get("registered") is not True
        or observed.get("exists") is not True
        or observed.get("dirty") is not False
        or observed.get("actual_head") != candidate
    ):
        return False
    current_tree = _git(
        Path(str(task["path"])).resolve(),
        "rev-parse",
        "HEAD^{tree}",
    )
    return current_tree == tree


def _observe_task(
    config: Config,
    task: dict[str, Any],
    *,
    principal_hash: str,
    workspace_id: str,
) -> dict[str, Any]:
    observed = worktrees.worktree_status(
        config,
        str(task["worktree_id"]),
        expected_principal_hash=principal_hash,
        expected_workspace_id=workspace_id,
        strict_scope=True,
    )
    repo = Path(str(task["source_repo"])).resolve()
    validation = task.get("validation")
    validation_current = _validation_is_current(
        task,
        observed,
        validation if isinstance(validation, dict) else None,
    )

    task_ref = str(task["task_ref"])
    ref_target = _ref_target(repo, task_ref)
    published_commit = task.get("published_commit")
    source_head = _git(repo, "rev-parse", "HEAD").lower()
    source_dirty = bool(worktrees._status_porcelain(repo))
    integrated_source_commit = task.get("integrated_source_commit")
    return {
        **task,
        "worktree": observed,
        "validation_current": validation_current,
        "ref_target": ref_target,
        "publication_current": bool(
            published_commit and ref_target == published_commit
        ),
        "source_head": source_head,
        "source_dirty": source_dirty,
        "integration_current": bool(
            integrated_source_commit
            and published_commit
            and integrated_source_commit == published_commit
            and source_head == integrated_source_commit
            and ref_target == published_commit
            and not source_dirty
        ),
    }


def create_task(
    config: Config,
    *,
    task_id: str,
    repo: Path,
    base_ref: str,
    declared_paths: list[Any],
    reservation_id: str,
    scope_metadata: dict[str, Any],
) -> dict[str, Any]:
    task_id = validate_task_id(task_id)
    paths = normalize_declared_paths(declared_paths)
    repo = repo.resolve()
    base_ref = str(base_ref or "HEAD").strip() or "HEAD"
    store = _store(config)
    principal_hash = str(scope_metadata["principal_hash"])
    workspace_id = str(scope_metadata["workspace_id"])
    task_key = _task_key(
        store,
        principal_hash=principal_hash,
        workspace_id=workspace_id,
        task_id=task_id,
    )
    with store.task_lock(task_key):
        existing = store.read_task(task_key)
        if existing is not None:
            _assert_task_scope(
                existing,
                principal_hash=principal_hash,
                workspace_id=workspace_id,
                repo=repo,
            )
            if ((str(existing.get("base_ref") or "HEAD").strip() or "HEAD") != base_ref
                    or existing.get("declared_paths") != paths):
                raise ValueError(
                    "idempotency_conflict: task_id already belongs to a different "
                    "coding task request"
                )
            with store.operation_lock(reservation_id):
                reservation = store.read_operation(reservation_id)
                worktree_id = existing["worktree_id"]
                if reservation is not None and reservation["worktree_id"] != worktree_id:
                    raise ValueError("coding_task_worktree_owner_mismatch")
                with store.worktree_lock(worktree_id):
                    record = store.read_record(worktree_id)
                    owner_scope = {**scope_metadata, "coding_task_key": task_key}
                    for item in (reservation, record):
                        if item is not None:
                            scope = item.get("scope") or {}
                            if scope.get("coding_task_key", task_key) != task_key:
                                raise ValueError("coding_task_worktree_owner_mismatch")
                    if reservation is not None and reservation.get("scope") != owner_scope:
                        store.update_operation(reservation_id, {"scope": owner_scope})
                    if record.get("scope") != owner_scope:
                        record["scope"] = owner_scope
                        store.write_record(record)
            return {
                **_observe_task(
                    config,
                    existing,
                    principal_hash=principal_hash,
                    workspace_id=workspace_id,
                ),
                "replayed": True,
                "created": False,
            }

        intent_dir = store.root / "task-intents"
        intent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        intent_path = intent_dir / f"{task_key}.json"
        intent = store._read_json(intent_path)
        if intent is None:
            # Legacy reservations did not persist the selector and path set.
            # Keep their existing resolution and conflict behavior.
            base_commit = worktrees._resolve_commit(repo, base_ref)
            with store.operation_lock(reservation_id):
                if store.read_operation(reservation_id) is None:
                    intent = {"base_ref": base_ref, "declared_paths": paths, "base_commit": base_commit}
                    store._write_json(intent_path, intent)
        else:
            if intent.get("base_ref") != base_ref or intent.get("declared_paths") != paths:
                raise ValueError("idempotency_conflict: task_id already belongs to a different coding task request")
            base_commit = str(intent["base_commit"])
        fingerprint = _task_fingerprint(
            task_id=task_id, repo=repo, base_commit=base_commit, declared_paths=paths,
        )
        worktree = worktrees.create_worktree(
            config,
            operation_id=f"coding-task:{task_id}",
            cwd=str(repo),
            base_ref=base_commit,
            reservation_id=reservation_id,
            scope_metadata={**scope_metadata, "coding_task_key": task_key},
        )
        if worktree.get("state") != "active" or not worktree.get("registered"):
            raise ValueError("coding_task_worktree_not_active")
        task = {
            "schema_version": 1,
            "task_id": task_id,
            "task_key": task_key,
            "fingerprint": fingerprint,
            "scope": dict(scope_metadata),
            "source_repo": str(repo),
            "base_ref": base_ref,
            "base_commit": str(worktree["base_commit"]),
            "declared_paths": paths,
            "worktree_id": str(worktree["worktree_id"]),
            "path": str(worktree["path"]),
            "task_ref": f"refs/mcp4chatgpt/scoped-tasks/{task_key}",
            "state": "editing",
            "validation": None,
            "validation_history": [],
            "validation_attempt": None,
            "validation_attempt_history": [],
            "published_commit": None,
            "integrated_source_commit": None,
            "integrated_at": None,
            "created_at": _iso_now(),
            "updated_at": _iso_now(),
        }
        store.write_task(task_key, task)
        return {
            **_observe_task(
                config,
                task,
                principal_hash=principal_hash,
                workspace_id=workspace_id,
            ),
            "replayed": False,
            "created": True,
            "worktree_replayed": bool(worktree.get("replayed")),
        }


def inspect_task(
    config: Config,
    *,
    task_id: str,
    repo: Path,
    principal_hash: str,
    workspace_id: str,
) -> dict[str, Any]:
    task_id = validate_task_id(task_id)
    store = _store(config)
    task_key = _task_key(
        store,
        principal_hash=principal_hash,
        workspace_id=workspace_id,
        task_id=task_id,
    )
    with store.task_lock(task_key):
        task = _read_task(
            store,
            task_key,
            task_id=task_id,
            principal_hash=principal_hash,
            workspace_id=workspace_id,
            repo=repo,
        )
        return _observe_task(
            config,
            task,
            principal_hash=principal_hash,
            workspace_id=workspace_id,
        )


def _prepare_candidate(task: dict[str, Any]) -> tuple[str, str, list[str]]:
    worktree = Path(str(task["path"])).resolve()
    base_commit = str(task["base_commit"])
    declared = set(task["declared_paths"])

    ancestor = _git_result(
        worktree,
        "merge-base",
        "--is-ancestor",
        base_commit,
        "HEAD",
    )
    if ancestor.returncode != 0:
        raise ValueError("coding_task_history_diverged_from_baseline")

    changed = _changed_paths(worktree, base_commit)
    unexpected = sorted(set(changed) - declared)
    if unexpected:
        raise ValueError(
            "coding_task_undeclared_changes: "
            + json.dumps(unexpected, ensure_ascii=False)
        )

    unstaged = _git_result(worktree, "diff", "--no-renames", "--name-only", "-z", "--")
    if unstaged.returncode != 0:
        raise RuntimeError("coding_task_diff_failed")
    untracked = _git_result(worktree, "ls-files", "--others", "--exclude-standard", "-z")
    if untracked.returncode != 0:
        raise RuntimeError("coding_task_untracked_scan_failed")
    to_stage = sorted(set(_split_z(unstaged.stdout) + _split_z(untracked.stdout)))
    unexpected = sorted(set(to_stage) - declared)
    if unexpected:
        raise ValueError("coding_task_undeclared_changes: " + json.dumps(unexpected, ensure_ascii=False))
    if to_stage:
        add = _git_result(worktree, "--literal-pathspecs", "add", "-A", "--", *to_stage)
        if add.returncode != 0:
            raise RuntimeError("coding_task_stage_failed")

    staged = _git_result(worktree, "diff", "--cached", "--no-renames", "--name-only", "-z", base_commit, "--")
    if staged.returncode != 0:
        raise RuntimeError("coding_task_index_check_failed")
    unexpected = sorted(set(_split_z(staged.stdout)) - declared)
    if unexpected:
        raise ValueError("coding_task_candidate_contains_undeclared_paths: " + json.dumps(unexpected, ensure_ascii=False))

    cached = _git_result(worktree, "diff", "--cached", "--quiet")
    if cached.returncode not in (0, 1):
        raise RuntimeError("coding_task_index_check_failed")
    if cached.returncode == 1:
        result = _git_result(
            worktree,
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            f"MCP4ChatGPT coding task {task['task_id']}",
            timeout=60,
            env=_git_commit_env(),
        )
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"coding_task_commit_failed: {redact(message)}")

    candidate = _git(worktree, "rev-parse", "HEAD").lower()
    candidate_tree = _git(worktree, "rev-parse", "HEAD^{tree}").lower()
    base_tree = _git(worktree, "rev-parse", f"{base_commit}^{{tree}}").lower()
    if candidate_tree == base_tree:
        raise ValueError("coding_task_has_no_changes")

    if worktrees._status_porcelain(worktree):
        raise ValueError("coding_task_candidate_worktree_not_clean")

    committed_paths = _commit_changed_paths(worktree, base_commit, candidate)
    unexpected_committed = sorted(set(committed_paths) - declared)
    if unexpected_committed:
        raise ValueError(
            "coding_task_candidate_contains_undeclared_paths: "
            + json.dumps(unexpected_committed, ensure_ascii=False)
        )
    return candidate, candidate_tree, committed_paths


def _tail(path: Path, max_bytes: int = 4096) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            return stream.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _validation_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    # Zombies cannot write files or hold log descriptors. A failed process
    # query must propagate rather than being interpreted as an empty group.
    listing = subprocess.run(
        ["ps", "-axo", "pgid=,stat="],
        capture_output=True, text=True, check=True, timeout=1,
    )
    for line in listing.stdout.splitlines():
        group, state = line.split()
        if int(group) == pgid and not state.startswith("Z"):
            return True
    return False


def _wait_validation_group(process: subprocess.Popen[bytes], deadline: float) -> bool:
    while True:
        process.poll()
        if not _validation_group_alive(process.pid):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def _terminate_validation_group(process: subprocess.Popen[bytes]) -> None:
    for sig, grace in ((signal.SIGTERM, 2), (signal.SIGKILL, 5)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        if _wait_validation_group(process, time.monotonic() + grace):
            return
    raise RuntimeError("coding_task_validation_group_exit_unconfirmed")


def _validation_response(
    config: Config,
    task: dict[str, Any],
    validation: dict[str, Any],
    *,
    principal_hash: str,
    workspace_id: str,
    replayed: bool,
) -> dict[str, Any]:
    observed_task = _observe_task(
        config,
        task,
        principal_hash=principal_hash,
        workspace_id=workspace_id,
    )
    current = _validation_is_current(
        task,
        observed_task["worktree"],
        validation,
    )
    return {
        **observed_task,
        "validation": validation,
        "validation_current": current,
        "validation_replayed": replayed,
        "stdout_tail": redact(_tail(Path(str(validation["stdout_path"])))),
        "stderr_tail": redact(_tail(Path(str(validation["stderr_path"])))),
    }


def validate_task(
    config: Config,
    *,
    task_id: str,
    repo: Path,
    principal_hash: str,
    workspace_id: str,
    operation_id: str,
    command: str,
    timeout_sec: int = 300,
) -> dict[str, Any]:
    task_id = validate_task_id(task_id)
    operation_id = worktrees._validate_operation_id(operation_id)
    command = validate_command(
        command,
        personal_full_access=getattr(config, "personal_full_access", False),
    )
    timeout_sec = max(1, min(int(timeout_sec), 1800))
    validation_fingerprint = _validation_fingerprint(
        command=command,
        timeout_sec=timeout_sec,
    )
    store = _store(config)
    task_key = _task_key(
        store,
        principal_hash=principal_hash,
        workspace_id=workspace_id,
        task_id=task_id,
    )

    with store.task_lock(task_key):
        task = _read_task(
            store,
            task_key,
            task_id=task_id,
            principal_hash=principal_hash,
            workspace_id=workspace_id,
            repo=repo,
        )
        history = task.get("validation_history")
        if not isinstance(history, list):
            history = []
        for previous in history:
            if not isinstance(previous, dict) or previous.get("operation_id") != operation_id:
                continue
            if previous.get("fingerprint") != validation_fingerprint:
                raise ValueError(
                    "idempotency_conflict: validation operation_id already belongs "
                    "to different command or timeout parameters"
                )
            return _validation_response(
                config,
                task,
                previous,
                principal_hash=principal_hash,
                workspace_id=workspace_id,
                replayed=True,
            )

        attempt_history = task.get("validation_attempt_history")
        if not isinstance(attempt_history, list):
            attempt_history = []
        for previous_attempt in attempt_history:
            if (
                isinstance(previous_attempt, dict)
                and previous_attempt.get("operation_id") == operation_id
            ):
                raise ValueError(
                    "coding_task_validation_operation_abandoned: "
                    "use a new operation_id"
                )

        active_attempt = task.get("validation_attempt")
        resume_prepared: dict[str, Any] | None = None
        if isinstance(active_attempt, dict):
            if active_attempt.get("operation_id") != operation_id:
                raise ValueError(
                    "coding_task_validation_in_progress_or_uncertain: "
                    f"{active_attempt.get('operation_id')}"
                )
            if active_attempt.get("fingerprint") != validation_fingerprint:
                raise ValueError(
                    "idempotency_conflict: validation operation_id already belongs "
                    "to different command or timeout parameters"
                )
            if active_attempt.get("state") == "prepared":
                resume_prepared = active_attempt
            else:
                raise ValueError(
                    "coding_task_validation_uncertain: operation may have started; "
                    "inspect and reconcile it before retrying"
                )

        observed = worktrees.worktree_status(
            config,
            str(task["worktree_id"]),
            expected_principal_hash=principal_hash,
            expected_workspace_id=workspace_id,
            strict_scope=True,
        )
        if (
            observed.get("state") != "active"
            or observed.get("registered") is not True
            or observed.get("exists") is not True
        ):
            raise ValueError("coding_task_worktree_not_active")

        worktree = Path(str(task["path"])).resolve()
        if resume_prepared is not None:
            candidate = str(resume_prepared["candidate_commit"])
            candidate_tree = str(resume_prepared["candidate_tree"])
            committed_paths = list(resume_prepared["committed_paths"])
            validation_id = str(resume_prepared["validation_id"])
            evidence_dir = Path(str(resume_prepared["evidence_dir"]))
            stdout_path = Path(str(resume_prepared["stdout_path"]))
            stderr_path = Path(str(resume_prepared["stderr_path"]))
            if (
                _git(worktree, "rev-parse", "HEAD").lower() != candidate
                or _git(worktree, "rev-parse", "HEAD^{tree}").lower()
                != candidate_tree
                or worktrees._status_porcelain(worktree)
            ):
                raise ValueError(
                    "coding_task_validation_prepared_candidate_stale: "
                    "reconcile the operation and start a new validation"
                )
        else:
            candidate, candidate_tree, committed_paths = _prepare_candidate(task)
            validation_id = "val_" + uuid.uuid4().hex[:24]
            evidence_dir = (
                Path(config.data_dir)
                / "managed-worktrees"
                / "evidence"
                / task_key
                / validation_id
            )
            stdout_path = evidence_dir / "stdout.log"
            stderr_path = evidence_dir / "stderr.log"
            resume_prepared = {
                "operation_id": operation_id,
                "fingerprint": validation_fingerprint,
                "state": "prepared",
                "validation_id": validation_id,
                "candidate_commit": candidate,
                "candidate_tree": candidate_tree,
                "committed_paths": committed_paths,
                "evidence_dir": str(evidence_dir),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "prepared_at": _iso_now(),
            }
            task.update(
                {
                    "candidate_commit": candidate,
                    "candidate_tree": candidate_tree,
                    "validation_attempt": resume_prepared,
                    "state": "validation_prepared",
                    "updated_at": _iso_now(),
                }
            )
            store.write_task(task_key, task)

        evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A persisted prepared state guarantees that the command was not started.
        # Empty/partial log files from a crash before the running marker are safe
        # to replace when resuming that same operation.
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)
        (evidence_dir / "evidence.json").unlink(missing_ok=True)

        shell = os.environ.get("SHELL", "/bin/zsh")
        started_at = _iso_now()
        timed_out = False
        effects_may_remain = False
        exit_code: int | None = None
        stdout_fd = os.open(
            stdout_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            stderr_fd = os.open(
                stderr_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except Exception:
            os.close(stdout_fd)
            raise

        attempt = dict(resume_prepared)
        attempt.update(
            {
                "state": "running",
                "started_at": started_at,
                "shell": shell,
                "launch_protocol": "gated_v1",
                "effects_may_remain": True,
            }
        )
        process: subprocess.Popen[bytes] | None = None
        gate_read, gate_write = os.pipe()
        try:
            with os.fdopen(stdout_fd, "wb", buffering=0) as stdout_stream, os.fdopen(
                stderr_fd, "wb", buffering=0
            ) as stderr_stream:
                deadline = time.monotonic() + timeout_sec
                process = subprocess.Popen(
                    [sys.executable, "-c",
                     "import os,sys; fd=int(sys.argv[1]); token=os.read(fd,1); os.close(fd); "
                     "os.execv(sys.argv[2], [sys.argv[2], '-lc', sys.argv[3]]) if token == b'1' else sys.exit(1)",
                     str(gate_read), shell, command],
                    cwd=worktree,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_stream,
                    stderr=stderr_stream,
                    start_new_session=True,
                    close_fds=True,
                    pass_fds=(gate_read,),
                    env=os.environ.copy(),
                )
                os.close(gate_read)
                gate_read = -1
                attempt.update(pid=process.pid, pgid=process.pid)
                identity = process_identity(process.pid)
                attempt["process_identity"] = (
                    hashlib.sha256(identity.encode()).hexdigest() if identity else None
                )
                task["validation_attempt"] = attempt
                task["state"] = "validation_running"
                task["updated_at"] = _iso_now()
                store.write_task(task_key, task)
                os.write(gate_write, b"1")
                os.close(gate_write)
                gate_write = -1
                try:
                    exit_code = process.wait(timeout=max(0, deadline - time.monotonic()))
                    timed_out = not _wait_validation_group(process, deadline)
                except subprocess.TimeoutExpired:
                    timed_out = True
                if timed_out:
                    effects_may_remain = True
                    _terminate_validation_group(process)
                    exit_code = process.wait(timeout=1)
        except Exception as exc:
            if process is None and isinstance(exc, OSError):
                attempt["launch_failed"] = True
            if process is not None and process.poll() is None:
                try:
                    _terminate_validation_group(process)
                except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
                    pass  # Preserve the unresolved attempt even if cleanup fails.
            attempt.update(
                {
                    "state": "uncertain",
                    "error": redact(str(exc)),
                    "uncertain_at": _iso_now(),
                    "effects_may_remain": effects_may_remain
                    or process is not None,
                }
            )
            task.update(
                {
                    "validation_attempt": attempt,
                    "state": "validation_uncertain",
                    "updated_at": _iso_now(),
                }
            )
            store.write_task(task_key, task)
            raise RuntimeError(
                "coding_task_validation_uncertain: explicit reconciliation required"
            ) from exc
        finally:
            if gate_read >= 0:
                os.close(gate_read)
            if gate_write >= 0:
                os.close(gate_write)

        finished_at = _iso_now()
        current_head = _git(worktree, "rev-parse", "HEAD").lower()
        current_tree = _git(worktree, "rev-parse", "HEAD^{tree}").lower()
        dirty = bool(worktrees._status_porcelain(worktree))
        workspace_unchanged = (
            current_head == candidate
            and current_tree == candidate_tree
            and not dirty
        )
        validated = bool(
            exit_code == 0
            and not timed_out
            and workspace_unchanged
        )

        evidence = {
            "schema_version": 1,
            "validation_id": validation_id,
            "operation_id": operation_id,
            "fingerprint": validation_fingerprint,
            "task_id": task_id,
            "worktree_id": task["worktree_id"],
            "base_commit": task["base_commit"],
            "candidate_commit": candidate,
            "candidate_tree": candidate_tree,
            "declared_paths": list(task["declared_paths"]),
            "committed_paths": committed_paths,
            "command": command,
            "timeout_sec": timeout_sec,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "effects_may_remain": effects_may_remain,
            "workspace_unchanged": workspace_unchanged,
            "validated": validated,
            "started_at": started_at,
            "finished_at": finished_at,
            "environment": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python": sys.version.split()[0],
                "shell": shell,
                "git": _git(worktree, "--version"),
            },
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
        evidence_path = evidence_dir / "evidence.json"
        recovery.atomic_replace_bytes(
            evidence_path,
            (
                json.dumps(
                    evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8"),
            0o600,
        )

        validation_record = {
            **evidence,
            "command": redact(command),
            "evidence_path": str(evidence_path),
        }
        history.append(validation_record)
        attempt.update(
            {
                "state": "completed",
                "completed_at": finished_at,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "effects_may_remain": effects_may_remain,
                "evidence_path": str(evidence_path),
            }
        )
        attempt_history.append(dict(attempt))
        task.update(
            {
                "candidate_commit": candidate,
                "candidate_tree": candidate_tree,
                "validation": validation_record,
                "validation_history": history,
                "validation_attempt": None,
                "validation_attempt_history": attempt_history,
                "state": "validated" if validated else "validation_failed",
                "updated_at": _iso_now(),
            }
        )
        store.write_task(task_key, task)
        return _validation_response(
            config,
            task,
            validation_record,
            principal_hash=principal_hash,
            workspace_id=workspace_id,
            replayed=False,
        )


def reconcile_validation_task(
    config: Config,
    *,
    task_id: str,
    repo: Path,
    principal_hash: str,
    workspace_id: str,
    operation_id: str,
    resolution: str,
    confirm_effects_reviewed: bool,
    confirm_no_process_running: bool = False,
) -> dict[str, Any]:
    """Resolve a durable prepared/running/uncertain validation attempt."""

    task_id = validate_task_id(task_id)
    operation_id = worktrees._validate_operation_id(operation_id)
    if resolution not in {"abandon", "abandon_unknown_process"}:
        raise ValueError("unsupported_validation_reconciliation_resolution")
    if confirm_effects_reviewed is not True:
        raise ValueError(
            "validation_reconciliation_requires_effects_review_confirmation"
        )

    store = _store(config)
    task_key = _task_key(
        store,
        principal_hash=principal_hash,
        workspace_id=workspace_id,
        task_id=task_id,
    )
    with store.task_lock(task_key):
        task = _read_task(
            store,
            task_key,
            task_id=task_id,
            principal_hash=principal_hash,
            workspace_id=workspace_id,
            repo=repo,
        )

        for validation in task.get("validation_history") or []:
            if (
                isinstance(validation, dict)
                and validation.get("operation_id") == operation_id
            ):
                raise ValueError(
                    "coding_task_validation_already_completed: "
                    "reconciliation is not required"
                )

        attempt_history = task.get("validation_attempt_history")
        if not isinstance(attempt_history, list):
            attempt_history = []
        for previous in attempt_history:
            if (
                isinstance(previous, dict)
                and previous.get("operation_id") == operation_id
                and previous.get("state") == "abandoned"
            ):
                return {
                    **_observe_task(
                        config,
                        task,
                        principal_hash=principal_hash,
                        workspace_id=workspace_id,
                    ),
                    "reconciled": False,
                    "already_reconciled": True,
                    "resolution": previous.get("resolution", "abandon"),
                    "abandoned_attempt": previous,
                }

        active = task.get("validation_attempt")
        if not isinstance(active, dict) or active.get("operation_id") != operation_id:
            raise ValueError(
                f"coding_task_validation_attempt_not_found: {operation_id}"
            )
        if active.get("state") not in {"prepared", "running", "uncertain"}:
            raise ValueError(
                "coding_task_validation_attempt_not_reconcilable: "
                f"{active.get('state')}"
            )

        if resolution == "abandon_unknown_process":
            if (confirm_no_process_running is not True or active.get("launch_protocol") is not None
                    or active.get("state") not in {"running", "uncertain"}
                    or active.get("pid") is not None or active.get("pgid") is not None):
                raise ValueError("coding_task_unknown_process_confirmation_invalid")
        elif active.get("state") != "prepared" and not active.get("launch_failed"):
            pgid = active.get("pgid", active.get("pid"))
            if not isinstance(pgid, int) or pgid <= 0:
                raise ValueError("coding_task_validation_process_identity_unknown")
            try:
                alive = _validation_group_alive(pgid)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                raise ValueError(
                    "coding_task_validation_process_may_still_be_running"
                ) from exc
            if alive:
                raise ValueError("coding_task_validation_process_may_still_be_running")

        abandoned = {
            **active,
            "state": "abandoned",
            "resolution": resolution,
            "confirm_effects_reviewed": True,
            "confirm_no_process_running": confirm_no_process_running is True,
            "resolved_at": _iso_now(),
        }
        attempt_history.append(abandoned)

        published_commit = task.get("published_commit")
        publication_current = bool(
            isinstance(published_commit, str)
            and _ref_target(repo, str(task["task_ref"])) == published_commit
        )
        task.update(
            {
                "validation_attempt": None,
                "validation_attempt_history": attempt_history,
                "state": "published" if publication_current else "editing",
                "updated_at": _iso_now(),
            }
        )
        store.write_task(task_key, task)
        return {
            **_observe_task(
                config,
                task,
                principal_hash=principal_hash,
                workspace_id=workspace_id,
            ),
            "reconciled": True,
            "already_reconciled": False,
            "resolution": resolution,
            "abandoned_attempt": abandoned,
        }


def _migrate_task_ref(store: WorktreeStore, task_key: str, task: dict[str, Any], repo: Path) -> None:
    new_ref = f"refs/mcp4chatgpt/scoped-tasks/{task_key}"
    old_ref = str(task["task_ref"])
    if old_ref == new_ref:
        return
    published = task.get("published_commit")
    new_target = _ref_target(repo, new_ref)
    if published:
        if new_target not in (None, published):
            raise ValueError("coding_task_publish_conflict: legacy reference drift")
        action = "create" if new_target is None else "verify"
        result = _git_result(
            repo, "update-ref", "--stdin",
            input=f"verify {old_ref} {published}\n{action} {new_ref} {published}\n".encode(),
        )
        if result.returncode != 0:
            raise ValueError("coding_task_publish_conflict: legacy or scoped reference changed")
    elif new_target is not None:
        raise ValueError("coding_task_publish_conflict: scoped reference already exists")
    task["task_ref"] = new_ref
    store.write_task(task_key, task)


def publish_task(
    config: Config,
    *,
    task_id: str,
    repo: Path,
    principal_hash: str,
    workspace_id: str,
) -> dict[str, Any]:
    task_id = validate_task_id(task_id)
    store = _store(config)
    task_key = _task_key(
        store,
        principal_hash=principal_hash,
        workspace_id=workspace_id,
        task_id=task_id,
    )
    with store.task_lock(task_key):
        task = _read_task(
            store,
            task_key,
            task_id=task_id,
            principal_hash=principal_hash,
            workspace_id=workspace_id,
            repo=repo,
        )
        _require_resolved_validation(task)
        _migrate_task_ref(store, task_key, task, repo)
        observed = _observe_task(
            config,
            task,
            principal_hash=principal_hash,
            workspace_id=workspace_id,
        )
        validation = task.get("validation")
        if (
            not isinstance(validation, dict)
            or validation.get("validated") is not True
            or observed.get("validation_current") is not True
        ):
            raise ValueError("coding_task_validation_stale_or_missing")

        candidate = str(validation["candidate_commit"])
        task_ref = str(task["task_ref"])
        current_ref = _ref_target(repo, task_ref)
        published_commit = task.get("published_commit")

        if current_ref == candidate:
            task.update(
                {
                    "state": "published",
                    "published_commit": candidate,
                    "published_at": task.get("published_at") or _iso_now(),
                    "updated_at": _iso_now(),
                }
            )
            store.write_task(task_key, task)
            return {
                **_observe_task(
                    config,
                    task,
                    principal_hash=principal_hash,
                    workspace_id=workspace_id,
                ),
                "published": True,
                "replayed": bool(published_commit == candidate),
                "reconciled": bool(published_commit != candidate),
            }

        expected_old = str(published_commit or _ZERO_OID)
        if current_ref != (published_commit or None):
            raise ValueError(
                f"coding_task_publish_conflict: expected {published_commit or 'missing'}, "
                f"found {current_ref or 'missing'}"
            )

        result = _git_result(
            repo,
            "update-ref",
            task_ref,
            candidate,
            expected_old,
        )
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(f"coding_task_publish_conflict: {redact(message)}")
        if _ref_target(repo, task_ref) != candidate:
            raise RuntimeError("coding_task_publish_verification_failed")

        task.update(
            {
                "state": "published",
                "published_commit": candidate,
                "published_at": _iso_now(),
                "updated_at": _iso_now(),
            }
        )
        store.write_task(task_key, task)
        return {
            **_observe_task(
                config,
                task,
                principal_hash=principal_hash,
                workspace_id=workspace_id,
            ),
            "published": True,
            "replayed": False,
            "reconciled": False,
        }


def integrate_task(
    config: Config,
    *,
    task_id: str,
    repo: Path,
    principal_hash: str,
    workspace_id: str,
    expected_source_commit: str,
) -> dict[str, Any]:
    """Fast-forward one published task artifact into a clean source checkout."""

    task_id = validate_task_id(task_id)
    expected_source_commit = _validate_oid(
        expected_source_commit,
        field="expected_source_commit",
    )
    repo = repo.resolve()
    store = _store(config)
    task_key = _task_key(
        store,
        principal_hash=principal_hash,
        workspace_id=workspace_id,
        task_id=task_id,
    )

    # Serialize integrations for one repository workspace even when multiple task
    # records are being integrated concurrently.
    with store.task_lock(task_key):
        task = _read_task(
            store,
            task_key,
            task_id=task_id,
            principal_hash=principal_hash,
            workspace_id=workspace_id,
            repo=repo,
        )
        _require_resolved_validation(task)
        _migrate_task_ref(store, task_key, task, repo)
        with store.operation_lock(f"coding-integration:{workspace_id}"):
            published_commit = task.get("published_commit")
            if not isinstance(published_commit, str):
                raise ValueError("coding_task_not_published")
            candidate = _validate_oid(
                published_commit,
                field="published_commit",
            )
            task_ref = str(task["task_ref"])
            current_ref = _ref_target(repo, task_ref)
            if current_ref != candidate:
                raise ValueError(
                    "coding_task_publication_stale_or_missing: "
                    f"expected {candidate}, found {current_ref or 'missing'}"
                )

            validation = task.get("validation")
            if (
                not isinstance(validation, dict)
                or validation.get("validated") is not True
                or validation.get("candidate_commit") != candidate
            ):
                raise ValueError("coding_task_published_validation_missing")

            committed_paths = _commit_changed_paths(
                repo,
                str(task["base_commit"]),
                candidate,
            )
            undeclared = sorted(
                set(committed_paths) - set(task["declared_paths"])
            )
            if undeclared:
                raise ValueError(
                    "coding_task_published_candidate_contains_undeclared_paths: "
                    + json.dumps(undeclared, ensure_ascii=False)
                )

            if worktrees._status_porcelain(repo):
                raise ValueError(
                    "coding_task_source_checkout_dirty: commit or discard source "
                    "changes before integration"
                )

            current_head = _git(repo, "rev-parse", "HEAD").lower()
            previous_integrated = task.get("integrated_source_commit")
            if current_head == candidate:
                task.update(
                    {
                        "state": "integrated",
                        "integrated_source_commit": candidate,
                        "integrated_at": task.get("integrated_at") or _iso_now(),
                        "updated_at": _iso_now(),
                    }
                )
                store.write_task(task_key, task)
                return {
                    **_observe_task(
                        config,
                        task,
                        principal_hash=principal_hash,
                        workspace_id=workspace_id,
                    ),
                    "integrated": True,
                    "replayed": bool(previous_integrated == candidate),
                    "reconciled": bool(previous_integrated != candidate),
                }

            if current_head != expected_source_commit:
                raise ValueError(
                    "coding_task_integration_conflict: "
                    f"expected source {expected_source_commit}, found {current_head}"
                )

            ancestor = _git_result(
                repo,
                "merge-base",
                "--is-ancestor",
                current_head,
                candidate,
            )
            if ancestor.returncode != 0:
                raise ValueError(
                    "coding_task_integration_conflict: source HEAD is not an "
                    "ancestor of the published candidate"
                )

            result = _git_result(
                repo,
                "-c",
                "core.hooksPath=/dev/null",
                "merge",
                "--ff-only",
                "--no-overwrite-ignore",
                "--no-edit",
                candidate,
                timeout=60,
            )
            if result.returncode != 0:
                message = result.stderr.decode("utf-8", errors="replace").strip()
                raise ValueError(
                    f"coding_task_integration_conflict: {redact(message)}"
                )

            final_head = _git(repo, "rev-parse", "HEAD").lower()
            if final_head != candidate:
                raise RuntimeError("coding_task_integration_verification_failed")
            if worktrees._status_porcelain(repo):
                raise RuntimeError(
                    "coding_task_integration_left_source_checkout_dirty"
                )

            task.update(
                {
                    "state": "integrated",
                    "integrated_source_commit": candidate,
                    "integrated_at": _iso_now(),
                    "updated_at": _iso_now(),
                }
            )
            store.write_task(task_key, task)
            return {
                **_observe_task(
                    config,
                    task,
                    principal_hash=principal_hash,
                    workspace_id=workspace_id,
                ),
                "integrated": True,
                "replayed": False,
                "reconciled": False,
            }
