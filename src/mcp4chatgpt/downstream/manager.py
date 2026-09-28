"""DownstreamMCPManager – orchestrates multiple downstream MCP clients.

Responsibilities:
  - Load downstream configs and start/stop clients
  - Aggregate downstream tools with namespace prefixes
  - Route namespaced tool calls to the correct downstream client
  - Report per-downstream health/status
  - Enforce allow/deny tool filtering
  - Provide fault isolation (one downstream failure doesn't affect others)
"""
from __future__ import annotations

import logging
import threading
from copy import deepcopy
from typing import Any

from .client import StdioMCPClient, StdioMCPClientError
from .config import load_downstream_configs
from .models import (
    DownstreamConfig,
    DownstreamState,
    DownstreamStatus,
    DownstreamToolInfo,
)

log = logging.getLogger(__name__)

# Separator between namespace and original tool name
_NS_SEP = "__"


class DownstreamMCPManager:
    """Manages multiple downstream MCP server connections.

    Thread-safe for tool calls from the multi-threaded HTTP server.
    """

    def __init__(self) -> None:
        self._catalog_lock = threading.RLock()
        self._clients: dict[str, StdioMCPClient] = {}
        self._configs: dict[str, DownstreamConfig] = {}
        self._tools: dict[str, DownstreamToolInfo] = {}  # namespaced_name -> info
        self._states: dict[str, DownstreamState] = {}
        self._errors: dict[str, str] = {}

    # ── Lifecycle ───────────────────────────────────────────────

    def start_all(self, project_root: "Path") -> None:  # noqa: F821
        """Load config and start all enabled downstream MCPs.

        Never raises – individual failures are captured as degraded state.
        """
        from pathlib import Path
        root = Path(project_root) if not isinstance(project_root, Path) else project_root

        configs = load_downstream_configs(root)
        if not configs:
            log.info("downstream manager: no downstream MCPs configured")
            return

        seen_namespaces: set[str] = set()
        for cfg in configs:
            self._configs[cfg.id] = cfg
            if not cfg.enabled:
                self._states[cfg.id] = DownstreamState.STOPPED
                log.info("downstream manager: %r is disabled, skipping", cfg.id)
                continue
            if cfg.namespace in seen_namespaces:
                self._states[cfg.id] = DownstreamState.FAILED
                self._errors[cfg.id] = f"duplicate downstream namespace: {cfg.namespace}"
                log.error(
                    "downstream manager: %r duplicates namespace %r",
                    cfg.id, cfg.namespace,
                )
                continue
            seen_namespaces.add(cfg.namespace)

            if cfg.transport != "stdio":
                self._states[cfg.id] = DownstreamState.FAILED
                self._errors[cfg.id] = f"unsupported transport: {cfg.transport}"
                log.warning(
                    "downstream manager: %r has unsupported transport %r",
                    cfg.id, cfg.transport,
                )
                continue
            if not cfg.command:
                self._states[cfg.id] = DownstreamState.DEGRADED
                self._errors[cfg.id] = "enabled stdio downstream requires a command"
                log.warning("downstream manager: %r has no command", cfg.id)
                continue

            self._start_one(cfg)

    def _start_one(self, cfg: DownstreamConfig) -> None:
        """Start a single downstream MCP. Captures errors, never raises."""
        self._states[cfg.id] = DownstreamState.STARTING
        try:
            client = StdioMCPClient(
                downstream_id=cfg.id,
                command=cfg.command,
                args=cfg.args,
                cwd=cfg.cwd or None,
                env=cfg.env or None,
                startup_timeout=cfg.startup_timeout,
                call_timeout=cfg.call_timeout,
            )
            client.start()

            with self._catalog_lock:
                self._clients[cfg.id] = client
                self._states[cfg.id] = DownstreamState.RUNNING
                self._errors.pop(cfg.id, None)
                self._register_tools(cfg, client)

            log.info(
                "downstream manager: %r started (pid=%s, tools=%d)",
                cfg.id, client.pid, len(client.tools),
            )

        except Exception as exc:
            # An optional downstream failure degrades only that integration;
            # the MCP4ChatGPT core and other channels remain available.
            self._states[cfg.id] = DownstreamState.DEGRADED
            self._errors[cfg.id] = str(exc)[:1000]
            log.error(
                "downstream manager: %r failed to start: %s",
                cfg.id, exc,
            )

    def _register_tools(
        self, cfg: DownstreamConfig, client: StdioMCPClient,
    ) -> None:
        """Register downstream tools with namespace prefix and filtering."""
        replacement: dict[str, DownstreamToolInfo] = {}
        instance_id, tools = client.catalog_snapshot()
        for tool in tools:
            original_name = tool.get("name", "")
            if not original_name:
                continue

            # Apply allow/deny filtering
            if cfg.allow_tools is not None:
                if original_name not in cfg.allow_tools:
                    log.debug(
                        "downstream %s: tool %r filtered by allow_tools",
                        cfg.id, original_name,
                    )
                    continue
            if cfg.deny_tools is not None:
                if original_name in cfg.deny_tools:
                    log.debug(
                        "downstream %s: tool %r filtered by deny_tools",
                        cfg.id, original_name,
                    )
                    continue

            namespaced = f"{cfg.namespace}{_NS_SEP}{original_name}"

            info = DownstreamToolInfo(
                original_name=original_name,
                namespaced_name=namespaced,
                description=tool.get("description", ""),
                input_schema=tool.get("inputSchema", {}),
                downstream_id=cfg.id,
                annotations=tool.get("annotations") if isinstance(tool.get("annotations"), dict) else None,
                output_schema=tool.get("outputSchema") if isinstance(tool.get("outputSchema"), dict) else None,
                backend_instance_id=instance_id,
            )
            replacement[namespaced] = info
        with self._catalog_lock:
            self._tools = {
                name: info for name, info in self._tools.items()
                if info.downstream_id != cfg.id
            } | replacement

    def stop_all(self, timeout: float = 15.0) -> None:
        """Stop all downstream MCP clients gracefully."""
        for ds_id, client in list(self._clients.items()):
            log.info("downstream manager: stopping %r", ds_id)
            self._states[ds_id] = DownstreamState.STOPPING
            try:
                client.stop(timeout=timeout)
            except Exception as exc:
                log.warning(
                    "downstream manager: error stopping %r: %s", ds_id, exc
                )
                self._states[ds_id] = DownstreamState.DEGRADED
                self._errors[ds_id] = f"shutdown failed: {exc}"[:1000]
            else:
                self._states[ds_id] = DownstreamState.STOPPED

        with self._catalog_lock:
            self._clients.clear()
            self._tools.clear()

    # ── Tool aggregation ────────────────────────────────────────

    def get_tools(self) -> list[DownstreamToolInfo]:
        """Return all registered downstream tools."""
        with self._catalog_lock:
            for ds_id, client in self._clients.items():
                self._register_tools(self._configs[ds_id], client)
            return deepcopy(list(self._tools.values()))

    def has_tool(self, namespaced_name: str) -> bool:
        """Check if a namespaced tool name belongs to a downstream."""
        with self._catalog_lock:
            return namespaced_name in self._tools

    def call_tool(
        self, namespaced_name: str, arguments: dict[str, Any],
        *, expected_tool: DownstreamToolInfo | None = None,
    ) -> dict[str, Any]:
        """Route a tool call to the appropriate downstream client.

        Returns the raw MCP result from the downstream server.
        Raises StdioMCPClientError on failure.
        """
        self.get_tools()
        with self._catalog_lock:
            info = self._tools.get(namespaced_name)
            if (expected_tool is not None and info is not None
                    and info.backend_instance_id != expected_tool.backend_instance_id):
                raise StdioMCPClientError("capability_backend_changed")
            if expected_tool is not None and info != expected_tool:
                raise StdioMCPClientError("capability_definition_changed")
            client = self._clients.get(info.downstream_id) if info else None
        if info is None:
            raise StdioMCPClientError(
                f"Unknown downstream tool: {namespaced_name}"
            )

        if client is None:
            raise StdioMCPClientError(
                f"Downstream {info.downstream_id!r} is not running"
            )

        if not client.is_running:
            self._states[info.downstream_id] = DownstreamState.DEGRADED
            detail = client.transport_error or "downstream process/transport is unavailable"
            self._errors[info.downstream_id] = detail[:1000]
            raise StdioMCPClientError(
                f"Downstream {info.downstream_id!r} is unavailable: {detail}"
            )

        try:
            if isinstance(client, StdioMCPClient):
                return client.call_tool(info.original_name, arguments,
                                        expected_instance_id=info.backend_instance_id)
            return client.call_tool(info.original_name, arguments)
        except StdioMCPClientError as exc:
            # A single tool-level JSON-RPC error does not necessarily mean the
            # process is unhealthy.  Mark degraded only when the process died.
            if not client.is_running:
                self._states[info.downstream_id] = DownstreamState.DEGRADED
                self._errors[info.downstream_id] = str(exc)[:1000]
            raise

    # ── Health / status ─────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        """Return aggregated status for all downstream MCPs."""
        tools = self.get_tools()
        statuses: list[dict[str, Any]] = []
        for ds_id, cfg in self._configs.items():
            client = self._clients.get(ds_id)
            state = self._states.get(ds_id, DownstreamState.STOPPED)
            if client is not None and state == DownstreamState.RUNNING and not client.is_running:
                state = DownstreamState.DEGRADED
                self._states[ds_id] = state
                self._errors[ds_id] = (
                    client.transport_error or "downstream process/transport is unavailable"
                )[:1000]
            status = DownstreamStatus(
                id=ds_id,
                name=cfg.name,
                configured=True,
                enabled=cfg.enabled,
                state=state,
                pid=client.pid if client else None,
                tool_count=sum(
                    1 for t in tools
                    if t.downstream_id == ds_id
                ),
                last_error=self._errors.get(ds_id),
                started_at=client.started_at if client else None,
            )
            record = status.to_dict()
            if client is not None and client.catalog_error:
                record["catalog_error"] = client.catalog_error
            statuses.append(record)

        return {"downstream": statuses}

    def is_downstream_tool(self, name: str) -> bool:
        """Check if a tool name looks like a downstream-namespaced tool."""
        with self._catalog_lock:
            return _NS_SEP in name and name in self._tools

    # ── Namespace helpers ───────────────────────────────────────

    @staticmethod
    def parse_namespace(namespaced_name: str) -> tuple[str, str] | None:
        """Split 'namespace__tool_name' into (namespace, tool_name)."""
        if _NS_SEP not in namespaced_name:
            return None
        parts = namespaced_name.split(_NS_SEP, 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return None
        return (parts[0], parts[1])
