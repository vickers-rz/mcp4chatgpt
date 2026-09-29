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
    repo = worktrees._source_repo(config, cwd or ".")
    workspace_id = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()
    return repo, workspace_id


def _scope(
    config: Config,
    *,
    client_id: str | None,
    operation_id: str,
    cwd: str | None,
) -> tuple[str, dict[str, Any], Path]:
    public_operation_id = worktrees._validate_operation_id(operation_id)
    principal = _principal(config, client_id)
    repo, workspace_id = _workspace(config, cwd)
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
    return reservation_id, scope_metadata, repo


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
    expected_workspace_id = None
    if args.get("cwd"):
        _, expected_workspace_id = _workspace(config, args["cwd"])
    return worktrees.worktree_status(
        config,
        args["worktree_id"],
        expected_principal_hash=_principal_hash(config, client_id),
        expected_workspace_id=expected_workspace_id,
        strict_scope=True,
    )


def list_worktrees(config: Config, args: dict[str, Any], client_id: str | None = None) -> dict[str, Any]:
    expected_workspace_id = None
    if args.get("cwd"):
        _, expected_workspace_id = _workspace(config, args["cwd"])
    return worktrees.list_worktrees(
        config,
        limit=int(args.get("limit", 50)),
        cursor=args.get("cursor"),
        expected_principal_hash=_principal_hash(config, client_id),
        expected_workspace_id=expected_workspace_id,
        strict_scope=True,
    )


def remove(config: Config, args: dict[str, Any], client_id: str | None = None) -> dict[str, Any]:
    expected_workspace_id = None
    if args.get("cwd"):
        _, expected_workspace_id = _workspace(config, args["cwd"])
    return worktrees.remove_worktree(
        config,
        args["worktree_id"],
        expected_principal_hash=_principal_hash(config, client_id),
        expected_workspace_id=expected_workspace_id,
        strict_scope=True,
    )
