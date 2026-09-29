"""Authenticated MCP entry points for the strict file operation protocol."""
from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path

from .files import FileExecutor
from .store import OperationKey, OperationStore
from ..config import Config
from ..safety import resolve_allowed_path
from ..workspace import versioning


def _open(config: Config, path: str, client_id: str, *, initialize: bool
          ) -> tuple[FileExecutor, str, str, Path]:
    if not client_id and not config.local_auth_disabled:
        raise ValueError("authenticated_principal_required")
    target = resolve_allowed_path(path, config.allowed_roots, must_exist=False)
    roots = [root.expanduser().resolve(strict=True) for root in config.allowed_roots]
    workspace = max((root for root in roots if target.is_relative_to(root)),
                    key=lambda root: len(root.parts))
    state_dir = config.data_dir / "operations"
    db_path = state_dir / "operations.db"
    if initialize:
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(state_dir / "init.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            existing = db_path.exists()
            if existing:
                installation_key = (state_dir / "mutation-token.key").read_bytes()
            else:
                installation_key = versioning.load_or_create_key(state_dir)
            if len(installation_key) < 32:
                raise ValueError("operation_identity_key_invalid")
            store = OperationStore(db_path) if existing else OperationStore.create(db_path)
            store.recover_database()
        finally:
            os.close(fd)
    else:
        if not db_path.exists():
            raise KeyError("operation_not_found")
        store = OperationStore(db_path)
        installation_key = (state_dir / "mutation-token.key").read_bytes()
        if len(installation_key) < 32:
            raise ValueError("operation_identity_key_invalid")
    installation_id = hashlib.sha256(installation_key).hexdigest()
    executor = FileExecutor(store, state_dir=state_dir, workspace=workspace,
                            token_dir=config.audit_log.parent)
    workspace_id = hashlib.sha256(str(workspace).encode()).hexdigest()
    return executor, installation_id, workspace_id, target


def _key(config: Config, args: dict, client_id: str, *, initialize: bool
         ) -> tuple[FileExecutor, OperationKey, Path]:
    executor, installation_id, workspace_id, target = _open(
        config, args["path"], client_id, initialize=initialize)
    return executor, OperationKey(installation_id, client_id or "local", workspace_id,
                                  args["operation_id"]), target


def write(config: Config, args: dict, client_id: str) -> dict:
    executor, key, target = _key(config, args, client_id, initialize=True)
    return executor.write(key, target, args["content"],
                          expected_sha256=args["expected_sha256"],
                          full_replace_token=args["full_replace_token"],
                          allow_large_reduction=bool(args.get("allow_large_reduction", False)))


def patch(config: Config, args: dict, client_id: str) -> dict:
    executor, key, target = _key(config, args, client_id, initialize=True)
    return executor.patch(key, target, args["old"], args["new"],
                          expected_sha256=args["expected_sha256"])


def status(config: Config, args: dict, client_id: str) -> dict:
    executor, key, _target = _key(config, args, client_id, initialize=False)
    row = executor.store.get(key)
    if row is None:
        raise KeyError("operation_not_found")
    return row


def reconcile(config: Config, args: dict, client_id: str) -> dict:
    executor, key, _target = _key(config, args, client_id, initialize=True)
    return executor.reconcile(key)
