"""Authenticated MCP entry points for managed Git worktrees."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..config import Config
from ..safety import resolve_allowed_path
from . import worktrees

_SCOPE_VERSION = 1


def _principal(config: Config, client_id: str | None) -> str:
    value = str(client_id or "").strip()
    if value:
        return value
    if getattr(config, "local_auth_disabled", False):
        return "local"
    raise ValueError("authenticated_principal_required")


def _principal_hash(config: Config, client_id: str | None) -> str:
    principal = _principal(config, client_id)
    return hashlib.sha256(principal.encode("utf-8")).hexdigest()


def _workspace(config: Config, cwd: str | None) -> tuple[Path, str]:
    resolved = resolve_allowed_path(cwd or ".", config.allowed_roots, must_exist=True)
    if not resolved.is_dir():
        raise ValueError(f"Not a directory: {resolved}")
    roots = [root.expanduser().resolve(strict=True) for root in config.allowed_roots]
    workspace = max(
        (root for root in roots if resolved.is_relative_to(root)),
        key=lambda root: len(root.parts),
    )
    workspace_id = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()
    return resolved, workspace_id


def _scope(
    config: Config,
    *,
    client_id: str | None,
    operation_id: str,
    cwd: str | None,
) -> tuple[str, dict[str, Any], Path]:
    public_operation_id = worktrees._validate_operation_id(operation_id)
    principal = _principal(config, client_id)
    resolved_cwd, workspace_id = _workspace(config, cwd)
    principal_hash = hashlib.sha256(principal.encode("utf-8")).hexdigest()
    payload = json.dumps(
        [principal_hash, workspace_id, public_operation_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    reservation_id = "scoped_v1_" + hashlib.sha256(payload).hexdigest()
    scope_metadata = {
        "version": _SCOPE_VERSION,
        "principal_hash": principal_hash,
        "workspace_id": workspace_id,
    }
    return reservation_id, scope_metadata, resolved_cwd


def create(config: Config, args: dict[str, Any], client_id: str | None = None) -> dict[str, Any]:
    reservation_id, scope_metadata, resolved_cwd = _scope(
        config,
        client_id=client_id,
        operation_id=args["operation_id"],
        cwd=args.get("cwd"),
    )
    return worktrees.create_worktree(
        config,
        operation_id=args["operation_id"],
        cwd=str(resolved_cwd),
        base_ref=args.get("base_ref", "HEAD"),
        reservation_id=reservation_id,
        scope_metadata=scope_metadata,
    )


def status(config: Config, args: dict[str, Any], client_id: str | None = None) -> dict[str, Any]:
    return worktrees.worktree_status(
        config,
        args["worktree_id"],
        expected_principal_hash=_principal_hash(config, client_id),
    )


def list_worktrees(config: Config, args: dict[str, Any], client_id: str | None = None) -> dict[str, Any]:
    return worktrees.list_worktrees(
        config,
        limit=int(args.get("limit", 50)),
        expected_principal_hash=_principal_hash(config, client_id),
    )


def remove(config: Config, args: dict[str, Any], client_id: str | None = None) -> dict[str, Any]:
    return worktrees.remove_worktree(
        config,
        args["worktree_id"],
        expected_principal_hash=_principal_hash(config, client_id),
    )
