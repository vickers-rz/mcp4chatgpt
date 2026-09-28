"""Atomic, compare-and-swap workspace file transactions."""

from __future__ import annotations

import difflib
import fcntl
import hashlib
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import recovery, validators, versioning


class FileChangedSinceRead(ValueError):
    """Raised when a caller attempts a stale compare-and-swap mutation."""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lock_dir(state_dir: Path) -> Path:
    return state_dir / "file-locks"


@contextmanager
def file_transaction_lock(
    state_dir: Path,
    target: Path,
    *,
    blocking: bool = True,
) -> Iterator[None]:
    lock_dir = _lock_dir(state_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_name = hashlib.sha256(str(target).encode("utf-8")).hexdigest() + ".lock"
    fd = os.open(lock_dir / lock_name, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def assert_expected_sha(
    *,
    current_sha256: str | None,
    expected_sha256: str | None,
) -> None:
    if expected_sha256 is None:
        return
    if current_sha256 != expected_sha256:
        raise FileChangedSinceRead(
            "file_changed_since_read: "
            f"expected_sha256={expected_sha256}, current_sha256={current_sha256}"
        )


def _commit_text_transaction(
    state_dir: Path,
    *,
    target: Path,
    before: bytes | None,
    after_text: str,
    operation: str,
    validation: dict[str, object] | None = None,
) -> dict[str, Any]:
    after = after_text.encode("utf-8")
    before_sha = recovery.sha256_bytes(before) if before is not None else None
    after_sha = recovery.sha256_bytes(after)
    tx_id = "filetx-" + uuid.uuid4().hex

    recovery_blob = None
    git_info: dict[str, str] | None = None
    if before is not None:
        _, recovery_blob = recovery.persist_recovery_blob(state_dir, before)
        git_info = recovery.pin_git_before_image(
            target=target,
            data=before,
            transaction_id=tx_id,
        )

    base_record: dict[str, Any] = {
        "ts": _iso_now(),
        "transaction_id": tx_id,
        "operation": operation,
        "path": str(target),
        "before_sha256": before_sha,
        "after_sha256": after_sha,
        "recovery_blob": recovery_blob,
        "validation": validation,
        **(git_info or {}),
    }
    recovery.append_transaction_record(
        state_dir,
        {**base_record, "state": "prepared"},
    )

    mode = target.stat().st_mode if target.exists() else None
    replaced = False
    try:
        recovery.atomic_replace_bytes(target, after, mode)
        replaced = True
        if git_info is not None:
            git_info = recovery.finalize_git_recovery_ref(
                git_info=git_info,
                after=after,
                transaction_id=tx_id,
            )
    except Exception as exc:
        replaced = replaced or bool(getattr(exc, "replaced", False))
        rollback_error = None
        if replaced:
            try:
                if before is None:
                    target.unlink(missing_ok=True)
                    recovery.fsync_directory(target.parent)
                else:
                    recovery.atomic_replace_bytes(target, before, mode)
            except Exception as rollback_exc:  # pragma: no cover
                rollback_error = recovery.redacted_error(rollback_exc)
        journal_error = None
        try:
            recovery.append_transaction_record(
                state_dir,
                {
                    **base_record,
                    "ts": _iso_now(),
                    "state": "aborted",
                    "rolled_back": replaced and rollback_error is None,
                    "rollback_error": rollback_error,
                    "error": recovery.redacted_error(exc),
                },
            )
        except Exception as journal_exc:
            journal_error = recovery.redacted_error(journal_exc)
            # Preserve the actual mutation/finalize failure. Losing the ABORTED
            # audit record is important diagnostics, but must not mask the
            # original exception that determined transaction outcome.
            try:
                exc.add_note(
                    "file transaction abort journal failed: "
                    + recovery.redacted_error(journal_exc)
                )
            except AttributeError:  # pragma: no cover - Python < 3.11
                pass
        if replaced and rollback_error:
            raise RuntimeError(
                f"file transaction {tx_id} outcome_unknown; rollback failed: {rollback_error}"
            ) from exc
        if journal_error:
            raise RuntimeError(
                f"file transaction {tx_id} outcome_unknown; abort journal failed: {journal_error}"
            ) from exc
        raise

    committed_record = {
        **base_record,
        **(git_info or {}),
        "ts": _iso_now(),
        "state": "committed",
    }
    journal_warning = None
    try:
        recovery.append_transaction_record(state_dir, committed_record)
    except Exception as journal_exc:
        # os.replace + directory fsync (and Git finalize when applicable) is
        # the commit point. A later audit-log failure must not make callers
        # believe the file mutation failed and tempt them to replay it.
        journal_warning = recovery.redacted_error(journal_exc)

    result = {
        "transaction_id": tx_id,
        "before_sha256": before_sha,
        "sha256": after_sha,
        "recovery_blob": recovery_blob,
        "validation": validation,
        "journal_state": (
            "committed" if journal_warning is None else "commit_record_failed"
        ),
        **(git_info or {}),
    }
    if journal_warning is not None:
        result["journal_warning"] = journal_warning
    return result


def write_text(
    state_dir: Path,
    target: Path,
    content: str,
    *,
    overwrite: bool = False,
    expected_sha256: str | None = None,
    full_replace_token: str | None = None,
    allow_large_reduction: bool = False,
) -> dict[str, Any]:
    """Transactionally create or replace a UTF-8 text file."""

    with file_transaction_lock(state_dir, target):
        exists = target.exists()
        if exists and not target.is_file():
            raise ValueError(f"Not a file: {target}")
        if exists and not overwrite:
            raise ValueError(
                f"Refusing to overwrite existing file without overwrite=true: {target}"
            )

        before = target.read_bytes() if exists else None
        current_sha = (
            recovery.sha256_bytes(before)
            if before is not None
            else None
        )
        if exists and expected_sha256 is None:
            raise ValueError(
                "overwrite_requires_expected_sha256: read the current file first"
            )
        assert_expected_sha(
            current_sha256=current_sha,
            expected_sha256=expected_sha256,
        )
        validation = validators.validate_candidate(
            target,
            before=before,
            after=content.encode("utf-8"),
            allow_large_reduction=allow_large_reduction,
        )
        if exists:
            key = versioning.load_or_create_key(state_dir)
            if not versioning.verify_full_replace_token(
                key,
                full_replace_token,
                target=target,
                sha256=current_sha or "",
                size=len(before or b""),
            ):
                raise ValueError(
                    "full_replace_requires_complete_read: obtain a non-truncated "
                    "full_replace_token from local_read_text, or use local_apply_patch"
                )
        tx = _commit_text_transaction(
            state_dir,
            target=target,
            before=before,
            after_text=content,
            operation="write_file",
            validation=validation,
        )

    return {
        "path": str(target),
        "bytes": len(content.encode("utf-8")),
        **tx,
    }


def apply_exact_patch(
    state_dir: Path,
    target: Path,
    old: str,
    new: str,
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Transactionally replace exactly the first matching text block."""

    if not target.is_file():
        raise ValueError(f"Not a file: {target}")

    with file_transaction_lock(state_dir, target):
        before = target.read_bytes()
        current_sha = recovery.sha256_bytes(before)
        assert_expected_sha(
            current_sha256=current_sha,
            expected_sha256=expected_sha256,
        )

        original = before.decode("utf-8")
        if old not in original:
            raise ValueError("Patch anchor text was not found.")
        updated = original.replace(old, new, 1)
        validation = validators.validate_candidate(
            target,
            before=before,
            after=updated.encode("utf-8"),
            # Exact-patch mutations already identify the intentional removed
            # region, so only structural/encoding validation applies here.
            allow_large_reduction=True,
        )

        tx = _commit_text_transaction(
            state_dir,
            target=target,
            before=before,
            after_text=updated,
            operation="apply_patch",
            validation=validation,
        )

    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=str(target),
            tofile=str(target),
        )
    )
    return {
        "path": str(target),
        "diff": diff,
        **tx,
    }
