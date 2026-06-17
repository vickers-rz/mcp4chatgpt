from __future__ import annotations

import re
from pathlib import Path


SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{20,})"),
    re.compile(r"(ghp_[A-Za-z0-9_]{20,})"),
    re.compile(r"(github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"((?:AKIA|ASIA)[A-Z0-9]{16})"),
    # Keep this tight so ordinary words such as "secretary" are not redacted.
    # Only redact likely configuration-style keys with separators around the
    # sensitive token name.
    re.compile(r"(?i)(?:^|[^A-Za-z0-9])([A-Za-z0-9_-]*?[_-])(password|passwd|token|api[_-]?key|secret)(?:[_-])?([A-Za-z0-9_-]*?)(\s*[:=]\s*)([^\s,;\"']+)"),
]

DANGEROUS_COMMAND_PATTERNS = [
    re.compile(r"(^|[;&|]\s*)sudo\b"),
    re.compile(r"\brm\s+.*-[^\n]*r[^\n]*f"),
    re.compile(r"\brm\s+.*-[^\n]*f[^\n]*r"),
    re.compile(r"\bdd\s+.*\bof=/dev/"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdiskutil\s+(erase|partition|apfs\s+delete)", re.IGNORECASE),
    re.compile(r"\bchmod\s+.*-R\s+777\b"),
    re.compile(r"\bchown\s+.*-R\b"),
    re.compile(r"\b(?:curl|wget)\b.*\|\s*(?:sh|bash|zsh)\b"),
    re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;?\s*:"),
]


def redact(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 5:
            redacted = pattern.sub(r"\1\2\3\4[REDACTED]", redacted)
        elif pattern.groups >= 3:
            redacted = pattern.sub(r"\1\2[REDACTED]", redacted)
        elif pattern.groups >= 2:
            redacted = pattern.sub(r"\1=[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[: max_chars // 2] + "\n...[truncated]...\n" + text[-max_chars // 2 :], True


def validate_command(command: str) -> str:
    command = command.strip()
    if not command:
        raise ValueError("Command cannot be empty.")
    if len(command) > 4000:
        raise ValueError("Command is too long.")
    for pattern in DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(command):
            raise ValueError(f"Refusing potentially dangerous command: {command}")
    return command


def resolve_allowed_path(path: str, allowed_roots: list[Path], *, must_exist: bool = False) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate)
    if must_exist:
        resolved = candidate.resolve(strict=True)
    else:
        resolved = candidate.resolve()

    for root in allowed_roots:
        root = root.expanduser().resolve()
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    roots = ", ".join(str(root) for root in allowed_roots)
    raise ValueError(f"Path is outside MCP_ALLOWED_ROOTS: {resolved} (allowed: {roots})")
