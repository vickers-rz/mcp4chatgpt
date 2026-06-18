from __future__ import annotations

import difflib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .safety import redact, resolve_allowed_path, truncate_text, validate_command


def _command_log_path(config: Config) -> Path:
    return config.audit_log.parent / "commands.jsonl"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text_or_empty(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _write_command_log(config: Config, record: dict[str, Any]) -> None:
    path = _command_log_path(config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        return


def _command_log_record(
    config: Config,
    *,
    cwd: str,
    command: str,
    timeout_sec: int,
    exit_code: int | None,
    duration_ms: int,
    stdout: Any = "",
    stderr: Any = "",
    ok: bool,
) -> dict[str, Any]:
    stdout_text, stdout_truncated = truncate_text(redact(_text_or_empty(stdout)), config.max_output_chars)
    stderr_text, stderr_truncated = truncate_text(redact(_text_or_empty(stderr)), config.max_output_chars)
    return {
        "ts": _iso_now(),
        "tool": "local_run_command",
        "cwd": cwd,
        "command": redact(command),
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "timeout_sec": timeout_sec,
        "ok": ok,
        "truncated": stdout_truncated or stderr_truncated,
    }


def tail_command_log(config: Config, limit: int = 20) -> dict[str, Any]:
    path = _command_log_path(config)
    limit = max(1, min(limit, 200))
    if not path.exists():
        return {"path": str(path), "order": "oldest_to_newest", "entries": []}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    entries = []
    for line in lines[-limit:]:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        entries.append({key: redact(value) if isinstance(value, str) else value for key, value in entry.items()})
    return {"path": str(path), "order": "oldest_to_newest", "entries": entries}


def list_files(config: Config, path: str = ".", max_entries: int = 200) -> dict[str, Any]:
    root = resolve_allowed_path(path, config.allowed_roots, must_exist=True)
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")
    entries = []
    for item in sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:max_entries]:
        stat = item.stat()
        entries.append(
            {
                "name": item.name,
                "path": str(item),
                "type": "directory" if item.is_dir() else "file",
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            }
        )
    return {"path": str(root), "entries": entries}


def read_text(config: Config, path: str, max_chars: int | None = None) -> dict[str, Any]:
    target = resolve_allowed_path(path, config.allowed_roots, must_exist=True)
    if not target.is_file():
        raise ValueError(f"Not a file: {target}")
    text = target.read_text(encoding="utf-8", errors="replace")
    text, truncated = truncate_text(redact(text), max_chars or config.max_output_chars)
    return {"path": str(target), "text": text, "truncated": truncated}


def write_file(config: Config, path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    target = resolve_allowed_path(path, config.allowed_roots, must_exist=False)
    if target.exists() and not overwrite:
        raise ValueError(f"Refusing to overwrite existing file without overwrite=true: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": str(target), "bytes": len(content.encode("utf-8"))}


def apply_patch(config: Config, path: str, old: str, new: str) -> dict[str, Any]:
    target = resolve_allowed_path(path, config.allowed_roots, must_exist=True)
    if not target.is_file():
        raise ValueError(f"Not a file: {target}")
    original = target.read_text(encoding="utf-8")
    if old not in original:
        raise ValueError("Patch anchor text was not found.")
    updated = original.replace(old, new, 1)
    target.write_text(updated, encoding="utf-8")
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=str(target),
            tofile=str(target),
        )
    )
    diff, truncated = truncate_text(diff, config.max_output_chars)
    return {"path": str(target), "diff": diff, "truncated": truncated}


def _run(config: Config, args: list[str], cwd: str | None, timeout_sec: int = 30) -> dict[str, Any]:
    resolved_cwd = resolve_allowed_path(cwd or ".", config.allowed_roots, must_exist=True)
    start = time.time()
    proc = subprocess.run(
        args,
        cwd=resolved_cwd,
        text=True,
        capture_output=True,
        timeout=max(1, min(timeout_sec, 300)),
        check=False,
    )
    duration_ms = int((time.time() - start) * 1000)
    stdout, stdout_truncated = truncate_text(redact(proc.stdout), config.max_output_chars)
    stderr, stderr_truncated = truncate_text(redact(proc.stderr), config.max_output_chars)
    return {
        "cwd": str(resolved_cwd),
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": duration_ms,
        "truncated": stdout_truncated or stderr_truncated,
    }


def run_command(config: Config, command: str, cwd: str | None = None, timeout_sec: int = 30) -> dict[str, Any]:
    raw_command = command
    start = time.time()
    timeout_sec = max(1, min(timeout_sec, 300))
    try:
        command = validate_command(command)
        resolved_cwd = resolve_allowed_path(cwd or ".", config.allowed_roots, must_exist=True)
    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        cwd_for_log = str(Path(cwd or ".").expanduser()) if cwd else "."
        _write_command_log(
            config,
            _command_log_record(
                config,
                cwd=cwd_for_log,
                command=raw_command,
                timeout_sec=timeout_sec,
                exit_code=None,
                duration_ms=duration_ms,
                stderr=str(exc),
                ok=False,
            ),
        )
        raise
    # Commands intentionally run inside a resolved allowed cwd. The command
    # string remains shell-based for practical ChatGPT usage, so safety checks
    # happen before execution and outputs are redacted after execution.
    try:
        proc = subprocess.run(
            command,
            cwd=resolved_cwd,
            shell=True,
            executable=os.environ.get("SHELL", "/bin/zsh"),
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.time() - start) * 1000)
        _write_command_log(
            config,
            _command_log_record(
                config,
                cwd=str(resolved_cwd),
                command=command,
                timeout_sec=timeout_sec,
                exit_code=None,
                duration_ms=duration_ms,
                stdout=exc.stdout,
                stderr=exc.stderr or f"Command timed out after {timeout_sec} seconds.",
                ok=False,
            ),
        )
        raise
    duration_ms = int((time.time() - start) * 1000)
    stdout, stdout_truncated = truncate_text(redact(proc.stdout), config.max_output_chars)
    stderr, stderr_truncated = truncate_text(redact(proc.stderr), config.max_output_chars)
    _write_command_log(
        config,
        _command_log_record(
            config,
            cwd=str(resolved_cwd),
            command=command,
            timeout_sec=timeout_sec,
            exit_code=proc.returncode,
            duration_ms=duration_ms,
            stdout=proc.stdout,
            stderr=proc.stderr,
            ok=proc.returncode == 0,
        ),
    )
    return {
        "cwd": str(resolved_cwd),
        "command": command,
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": duration_ms,
        "truncated": stdout_truncated or stderr_truncated,
    }


def git_status(config: Config, cwd: str) -> dict[str, Any]:
    return _run(config, ["git", "status", "--short", "--branch"], cwd)


def git_diff(config: Config, cwd: str, staged: bool = False, max_chars: int | None = None) -> dict[str, Any]:
    args = ["git", "diff", "--no-ext-diff"]
    if staged:
        args.append("--staged")
    result = _run(config, args, cwd)
    if max_chars:
        result["stdout"], result["truncated"] = truncate_text(result["stdout"], max_chars)
    return result


def git_log(config: Config, cwd: str, limit: int = 20) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    return _run(config, ["git", "log", f"-{limit}", "--oneline", "--decorate"], cwd)


def git_show(config: Config, cwd: str, rev: str = "HEAD", max_chars: int | None = None) -> dict[str, Any]:
    result = _run(config, ["git", "show", "--no-ext-diff", "--stat", "--patch", rev], cwd)
    if max_chars:
        result["stdout"], result["truncated"] = truncate_text(result["stdout"], max_chars)
    return result
