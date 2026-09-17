"""本机能力的最后一道安全校验层。

MCP 的工具说明只能影响模型行为，不能构成安全控制。本模块因此在真正访问文件或
执行命令之前再次验证输入：敏感文本会被脱敏，超长输出会被截断，Shell 命令会被
拒绝高风险语法，文件路径会被解析到允许根目录中并防止 ``..``、符号链接等越界。

设计原则是“默认拒绝、显式放行”。任何新增的本机工具都应复用这里的确定性校验，
而不能仅在 prompt 或工具 description 中写一句“请勿执行危险操作”。
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlsplit


SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{20,})"),
    re.compile(r"(ghp_[A-Za-z0-9_]{20,})"),
    re.compile(r"(github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"((?:AKIA|ASIA)[A-Z0-9]{16})"),
    re.compile(r"(?i)(Authorization)(\s*:\s*(?:Bearer|Basic)\s+)([^\s,;\"\']+)"),
    # Pattern A – bare keyword form: password=, api_key=, token=
    # Requires the keyword to be at start-of-line or after a non-word char so
    # "notsecret" and "secretary" are NOT matched.
    re.compile(r"(?i)(?:(?:^|(?<=[^A-Za-z0-9_]))(password|passwd|api[_-]?key|secret|token))(\s*[:=]\s*)([^\s,;\"\']+)"),
    # Pattern B – env-var prefixed form: MCP_AUTH_SECRET=, FIRECRAWL_API_KEY=
    # The prefix guarantees we are inside a config key name, not prose.
    re.compile(r"(?i)([A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*[_-](?:password|passwd|api[_-]?key|secret|token)(?:[_-][A-Za-z0-9]+)*)(\s*[:=]\s*)([^\s,;\"\']+)"),
]

SAFE_PRIVILEGED_COMMAND_PATTERNS = [
    # Narrow allowlist for explicit macOS power actions. Keep these exact so
    # shell chaining, redirection, password piping, and arbitrary sudo usage
    # remain blocked by the general dangerous-command rules below.
    re.compile(r"^sudo\s+(?:/sbin/)?shutdown\s+-(?:h|r)\s+now$"),
    re.compile(r"^sudo\s+(?:/sbin/)?reboot$"),
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
        if pattern.groups == 3:
            # (key)(sep)(value) -> keep key+sep, replace value
            redacted = pattern.sub(r"\1\2[REDACTED]", redacted)
        else:
            # single-group token patterns
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _research_host_allowlist() -> set[str]:
    return {
        item.strip().lower().rstrip(".")
        for item in os.environ.get("MCP_BROWSER_RESEARCH_ALLOW_HOSTS", "").split(",")
        if item.strip()
    }


def _host_is_explicitly_allowed(host: str) -> bool:
    host = host.lower().rstrip(".")
    for item in _research_host_allowlist():
        if item.startswith("*."):
            suffix = item[1:]
            if host.endswith(suffix) and host != suffix[1:]:
                return True
        elif host == item:
            return True
    return False


def _strict_research_network_enabled() -> bool:
    return os.environ.get("MCP_BROWSER_RESEARCH_STRICT_NETWORK", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def validate_research_url(url: str, *, resolve_dns: bool = True) -> str:
    """Validate a URL used by browser research tools.

    This self-hosted MCP defaults to operator-trusted network access: public sites,
    localhost, RFC1918/private ranges, CGNAT/Tailscale-style ranges, .local names,
    and single-label intranet hosts are all allowed. Only URL syntax is enforced.

    Set ``MCP_BROWSER_RESEARCH_STRICT_NETWORK=1`` to opt into the legacy network
    restriction mode. In strict mode, ``MCP_BROWSER_RESEARCH_ALLOW_HOSTS`` can still
    explicitly allow exact hosts or ``*.example.internal`` patterns.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Expected an HTTP(S) URL without credentials.")

    # Default mode intentionally trusts the operator of this on-demand, self-hosted
    # MCP and permits direct access to local/LAN/private targets.
    if not _strict_research_network_enabled():
        return url

    host = parsed.hostname.lower().rstrip(".")
    if _host_is_explicitly_allowed(host):
        return url
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local") or "." not in host:
        raise ValueError("Browser research tools may not access local or single-label hosts in strict network mode.")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise ValueError("Browser research tools may not access non-global IP addresses in strict network mode.")
        return url

    if resolve_dns:
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError(f"Could not resolve research target host: {host}") from exc
        addresses = {info[4][0] for info in infos if info[4]}
        if not addresses:
            raise ValueError(f"Could not resolve research target host: {host}")
        fake_ip_v4 = ipaddress.ip_network("198.18.0.0/15")
        for address in addresses:
            try:
                resolved = ipaddress.ip_address(address)
            except ValueError:
                continue
            if isinstance(resolved, ipaddress.IPv4Address) and resolved in fake_ip_v4:
                continue
            if isinstance(resolved, ipaddress.IPv6Address):
                embedded_v4 = int(resolved) & 0xffffffff
                if int(fake_ip_v4.network_address) <= embedded_v4 <= int(fake_ip_v4.broadcast_address):
                    continue
            if not resolved.is_global:
                raise ValueError(
                    f"Browser research target {host} resolves to a non-global address in strict network mode; "
                    "use MCP_BROWSER_RESEARCH_ALLOW_HOSTS to allow it explicitly."
                )
    return url


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
    if any(pattern.fullmatch(command) for pattern in SAFE_PRIVILEGED_COMMAND_PATTERNS):
        return command
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
