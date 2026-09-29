"""Operator-driven retention for terminal strict file image blobs."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

from .store import OperationKey, OperationStore, canonical_json
from ..workspace import recovery


def _artifact_dir(state_dir: Path, key: OperationKey) -> Path:
    return state_dir / "operation-files" / hashlib.sha256(key.encoded().encode()).hexdigest()


def prune_images(store: OperationStore, state_dir: Path, key: OperationKey, *,
                 older_than_days: int = 90, apply: bool = False,
                 now: float | None = None) -> dict:
    """Keep ledger/receipts but prune old before/after images of terminal ops.

    A marker is durable before either image is removed, so interruption can be
    retried. Uncertain or pre-existing records without a terminal timestamp are
    never pruned. Dry-run is the default.
    """
    if older_than_days < 1:
        raise ValueError("retention_days_must_be_positive")
    state_dir = Path(state_dir).resolve()
    artifact = _artifact_dir(state_dir, key)
    if not artifact.is_dir():
        return {"operation_id": key.operation_id, "eligible": False, "reason": "no_artifact"}
    lock_path = state_dir / "operation-locks" / hashlib.sha256(key.encoded().encode()).hexdigest()
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        row = store.get(key)
        if row is None or row["state"] not in {"succeeded", "failed"}:
            return {"operation_id": key.operation_id, "eligible": False, "reason": "not_terminal"}
        updated = row.get("updated_at")
        if type(updated) not in {float, int} or updated > (now or time.time()) - older_than_days * 86400:
            return {"operation_id": key.operation_id, "eligible": False, "reason": "retention_active"}
        marker = artifact / "images-pruned.json"
        images = [artifact / name for name in ("before", "after") if (artifact / name).is_file()]
        bytes_to_prune = sum(path.stat().st_size for path in images)
        result = {"operation_id": key.operation_id, "eligible": True,
                  "bytes": bytes_to_prune, "pruned": bool(marker.exists())}
        if not apply:
            return result
        manifest_path = artifact / "prepared.json"
        if not manifest_path.is_file():
            return {"operation_id": key.operation_id, "eligible": False,
                    "reason": "missing_manifest"}
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("key") != key.encoded():
            raise ValueError("artifact_identity_mismatch")
        if marker.exists():
            summary = json.loads(marker.read_text(encoding="utf-8"))
            if (summary.get("key") != key.encoded() or
                    summary.get("manifest_sha256") != recovery.sha256_bytes(manifest_path.read_bytes())):
                raise ValueError("pruning_marker_mismatch")
        for name in ("before", "after"):
            image = artifact / name
            if image.is_file() and recovery.sha256_bytes(image.read_bytes()) != manifest[name]["sha256"]:
                raise ValueError("artifact_image_corrupt")
        if not marker.exists():
            summary = {"key": key.encoded(), "manifest_sha256": recovery.sha256_bytes(
                manifest_path.read_bytes()), "before_sha256": manifest["before"]["sha256"],
                       "after_sha256": manifest["after"]["sha256"], "pruned_at": now or time.time()}
            recovery.atomic_replace_bytes(marker, canonical_json(summary).encode(), 0o600)
        for image in images:
            image.unlink()
        recovery.fsync_directory(artifact)
        return {**result, "pruned": True}
    finally:
        os.close(fd)
