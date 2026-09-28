"""Shared data types for the downstream MCP subsystem."""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any


class DownstreamState(enum.Enum):
    """Runtime state of a downstream MCP server."""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPING = "stopping"


@dataclass
class DownstreamConfig:
    """Configuration for a single downstream MCP server."""
    id: str
    name: str = ""
    enabled: bool = True
    transport: str = "stdio"  # "stdio" for now; "http" reserved for future
    command: str = ""
    args: list[str] = field(default_factory=list)
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    startup_timeout: float = 30.0
    call_timeout: float = 60.0
    allow_tools: list[str] | None = None   # None = allow all
    deny_tools: list[str] | None = None    # None = deny none
    namespace: str = ""  # defaults to id if empty

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("downstream config 'id' must be non-empty")
        if not self.namespace:
            self.namespace = self.id
        if not self.name:
            self.name = self.id
        if self.startup_timeout <= 0:
            raise ValueError("startup_timeout must be > 0")
        if self.call_timeout <= 0:
            raise ValueError("call_timeout must be > 0")
        # Validate namespace is safe for tool naming
        if not self.namespace.replace("_", "").replace("-", "").isalnum():
            raise ValueError(
                f"namespace must be alphanumeric/underscore/hyphen: {self.namespace!r}"
            )


@dataclass
class DownstreamToolInfo:
    """A tool discovered from a downstream MCP server."""
    original_name: str
    namespaced_name: str
    description: str
    input_schema: dict[str, Any]
    downstream_id: str
    annotations: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    backend_instance_id: str = ""


@dataclass
class DownstreamStatus:
    """Observable status of a downstream MCP server."""
    id: str
    name: str
    configured: bool = True
    enabled: bool = True
    state: DownstreamState = DownstreamState.STOPPED
    pid: int | None = None
    tool_count: int = 0
    last_error: str | None = None
    started_at: float | None = None
    uptime: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "configured": self.configured,
            "enabled": self.enabled,
            "state": self.state.value,
            "tool_count": self.tool_count,
        }
        if self.pid is not None:
            d["pid"] = self.pid
        if self.last_error is not None:
            d["last_error"] = self.last_error
        if self.started_at is not None:
            d["uptime"] = round(time.time() - self.started_at, 1)
        return d
