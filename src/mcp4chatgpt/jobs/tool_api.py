"""Authenticated MCP entry points for durable local jobs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..config import Config
from ..safety import resolve_allowed_path
from . import manager


_SCOPE_VERSION = 1


def _principal(config: Config, client_id: str) -> str:
    value = str(client_id or "").strip()
    if value:
        return value
    if getattr(config, "local_auth_disabled", False):
        return "local"
    raise ValueError("authenticated_principal_required")


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
    client_id: str,
    operation_id: str,
    cwd: str | None,
) -> tuple[str, dict[str, Any], Path]:
    public_operation_id = manager.validate_operation_id(operation_id)
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


def _principal_hash(config: Config, client_id: str) -> str:
    principal = _principal(config, client_id)
    return hashlib.sha256(principal.encode("utf-8")).hexdigest()


def start(config: Config, args: dict, client_id: str) -> dict:
    reservation_id, scope_metadata, resolved_cwd = _scope(
        config,
        client_id=client_id,
        operation_id=args["operation_id"],
        cwd=args.get("cwd"),
    )
    return manager.start_job(
        config,
        operation_id=args["operation_id"],
        reservation_id=reservation_id,
        scope_metadata=scope_metadata,
        command=args["command"],
        cwd=str(resolved_cwd),
        timeout_sec=int(args.get("timeout_sec", 900)),
    )


def operation_status(config: Config, args: dict, client_id: str) -> dict:
    reservation_id, _scope_metadata, _resolved_cwd = _scope(
        config,
        client_id=client_id,
        operation_id=args["operation_id"],
        cwd=args.get("cwd"),
    )
    return manager.job_operation_status(
        config,
        operation_id=args["operation_id"],
        reservation_id=reservation_id,
    )


def status(config: Config, args: dict, client_id: str) -> dict:
    return manager.job_status(
        config,
        args["job_id"],
        expected_principal_hash=_principal_hash(config, client_id),
    )


def logs(config: Config, args: dict, client_id: str) -> dict:
    return manager.job_logs(
        config,
        args["job_id"],
        stdout_offset=int(args.get("stdout_offset", 0)),
        stderr_offset=int(args.get("stderr_offset", 0)),
        max_bytes=int(args.get("max_bytes", 16_384)),
        expected_principal_hash=_principal_hash(config, client_id),
    )


def list_jobs(config: Config, args: dict, client_id: str) -> dict:
    return manager.list_jobs(
        config,
        int(args.get("limit", 50)),
        expected_principal_hash=_principal_hash(config, client_id),
    )


def cancel(config: Config, args: dict, client_id: str) -> dict:
    return manager.cancel_job(
        config,
        args["job_id"],
        grace_sec=float(args.get("grace_sec", 2)),
        expected_principal_hash=_principal_hash(config, client_id),
    )
