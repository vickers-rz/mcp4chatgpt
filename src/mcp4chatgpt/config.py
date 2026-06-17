from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _split_paths(value: str) -> list[Path]:
    paths: list[Path] = []
    for part in value.split(":"):
        part = part.strip()
        if part:
            paths.append(Path(part).expanduser().resolve())
    return paths


@dataclass(frozen=True)
class Config:
    public_base_url: str
    bind_host: str
    bind_port: int
    auth_secret: str
    allowed_roots: list[Path]
    co_te_path: Path
    data_dir: Path
    audit_log: Path
    firecrawl_api_key: str
    firecrawl_base_url: str
    knowledge_roots: list[Path]
    knowledge_store_dir: Path
    tls_cert_path: str
    tls_key_path: str
    max_output_chars: int
    log_rotate_bytes: int
    log_retention_days: int

    @property
    def mcp_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/mcp"


def load_config() -> Config:
    project_root = Path(__file__).resolve().parents[2]
    _load_dotenv(project_root / ".env")

    data_dir = Path(os.environ.get("MCP_DATA_DIR", project_root / "data")).expanduser().resolve()
    knowledge_store = Path(
        os.environ.get("KNOWLEDGE_STORE_DIR", data_dir / "knowledge")
    ).expanduser().resolve()
    audit_log = Path(os.environ.get("MCP_AUDIT_LOG", project_root / "logs" / "audit.jsonl")).expanduser().resolve()

    secret = os.environ.get("MCP_AUTH_SECRET", "")
    if not secret or secret == "replace-with-a-long-random-secret":
        secret = secrets.token_urlsafe(32)
    default_co_te_path = project_root.parent / "codex_work_with_apps" / "co-te.py"

    return Config(
        public_base_url=os.environ.get("MCP_PUBLIC_BASE_URL", "http://127.0.0.1:8766").rstrip("/"),
        bind_host=os.environ.get("MCP_BIND_HOST", "127.0.0.1"),
        bind_port=int(os.environ.get("MCP_BIND_PORT", "8766")),
        auth_secret=secret,
        allowed_roots=_split_paths(os.environ.get("MCP_ALLOWED_ROOTS", str(Path.home() / "Documents"))),
        co_te_path=Path(os.environ.get("MCP_CO_TE_PATH", str(default_co_te_path))).expanduser().resolve(),
        data_dir=data_dir,
        audit_log=audit_log,
        firecrawl_api_key=os.environ.get("FIRECRAWL_API_KEY", ""),
        firecrawl_base_url=os.environ.get("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev").rstrip("/"),
        knowledge_roots=_split_paths(os.environ.get("KNOWLEDGE_ROOTS", str(Path.home() / "Documents"))),
        knowledge_store_dir=knowledge_store,
        tls_cert_path=os.environ.get("TLS_CERT_PATH", ""),
        tls_key_path=os.environ.get("TLS_KEY_PATH", ""),
        max_output_chars=int(os.environ.get("MCP_MAX_OUTPUT_CHARS", "50000")),
        log_rotate_bytes=int(os.environ.get("MCP_LOG_ROTATE_BYTES", str(20 * 1024 * 1024))),
        log_retention_days=int(os.environ.get("MCP_LOG_RETENTION_DAYS", "30")),
    )
