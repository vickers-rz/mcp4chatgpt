"""Authenticated MCP entry points for validated coding tasks."""

from __future__ import annotations

from typing import Any

from .. import local_ops
from ..config import Config
from . import coding_tasks, worktree_tool_api


def _access_scope(
    config: Config,
    *,
    cwd: str,
    client_id: str | None,
) -> tuple[Any, str, str]:
    repo, workspace_id = worktree_tool_api._workspace(config, cwd)
    principal_hash = worktree_tool_api._principal_hash(config, client_id)
    return repo, principal_hash, workspace_id


def create(
    config: Config,
    args: dict[str, Any],
    client_id: str | None = None,
) -> dict[str, Any]:
    task_id = coding_tasks.validate_task_id(args["task_id"])
    reservation_id, scope_metadata, repo = worktree_tool_api._scope(
        config,
        client_id=client_id,
        operation_id=f"coding-task:{task_id}",
        cwd=args["cwd"],
    )
    return coding_tasks.create_task(
        config,
        task_id=task_id,
        repo=repo,
        base_ref=args.get("base_ref", "HEAD"),
        declared_paths=args["paths"],
        reservation_id=reservation_id,
        scope_metadata=scope_metadata,
    )


def inspect(
    config: Config,
    args: dict[str, Any],
    client_id: str | None = None,
) -> dict[str, Any]:
    repo, principal_hash, workspace_id = _access_scope(
        config,
        cwd=args["cwd"],
        client_id=client_id,
    )
    return coding_tasks.inspect_task(
        config,
        task_id=args["task_id"],
        repo=repo,
        principal_hash=principal_hash,
        workspace_id=workspace_id,
    )


def validate(
    config: Config,
    args: dict[str, Any],
    client_id: str | None = None,
) -> dict[str, Any]:
    repo, principal_hash, workspace_id = _access_scope(
        config,
        cwd=args["cwd"],
        client_id=client_id,
    )
    return coding_tasks.validate_task(
        config,
        task_id=args["task_id"],
        repo=repo,
        principal_hash=principal_hash,
        workspace_id=workspace_id,
        operation_id=args["operation_id"],
        command=args["command"],
        timeout_sec=int(args.get("timeout_sec", 300)),
    )


def reconcile_validation(
    config: Config,
    args: dict[str, Any],
    client_id: str | None = None,
) -> dict[str, Any]:
    repo, principal_hash, workspace_id = _access_scope(
        config,
        cwd=args["cwd"],
        client_id=client_id,
    )
    return coding_tasks.reconcile_validation_task(
        config,
        task_id=args["task_id"],
        repo=repo,
        principal_hash=principal_hash,
        workspace_id=workspace_id,
        operation_id=args["operation_id"],
        resolution=args["resolution"],
        confirm_effects_reviewed=args["confirm_effects_reviewed"],
        confirm_no_process_running=args.get("confirm_no_process_running", False),
    )


def publish(
    config: Config,
    args: dict[str, Any],
    client_id: str | None = None,
) -> dict[str, Any]:
    repo, principal_hash, workspace_id = _access_scope(
        config,
        cwd=args["cwd"],
        client_id=client_id,
    )
    return coding_tasks.publish_task(
        config,
        task_id=args["task_id"],
        repo=repo,
        principal_hash=principal_hash,
        workspace_id=workspace_id,
    )


def integrate(
    config: Config,
    args: dict[str, Any],
    client_id: str | None = None,
) -> dict[str, Any]:
    repo, principal_hash, workspace_id = _access_scope(
        config,
        cwd=args["cwd"],
        client_id=client_id,
    )
    return coding_tasks.integrate_task(
        config,
        task_id=args["task_id"],
        repo=repo,
        principal_hash=principal_hash,
        workspace_id=workspace_id,
        expected_source_commit=args["expected_source_commit"],
    )

def _file(config: Config, args: dict[str, Any], client_id: str | None, operation):
    repo, principal_hash, workspace_id = _access_scope(
        config, cwd=args["cwd"], client_id=client_id,
    )
    with coding_tasks.task_file(
        config, repo=repo, principal_hash=principal_hash, workspace_id=workspace_id,
        task_id=args["task_id"], path=args["path"], write=operation is not local_ops.read_text,
    ) as (checkout, target):
        options = {k: v for k, v in args.items() if k not in {"cwd", "task_id", "path"}}
        return operation(config, str(target), allowed_roots=[checkout], **options)


def read_file(config: Config, args: dict[str, Any], client_id: str | None = None):
    return _file(config, args, client_id, local_ops.read_text)


def write_file(config: Config, args: dict[str, Any], client_id: str | None = None):
    return _file(config, args, client_id, local_ops.write_file)


def apply_patch(config: Config, args: dict[str, Any], client_id: str | None = None):
    return _file(config, args, client_id, local_ops.apply_patch)
