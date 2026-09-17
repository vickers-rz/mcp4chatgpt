"""Downstream MCP configuration loader.

Reads downstream MCP server definitions from a TOML file.
Falls back gracefully if no config exists (no downstream servers).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

from .models import DownstreamConfig

log = logging.getLogger(__name__)

_DEFAULT_CONFIG_FILENAME = "downstream_mcp.toml"

# Environment variable for config path override
_CONFIG_PATH_ENV = "MCP_DOWNSTREAM_CONFIG"


def _resolve_config_path(project_root: Path) -> Path | None:
    """Find the downstream config file path."""
    # Check env override first
    env_path = os.environ.get(_CONFIG_PATH_ENV)
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if p.is_file():
            return p
        log.warning(
            "downstream config: %s=%s does not exist",
            _CONFIG_PATH_ENV, env_path,
        )
        return None

    # Default location
    default = project_root / _DEFAULT_CONFIG_FILENAME
    if default.is_file():
        return default
    return None


def _sanitize_env_value(key: str, value: str) -> str:
    """Strip env values and reject obviously dangerous content."""
    v = value.strip()
    if len(v) > 4096:
        raise ValueError(f"env value for {key!r} exceeds 4096 chars")
    return v


def _parse_downstream_entry(
    ds_id: str, entry: dict[str, Any],
) -> DownstreamConfig:
    """Parse one [downstream_mcp.<id>] table into DownstreamConfig."""
    env_raw = entry.get("env", {})
    env = {}
    for k, v in env_raw.items():
        env[str(k)] = _sanitize_env_value(str(k), str(v))

    # Validate allow/deny are lists of strings if present
    allow = entry.get("allow_tools")
    deny = entry.get("deny_tools")
    if allow is not None:
        allow = [str(t) for t in allow]
    if deny is not None:
        deny = [str(t) for t in deny]

    return DownstreamConfig(
        id=ds_id,
        name=str(entry.get("name", ds_id)),
        enabled=bool(entry.get("enabled", True)),
        transport=str(entry.get("transport", "stdio")),
        command=str(entry.get("command", "")),
        args=[str(a) for a in entry.get("args", [])],
        cwd=str(entry.get("cwd", "")),
        env=env,
        startup_timeout=float(entry.get("startup_timeout", 30.0)),
        call_timeout=float(entry.get("call_timeout", 60.0)),
        allow_tools=allow,
        deny_tools=deny,
        namespace=str(entry.get("namespace", ds_id)),
    )


def load_downstream_configs(
    project_root: Path,
) -> list[DownstreamConfig]:
    """Load downstream MCP configs from TOML.

    Returns an empty list if no config file exists or parsing fails.
    Never raises — downstream config errors are logged, not fatal.
    """
    config_path = _resolve_config_path(project_root)
    if config_path is None:
        log.info("downstream config: no config file found, no downstream MCPs")
        return []

    try:
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:
        log.error(
            "downstream config: failed to parse %s: %s",
            config_path, exc,
        )
        return []

    ds_table = data.get("downstream_mcp", {})
    if not isinstance(ds_table, dict):
        log.error("downstream config: 'downstream_mcp' must be a table")
        return []

    configs: list[DownstreamConfig] = []
    for ds_id, entry in ds_table.items():
        if not isinstance(entry, dict):
            log.warning(
                "downstream config: skipping non-table entry %r", ds_id
            )
            continue
        try:
            cfg = _parse_downstream_entry(str(ds_id), entry)
            configs.append(cfg)
            log.info(
                "downstream config: loaded %r (enabled=%s, transport=%s)",
                cfg.id, cfg.enabled, cfg.transport,
            )
        except Exception as exc:
            log.error(
                "downstream config: failed to parse entry %r: %s",
                ds_id, exc,
            )

    return configs
