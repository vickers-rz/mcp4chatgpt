"""Durable recovery primitives for workspace mutations.

Recovery data is deliberately independent from the Git index and active branch.
For files inside a Git repository, before-images are written to Git's object
store and pinned by a dedicated recovery ref so they survive garbage collection.
A local content-addressed recovery blob is kept as a non-Git fallback.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..safety import redact


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def transaction_log_path(state_dir: Path) -> Path:
    return state_dir / "file_transactions.jsonl"


def recovery_blob_dir(state_dir: Path) -> Path:
    return state_dir / "file-recovery"


def append_transaction_record(state_dir: Path, record: dict[str, Any]) -> None:
    path = transaction_log_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            record,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_replace_bytes(
    target: Path,
    data: bytes,
    mode: int | None = None,
) -> None:
    """Replace target atomically using a same-directory temporary file."""

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.mcp4-",
        dir=target.parent,
    )
    temp = Path(temp_name)
    try:
        os.fchmod(fd, stat.S_IMODE(mode) if mode is not None else 0o644)
        with os.fdopen(fd, "wb", closefd=True) as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, target)
        fsync_directory(target.parent)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def persist_recovery_blob(
    state_dir: Path,
    data: bytes,
) -> tuple[str, str]:
    digest = sha256_bytes(data)
    blob_dir = recovery_blob_dir(state_dir)
    blob_dir.mkdir(parents=True, exist_ok=True)
    blob = blob_dir / digest
    if not blob.exists():
        atomic_replace_bytes(blob, data, 0o600)
    return digest, str(blob)


def _git_commit_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "MCP4ChatGPT Recovery",
            "GIT_AUTHOR_EMAIL": "recovery@mcp4chatgpt.local",
            "GIT_COMMITTER_NAME": "MCP4ChatGPT Recovery",
            "GIT_COMMITTER_EMAIL": "recovery@mcp4chatgpt.local",
        }
    )
    return env


def git_repo_root(target: Path) -> Path | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(target.parent), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    root = completed.stdout.strip()
    return Path(root).resolve() if root else None


def pin_git_before_image(
    *,
    target: Path,
    data: bytes,
    transaction_id: str,
) -> dict[str, str] | None:
    """Pin a before-image to a durable private Git ref without touching index."""

    repo = git_repo_root(target)
    if repo is None:
        return None

    blob_proc = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=data,
        check=True,
        capture_output=True,
        timeout=10,
    )
    before_blob = blob_proc.stdout.decode("ascii").strip()

    tree_line = f"100644 blob {before_blob}\tpayload\n".encode("ascii")
    tree_proc = subprocess.run(
        ["git", "-C", str(repo), "mktree"],
        input=tree_line,
        check=True,
        capture_output=True,
        timeout=10,
    )
    tree = tree_proc.stdout.decode("ascii").strip()

    try:
        relative = str(target.resolve().relative_to(repo))
    except ValueError:
        relative = str(target.resolve())
    message = (
        f"MCP4ChatGPT recovery {transaction_id}\n\n"
        f"path: {relative}\n"
        f"sha256: {sha256_bytes(data)}\n"
    )
    commit_proc = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", tree],
        input=message.encode("utf-8"),
        check=True,
        capture_output=True,
        timeout=10,
        env=_git_commit_env(),
    )
    prepared_commit = commit_proc.stdout.decode("ascii").strip()
    recovery_ref = f"refs/mcp4chatgpt/file-transactions/{transaction_id}"
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", recovery_ref, prepared_commit],
        check=True,
        capture_output=True,
        timeout=10,
    )
    return {
        "git_repo_root": str(repo),
        "git_blob": before_blob,
        "git_recovery_commit": prepared_commit,
        "git_recovery_ref": recovery_ref,
    }


def finalize_git_recovery_ref(
    *,
    git_info: dict[str, str],
    after: bytes,
    transaction_id: str,
) -> dict[str, str]:
    """Advance a prepared recovery ref to a commit containing before + after."""

    repo = Path(git_info["git_repo_root"])
    before_blob = git_info["git_blob"]
    prepared_commit = git_info["git_recovery_commit"]
    recovery_ref = git_info["git_recovery_ref"]

    after_proc = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=after,
        check=True,
        capture_output=True,
        timeout=10,
    )
    after_blob = after_proc.stdout.decode("ascii").strip()

    # git mktree requires entries sorted by name.
    tree_payload = (
        f"100644 blob {after_blob}\tafter\n"
        f"100644 blob {before_blob}\tbefore\n"
    ).encode("ascii")
    tree_proc = subprocess.run(
        ["git", "-C", str(repo), "mktree"],
        input=tree_payload,
        check=True,
        capture_output=True,
        timeout=10,
    )
    tree = tree_proc.stdout.decode("ascii").strip()

    message = (
        f"MCP4ChatGPT committed file transaction {transaction_id}\n\n"
        f"before: {before_blob}\n"
        f"after: {after_blob}\n"
    )
    commit_proc = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", tree, "-p", prepared_commit],
        input=message.encode("utf-8"),
        check=True,
        capture_output=True,
        timeout=10,
        env=_git_commit_env(),
    )
    committed = commit_proc.stdout.decode("ascii").strip()

    # Compare-and-swap the ref so a concurrent writer cannot silently move it.
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "update-ref",
            recovery_ref,
            committed,
            prepared_commit,
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    return {
        **git_info,
        "git_after_blob": after_blob,
        "git_recovery_commit": committed,
    }


def redacted_error(exc: BaseException) -> str:
    return redact(str(exc))
