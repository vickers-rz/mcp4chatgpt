"""受控的本地文件、Shell 与 Git 操作实现。

这一层位于 MCP 工具注册表之后、操作系统之前。它把模型传来的字符串参数转换为
确定性的本机动作，并统一处理允许目录、命令白名单、超时、进程组终止、输出截断、
脱敏和审计日志。``ToolRegistry`` 只负责分派；真正的安全约束必须在这里执行。

命令执行采用独立进程组，超时后先尝试温和终止再强制杀死整个进程组，避免子进程
残留。Git 辅助函数刻意使用参数数组而非拼接 Shell 字符串，以缩小命令注入面。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .jobs import manager as job_manager
from .safety import redact, resolve_allowed_path, truncate_text, validate_command
from .workspace import recovery, transactions, versioning


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


def _clamp_timeout(timeout_sec: int) -> int:
    timeout_sec = int(timeout_sec)
    if timeout_sec > 30:
        raise ValueError(
            "long_command_requires_job: synchronous local_run_command is limited "
            "to 30 seconds; use local_start_job for longer work"
        )
    return max(1, timeout_sec)


def _kill_process_group(proc: subprocess.Popen[str], grace_sec: float = 2.0) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + grace_sec
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.05)

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _run_process(
    args: str | list[str],
    *,
    cwd: Path,
    timeout_sec: int,
    shell: bool = False,
    executable: str | None = None,
) -> subprocess.CompletedProcess[str]:
    timeout_sec = _clamp_timeout(timeout_sec)
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        shell=shell,
        executable=executable,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(proc)
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(
            args,
            timeout_sec,
            output=stdout if stdout is not None else exc.output,
            stderr=stderr if stderr is not None else exc.stderr,
        ) from None
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def tail_command_log(config: Config, limit: int = 20) -> dict[str, Any]:
    path = _command_log_path(config)
    limit = max(1, min(limit, 200))
    if not path.exists():
        return {"path": str(path), "order": "oldest_to_newest", "entries": []}
    lines: deque[str] = deque(maxlen=limit)
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            lines.append(line.rstrip("\n"))
    entries = []
    for line in lines:
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

    raw = target.read_bytes()
    sha256 = recovery.sha256_bytes(raw)
    try:
        decoded = raw.decode("utf-8")
        utf8_lossless = True
    except UnicodeDecodeError:
        decoded = raw.decode("utf-8", errors="replace")
        utf8_lossless = False

    redacted_text = redact(decoded)
    visible_text, truncated = truncate_text(
        redacted_text,
        max_chars if max_chars is not None else config.max_output_chars,
    )

    blocked_reason = None
    if truncated:
        blocked_reason = "truncated"
    elif not utf8_lossless:
        blocked_reason = "invalid_utf8"
    elif redacted_text != decoded:
        blocked_reason = "redacted"

    replace_proof = None
    if blocked_reason is None:
        key = versioning.load_or_create_key(config.audit_log.parent)
        replace_proof = versioning.full_replace_token(
            key,
            target=target,
            sha256=sha256,
            size=len(raw),
        )

    return {
        "path": str(target),
        "text": visible_text,
        "truncated": truncated,
        "sha256": sha256,
        "bytes": len(raw),
        "full_replace_eligible": replace_proof is not None,
        "full_replace_token": replace_proof,
        "full_replace_blocked_reason": blocked_reason,
    }



def write_file(
    config: Config,
    path: str,
    content: str,
    overwrite: bool = False,
    expected_sha256: str | None = None,
    full_replace_token: str | None = None,
    allow_large_reduction: bool = False,
) -> dict[str, Any]:
    target = resolve_allowed_path(path, config.allowed_roots, must_exist=False)
    return transactions.write_text(
        config.audit_log.parent,
        target,
        content,
        overwrite=overwrite,
        expected_sha256=expected_sha256,
        full_replace_token=full_replace_token,
        allow_large_reduction=allow_large_reduction,
    )


def apply_patch(
    config: Config,
    path: str,
    old: str,
    new: str,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    target = resolve_allowed_path(path, config.allowed_roots, must_exist=True)
    result = transactions.apply_exact_patch(
        config.audit_log.parent,
        target,
        old,
        new,
        expected_sha256=expected_sha256,
    )
    result["diff"], result["truncated"] = truncate_text(
        result["diff"],
        config.max_output_chars,
    )
    return result


def start_job(
    config: Config,
    operation_id: str,
    command: str,
    cwd: str | None = None,
    timeout_sec: int = 900,
) -> dict[str, Any]:
    return job_manager.start_job(
        config,
        operation_id=operation_id,
        command=command,
        cwd=cwd,
        timeout_sec=timeout_sec,
    )


def job_status(config: Config, job_id: str) -> dict[str, Any]:
    return job_manager.job_status(config, job_id)


def job_logs(
    config: Config,
    job_id: str,
    stdout_offset: int = 0,
    stderr_offset: int = 0,
    max_bytes: int = 16_384,
) -> dict[str, Any]:
    return job_manager.job_logs(
        config,
        job_id,
        stdout_offset=stdout_offset,
        stderr_offset=stderr_offset,
        max_bytes=max_bytes,
    )


def list_jobs(config: Config, limit: int = 50) -> dict[str, Any]:
    return job_manager.list_jobs(config, limit)


def cancel_job(
    config: Config,
    job_id: str,
    grace_sec: float = 2.0,
) -> dict[str, Any]:
    return job_manager.cancel_job(
        config,
        job_id,
        grace_sec=grace_sec,
    )


def _run(config: Config, args: list[str], cwd: str | None, timeout_sec: int = 30) -> dict[str, Any]:
    resolved_cwd = resolve_allowed_path(cwd or ".", config.allowed_roots, must_exist=True)
    start = time.time()
    proc = _run_process(
        args,
        cwd=resolved_cwd,
        timeout_sec=timeout_sec,
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
    timeout_sec = _clamp_timeout(timeout_sec)
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
        proc = _run_process(
            command,
            cwd=resolved_cwd,
            shell=True,
            executable=os.environ.get("SHELL", "/bin/zsh"),
            timeout_sec=timeout_sec,
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
