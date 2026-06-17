from __future__ import annotations

import difflib
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import Config
from .safety import redact, resolve_allowed_path, truncate_text, validate_command


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
    command = validate_command(command)
    resolved_cwd = resolve_allowed_path(cwd or ".", config.allowed_roots, must_exist=True)
    start = time.time()
    # Commands intentionally run inside a resolved allowed cwd. The command
    # string remains shell-based for practical ChatGPT usage, so safety checks
    # happen before execution and outputs are redacted after execution.
    proc = subprocess.run(
        command,
        cwd=resolved_cwd,
        shell=True,
        executable=os.environ.get("SHELL", "/bin/zsh"),
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
