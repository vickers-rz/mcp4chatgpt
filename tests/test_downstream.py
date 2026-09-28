"""Tests for the downstream MCP aggregator subsystem.

Uses a fake stdio MCP server (tests/fake_mcp_server.py) to test:
  1. subprocess startup
  2. MCP initialize handshake
  3. tools/list discovery
  4. namespace prefixing
  5. tools/call forwarding
  6. schema preservation
  7. startup failure / degraded mode
  8. timeout handling
  9. graceful shutdown
 10. orphan prevention
 11. native tool regression
 12. allowlist/denylist filtering
 13. error propagation
 14. manager health/status reporting
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

# Ensure src/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp4chatgpt.downstream.models import (
    DownstreamConfig,
)
from mcp4chatgpt.downstream.client import StdioMCPClient, StdioMCPClientError
from mcp4chatgpt.downstream.manager import DownstreamMCPManager

# Path to the fake MCP server
FAKE_SERVER = str(Path(__file__).resolve().parent / "fake_mcp_server.py")
PYTHON = sys.executable


# ── Helpers ────────────────────────────────────────────────────

def _make_toml(tmp: Path, entries: dict[str, dict[str, Any]]) -> Path:
    """Write a downstream_mcp.toml config to a temp directory."""
    lines = []
    for ds_id, fields in entries.items():
        lines.append(f"[downstream_mcp.{ds_id}]")
        for k, v in fields.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, str):
                lines.append(f'{k} = "{v}"')
            elif isinstance(v, (int, float)):
                lines.append(f"{k} = {v}")
            elif isinstance(v, list):
                items = ", ".join(f'"{x}"' for x in v)
                lines.append(f"{k} = [{items}]")
        lines.append("")

    toml_path = tmp / "downstream_mcp.toml"
    toml_path.write_text("\n".join(lines), encoding="utf-8")
    return tmp


# ── StdioMCPClient tests ──────────────────────────────────────

class TestStdioMCPClient:
    """Tests for the low-level stdio MCP client."""

    def test_start_and_discover_tools(self):
        """1. subprocess startup + 2. initialize + 3. tools/list"""
        client = StdioMCPClient(
            downstream_id="test",
            command=PYTHON,
            args=[FAKE_SERVER],
            startup_timeout=10.0,
            call_timeout=5.0,
        )
        try:
            client.start()
            assert client.is_running
            assert client.pid is not None
            assert client.started_at is not None
            tools = client.tools
            assert len(tools) == 3
            names = {t["name"] for t in tools}
            assert "list_pages" in names
            assert "evaluate_script" in names
            assert "select_page" in names
        finally:
            client.stop(timeout=5)
            assert not client.is_running

    def test_concurrent_tool_calls(self):
        """Multiple HTTP worker threads can safely share one downstream loop."""
        from concurrent.futures import ThreadPoolExecutor

        client = StdioMCPClient(
            downstream_id="test", command=PYTHON, args=[FAKE_SERVER],
            startup_timeout=10.0, call_timeout=5.0,
        )
        try:
            client.start()
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(client.call_tool, "list_pages", {}) for _ in range(8)]
                results = [f.result(timeout=5) for f in futures]
            assert len(results) == 8
            assert all("content" in result for result in results)
            assert client._pending == {}
        finally:
            client.stop(timeout=5)

    def test_tool_call_forwarding(self):
        """5. tools/call forwarding"""
        client = StdioMCPClient(
            downstream_id="test",
            command=PYTHON,
            args=[FAKE_SERVER],
            startup_timeout=10.0,
            call_timeout=5.0,
        )
        try:
            client.start()
            result = client.call_tool("list_pages", {})
            assert "content" in result
            content = result["content"]
            assert isinstance(content, list)
            assert len(content) > 0
            parsed = json.loads(content[0]["text"])
            assert isinstance(parsed, list)
            assert parsed[0]["title"] == "Example"
        finally:
            client.stop(timeout=5)

    def test_tool_call_with_arguments(self):
        """5. tools/call forwarding with arguments"""
        client = StdioMCPClient(
            downstream_id="test",
            command=PYTHON,
            args=[FAKE_SERVER],
            startup_timeout=10.0,
            call_timeout=5.0,
        )
        try:
            client.start()
            result = client.call_tool("evaluate_script", {
                "expression": "1+1",
            })
            content = result["content"]
            parsed = json.loads(content[0]["text"])
            assert parsed["result"] == "evaluated: 1+1"
        finally:
            client.stop(timeout=5)

    def test_large_single_line_response_exceeds_default_streamreader_limit(self):
        """Real MCP tool payloads can exceed asyncio's default ~64 KiB line limit."""
        client = StdioMCPClient(
            downstream_id="test",
            command=PYTHON,
            args=[FAKE_SERVER],
            startup_timeout=10.0,
            call_timeout=5.0,
        )
        try:
            client.start()
            result = client.call_tool("evaluate_script", {"expression": "__large__"})
            text = result["content"][0]["text"]
            assert len(text) == 200_000
            assert client.is_running
            assert client.transport_error is None
        finally:
            client.stop(timeout=5)

    def test_schema_preservation(self):
        """6. schema preservation — inputSchema from downstream intact"""
        client = StdioMCPClient(
            downstream_id="test",
            command=PYTHON,
            args=[FAKE_SERVER],
            startup_timeout=10.0,
            call_timeout=5.0,
        )
        try:
            client.start()
            eval_tool = next(
                t for t in client.tools if t["name"] == "evaluate_script"
            )
            schema = eval_tool["inputSchema"]
            assert schema["type"] == "object"
            assert "expression" in schema["properties"]
            assert "expression" in schema["required"]
            assert schema["additionalProperties"] is False
            list_tool = next(t for t in client.tools if t["name"] == "list_pages")
            assert list_tool["annotations"]["readOnlyHint"] is True
            assert list_tool["outputSchema"]["type"] == "object"
        finally:
            client.stop(timeout=5)

    def test_startup_failure_command_not_found(self):
        """7. startup failure — command not found"""
        client = StdioMCPClient(
            downstream_id="test",
            command="/nonexistent/binary",
            args=[],
            startup_timeout=5.0,
            call_timeout=5.0,
        )
        with pytest.raises(StdioMCPClientError, match="startup failed"):
            client.start()

    def test_startup_timeout_cleans_child(self):
        client = StdioMCPClient(
            downstream_id="test",
            command=PYTHON,
            args=[FAKE_SERVER, "--slow=0.5"],
            startup_timeout=0.05,
            call_timeout=5.0,
        )
        with pytest.raises(StdioMCPClientError, match="startup failed"):
            client.start()
        assert not client.is_running
        if client.pid is not None:
            with pytest.raises(ProcessLookupError):
                os.kill(client.pid, 0)

    def test_startup_failure_init_error(self):
        """7. startup failure — downstream returns init error"""
        client = StdioMCPClient(
            downstream_id="test",
            command=PYTHON,
            args=[FAKE_SERVER, "--fail-init"],
            startup_timeout=10.0,
            call_timeout=5.0,
        )
        with pytest.raises(StdioMCPClientError, match="startup failed"):
            client.start()
        if client.pid is not None:
            with pytest.raises(ProcessLookupError):
                os.kill(client.pid, 0)

    def test_graceful_shutdown(self):
        """9. graceful shutdown — no orphans"""
        client = StdioMCPClient(
            downstream_id="test",
            command=PYTHON,
            args=[FAKE_SERVER],
            startup_timeout=10.0,
            call_timeout=5.0,
        )
        client.start()
        pid = client.pid
        assert pid is not None

        client.stop(timeout=5)
        assert not client.is_running

        # Verify process is actually gone
        time.sleep(0.3)
        try:
            os.kill(pid, 0)
            # Process still exists — might be zombie, try harder
            time.sleep(1)
            try:
                os.kill(pid, 0)
                pytest.fail(f"Process {pid} still alive after stop")
            except ProcessLookupError:
                pass  # Good
        except ProcessLookupError:
            pass  # Good — process is gone

    def test_graceful_shutdown_allows_eof_cleanup(self, tmp_path):
        """EOF cleanup completes before process-group termination."""
        marker = tmp_path / "eof-cleanup.txt"
        client = StdioMCPClient(
            downstream_id="test",
            command=PYTHON,
            args=[
                FAKE_SERVER,
                f"--eof-marker={marker}",
                "--eof-delay=0.25",
            ],
            startup_timeout=10.0,
            call_timeout=5.0,
        )
        client.start()
        client.stop(timeout=5)

        assert marker.read_text(encoding="utf-8") == "clean"

    def test_call_after_stop_raises(self):
        """Client raises after stop"""
        client = StdioMCPClient(
            downstream_id="test",
            command=PYTHON,
            args=[FAKE_SERVER],
            startup_timeout=10.0,
            call_timeout=5.0,
        )
        client.start()
        client.stop(timeout=5)

        with pytest.raises(StdioMCPClientError, match="not running"):
            client.call_tool("list_pages", {})

    def test_tool_call_timeout_does_not_kill_client(self):
        """8. call timeout is bounded and the downstream process survives."""
        client = StdioMCPClient(
            downstream_id="test",
            command=PYTHON,
            args=[FAKE_SERVER, "--tool-slow=0.25"],
            startup_timeout=10.0,
            call_timeout=0.05,
        )
        try:
            client.start()
            started = time.monotonic()
            with pytest.raises(StdioMCPClientError, match="failed"):
                client.call_tool("list_pages", {})
            elapsed = time.monotonic() - started
            assert elapsed < 0.2
            assert client.is_running
            time.sleep(0.3)
            assert client._pending == {}
        finally:
            client.stop(timeout=5)

    def test_error_propagation(self):
        """13. error propagation from downstream"""
        client = StdioMCPClient(
            downstream_id="test",
            command=PYTHON,
            args=[FAKE_SERVER],
            startup_timeout=10.0,
            call_timeout=5.0,
        )
        try:
            client.start()
            # Call a nonexistent tool
            with pytest.raises(StdioMCPClientError, match="Unknown tool"):
                client.call_tool("nonexistent_tool", {})
        finally:
            client.stop(timeout=5)


# ── DownstreamConfig model tests ───────────────────────────────

class TestDownstreamConfig:
    def test_defaults(self):
        cfg = DownstreamConfig(id="test")
        assert cfg.namespace == "test"
        assert cfg.name == "test"
        assert cfg.enabled is True
        assert cfg.transport == "stdio"

    def test_invalid_timeouts_raise(self):
        with pytest.raises(ValueError, match="startup_timeout"):
            DownstreamConfig(id="test", startup_timeout=0)
        with pytest.raises(ValueError, match="call_timeout"):
            DownstreamConfig(id="test", call_timeout=0)

    def test_empty_id_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            DownstreamConfig(id="")

    def test_unsafe_namespace_raises(self):
        with pytest.raises(ValueError, match="alphanumeric"):
            DownstreamConfig(id="test", namespace="bad namespace!")


# ── DownstreamMCPManager tests ─────────────────────────────────

class TestDownstreamMCPManager:
    """Tests for the high-level downstream manager."""

    def test_start_with_fake_server(self, tmp_path):
        """1-5. Full lifecycle with fake server"""
        config_dir = _make_toml(tmp_path, {
            "fake": {
                "enabled": True,
                "transport": "stdio",
                "command": PYTHON,
                "args": [FAKE_SERVER],
                "startup_timeout": 10.0,
                "call_timeout": 5.0,
            },
        })
        manager = DownstreamMCPManager()
        try:
            manager.start_all(config_dir)

            # 3. tools discovered
            tools = manager.get_tools()
            assert len(tools) == 3

            # 4. namespace
            names = {t.namespaced_name for t in tools}
            assert "fake__list_pages" in names
            assert "fake__evaluate_script" in names
            assert "fake__select_page" in names
            list_info = next(t for t in tools if t.namespaced_name == "fake__list_pages")
            assert list_info.annotations == {"readOnlyHint": True}
            assert list_info.output_schema is not None

            # 5. call forwarding
            result = manager.call_tool("fake__list_pages", {})
            assert "content" in result
            assert result["structuredContent"]["pages"][0]["title"] == "Example"

            # 14. status
            status = manager.get_status()
            ds_list = status["downstream"]
            assert len(ds_list) == 1
            assert ds_list[0]["state"] == "running"
            assert ds_list[0]["tool_count"] == 3
        finally:
            manager.stop_all(timeout=5)

    def test_namespace_collision_safe(self, tmp_path):
        """4. namespace prevents tool name collisions"""
        config_dir = _make_toml(tmp_path, {
            "alpha": {
                "enabled": True,
                "transport": "stdio",
                "command": PYTHON,
                "args": [FAKE_SERVER],
                "startup_timeout": 10.0,
            },
            "beta": {
                "enabled": True,
                "transport": "stdio",
                "command": PYTHON,
                "args": [FAKE_SERVER],
                "startup_timeout": 10.0,
            },
        })
        manager = DownstreamMCPManager()
        try:
            manager.start_all(config_dir)
            tools = manager.get_tools()
            names = {t.namespaced_name for t in tools}
            # Each downstream gets its own namespace
            assert "alpha__list_pages" in names
            assert "beta__list_pages" in names
            assert len(names) == 6  # 3 tools × 2 namespaces
        finally:
            manager.stop_all(timeout=5)

    def test_duplicate_namespace_isolated(self, tmp_path):
        config_dir = _make_toml(tmp_path, {
            "alpha": {
                "enabled": True, "transport": "stdio", "command": PYTHON,
                "args": [FAKE_SERVER], "namespace": "shared", "startup_timeout": 10.0,
            },
            "beta": {
                "enabled": True, "transport": "stdio", "command": PYTHON,
                "args": [FAKE_SERVER], "namespace": "shared", "startup_timeout": 10.0,
            },
        })
        manager = DownstreamMCPManager()
        try:
            manager.start_all(config_dir)
            names = {t.namespaced_name for t in manager.get_tools()}
            assert "shared__list_pages" in names
            by_id = {s["id"]: s for s in manager.get_status()["downstream"]}
            assert by_id["alpha"]["state"] == "running"
            assert by_id["beta"]["state"] == "failed"
        finally:
            manager.stop_all(timeout=5)

    def test_disabled_duplicate_namespace_does_not_block_enabled(self, tmp_path):
        config_dir = _make_toml(tmp_path, {
            "disabled": {
                "enabled": False, "transport": "stdio", "command": PYTHON,
                "args": [FAKE_SERVER], "namespace": "shared",
            },
            "enabled": {
                "enabled": True, "transport": "stdio", "command": PYTHON,
                "args": [FAKE_SERVER], "namespace": "shared", "startup_timeout": 10.0,
            },
        })
        manager = DownstreamMCPManager()
        try:
            manager.start_all(config_dir)
            names = {t.namespaced_name for t in manager.get_tools()}
            assert "shared__list_pages" in names
        finally:
            manager.stop_all(timeout=5)

    def test_startup_failure_degraded_mode(self, tmp_path):
        """7-8. startup failure results in degraded state, not crash"""
        config_dir = _make_toml(tmp_path, {
            "broken": {
                "enabled": True,
                "transport": "stdio",
                "command": PYTHON,
                "args": [FAKE_SERVER, "--fail-init"],
                "startup_timeout": 10.0,
            },
        })
        manager = DownstreamMCPManager()
        # Should NOT raise
        manager.start_all(config_dir)

        status = manager.get_status()
        ds_list = status["downstream"]
        assert len(ds_list) == 1
        assert ds_list[0]["state"] == "degraded"
        assert ds_list[0]["last_error"] is not None

        # No tools from broken downstream
        assert len(manager.get_tools()) == 0

    def test_missing_command_is_degraded_not_fatal(self, tmp_path):
        config_dir = _make_toml(tmp_path, {
            "broken": {"enabled": True, "transport": "stdio"},
        })
        manager = DownstreamMCPManager()
        manager.start_all(config_dir)
        status = manager.get_status()["downstream"][0]
        assert status["state"] == "degraded"
        assert "command" in status["last_error"]
        assert manager.get_tools() == []

    def test_unsupported_transport_is_failed_not_fatal(self, tmp_path):
        config_dir = _make_toml(tmp_path, {
            "future_http": {"enabled": True, "transport": "http", "command": "unused"},
        })
        manager = DownstreamMCPManager()
        manager.start_all(config_dir)
        status = manager.get_status()["downstream"][0]
        assert status["state"] == "failed"
        assert "unsupported transport" in status["last_error"]

    def test_disabled_downstream_skipped(self, tmp_path):
        """Disabled downstream is skipped cleanly"""
        config_dir = _make_toml(tmp_path, {
            "disabled_one": {
                "enabled": False,
                "transport": "stdio",
                "command": PYTHON,
                "args": [FAKE_SERVER],
            },
        })
        manager = DownstreamMCPManager()
        manager.start_all(config_dir)

        status = manager.get_status()
        ds_list = status["downstream"]
        assert len(ds_list) == 1
        assert ds_list[0]["state"] == "stopped"
        assert len(manager.get_tools()) == 0

    def test_allow_tools_filter(self, tmp_path):
        """12. allowlist filtering"""
        config_dir = _make_toml(tmp_path, {
            "filtered": {
                "enabled": True,
                "transport": "stdio",
                "command": PYTHON,
                "args": [FAKE_SERVER],
                "startup_timeout": 10.0,
                "allow_tools": ["list_pages"],
            },
        })
        manager = DownstreamMCPManager()
        try:
            manager.start_all(config_dir)
            tools = manager.get_tools()
            assert len(tools) == 1
            assert tools[0].namespaced_name == "filtered__list_pages"
        finally:
            manager.stop_all(timeout=5)

    def test_deny_tools_filter(self, tmp_path):
        """12. denylist filtering"""
        config_dir = _make_toml(tmp_path, {
            "filtered": {
                "enabled": True,
                "transport": "stdio",
                "command": PYTHON,
                "args": [FAKE_SERVER],
                "startup_timeout": 10.0,
                "deny_tools": ["evaluate_script"],
            },
        })
        manager = DownstreamMCPManager()
        try:
            manager.start_all(config_dir)
            tools = manager.get_tools()
            names = {t.original_name for t in tools}
            assert "evaluate_script" not in names
            assert "list_pages" in names
            assert "select_page" in names
        finally:
            manager.stop_all(timeout=5)

    def test_unknown_tool_raises(self, tmp_path):
        """Call to unknown downstream tool raises"""
        config_dir = _make_toml(tmp_path, {
            "fake": {
                "enabled": True,
                "transport": "stdio",
                "command": PYTHON,
                "args": [FAKE_SERVER],
                "startup_timeout": 10.0,
            },
        })
        manager = DownstreamMCPManager()
        try:
            manager.start_all(config_dir)
            with pytest.raises(StdioMCPClientError, match="Unknown downstream tool"):
                manager.call_tool("fake__nonexistent", {})
        finally:
            manager.stop_all(timeout=5)

    def test_no_config_file(self, tmp_path):
        """No config file → no downstreams, no error"""
        manager = DownstreamMCPManager()
        manager.start_all(tmp_path)  # no toml file exists
        assert len(manager.get_tools()) == 0
        status = manager.get_status()
        assert status["downstream"] == []

    def test_graceful_shutdown_cleans_processes(self, tmp_path):
        """10. graceful shutdown + 11. orphan prevention"""
        config_dir = _make_toml(tmp_path, {
            "fake": {
                "enabled": True,
                "transport": "stdio",
                "command": PYTHON,
                "args": [FAKE_SERVER],
                "startup_timeout": 10.0,
            },
        })
        manager = DownstreamMCPManager()
        manager.start_all(config_dir)

        # Capture PIDs
        status = manager.get_status()
        pids = [ds.get("pid") for ds in status["downstream"] if ds.get("pid")]
        assert len(pids) > 0

        manager.stop_all(timeout=5)

        # Verify processes are gone
        time.sleep(0.5)
        for pid in pids:
            try:
                os.kill(pid, 0)
                time.sleep(1)
                try:
                    os.kill(pid, 0)
                    pytest.fail(f"Process {pid} still alive after stop_all")
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass  # Already gone

    def test_mixed_healthy_and_broken(self, tmp_path):
        """Failure isolation: one broken + one healthy"""
        config_dir = _make_toml(tmp_path, {
            "healthy": {
                "enabled": True,
                "transport": "stdio",
                "command": PYTHON,
                "args": [FAKE_SERVER],
                "startup_timeout": 10.0,
            },
            "broken": {
                "enabled": True,
                "transport": "stdio",
                "command": PYTHON,
                "args": [FAKE_SERVER, "--fail-init"],
                "startup_timeout": 10.0,
            },
        })
        manager = DownstreamMCPManager()
        try:
            manager.start_all(config_dir)

            # Healthy works
            tools = manager.get_tools()
            names = {t.namespaced_name for t in tools}
            assert "healthy__list_pages" in names
            # Broken has no tools
            assert "broken__list_pages" not in names

            # Status reflects both
            status = manager.get_status()
            by_id = {ds["id"]: ds for ds in status["downstream"]}
            assert by_id["healthy"]["state"] == "running"
            assert by_id["broken"]["state"] == "degraded"

            # Can still call healthy tools
            result = manager.call_tool("healthy__list_pages", {})
            assert "content" in result
        finally:
            manager.stop_all(timeout=5)


# ── ToolRegistry integration tests ─────────────────────────────

class TestToolRegistryDownstreamIntegration:
    """Test that downstream tools integrate with the existing ToolRegistry."""

    def test_native_tools_not_lost_with_downstream(self, tmp_path, monkeypatch):
        """11. native tool regression — adding downstream doesn't lose native"""
        config_dir = _make_toml(tmp_path, {
            "fake": {
                "enabled": True,
                "transport": "stdio",
                "command": PYTHON,
                "args": [FAKE_SERVER],
                "startup_timeout": 10.0,
            },
        })

        manager = DownstreamMCPManager()
        manager.start_all(config_dir)
        try:
            from mcp4chatgpt.config import load_config
            from mcp4chatgpt.audit import AuditLogger
            from mcp4chatgpt.tools import ToolRegistry

            os.environ.setdefault("MCP_AUTH_SECRET", "test-secret-for-downstream-test")
            os.environ.setdefault("MCP_ALLOWED_ROOTS", str(tmp_path))
            monkeypatch.setenv("MCP_TOOL_EXPOSURE", "full")
            config = load_config()
            audit = AuditLogger(tmp_path / "audit.jsonl")

            # Registry WITHOUT downstream
            reg_without = ToolRegistry(config, audit)
            base_tools = reg_without.list_tools(auth_required=False)["tools"]
            base_names = {t["name"] for t in base_tools}

            # Registry WITH downstream
            reg_with = ToolRegistry(config, audit, downstream_manager=manager)
            all_tools = reg_with.list_tools(auth_required=False)["tools"]
            all_names = {t["name"] for t in all_tools}

            # All base tools still present
            assert base_names.issubset(all_names), f"Missing: {base_names - all_names}"

            # Downstream tools are added
            assert "fake__list_pages" in all_names
            assert "fake__evaluate_script" in all_names
            assert "fake__select_page" in all_names
            list_def = next(t for t in all_tools if t["name"] == "fake__list_pages")
            assert list_def["annotations"]["readOnlyHint"] is True
            assert list_def["annotations"]["openWorldHint"] is True
            assert list_def["outputSchema"]["type"] == "object"

            # Total is base + downstream
            assert len(all_names) == len(base_names) + 3
        finally:
            manager.stop_all(timeout=5)

    def test_downstream_tool_callable_through_registry(self, tmp_path):
        """Downstream tools can be called through ToolRegistry.call_tool"""
        config_dir = _make_toml(tmp_path, {
            "fake": {
                "enabled": True,
                "transport": "stdio",
                "command": PYTHON,
                "args": [FAKE_SERVER],
                "startup_timeout": 10.0,
            },
        })

        manager = DownstreamMCPManager()
        manager.start_all(config_dir)
        try:
            from mcp4chatgpt.config import load_config
            from mcp4chatgpt.audit import AuditLogger
            from mcp4chatgpt.tools import ToolRegistry

            os.environ.setdefault("MCP_AUTH_SECRET", "test-secret-for-downstream-test")
            os.environ.setdefault("MCP_ALLOWED_ROOTS", str(tmp_path))
            config = load_config()
            audit = AuditLogger(tmp_path / "audit.jsonl")
            reg = ToolRegistry(config, audit, downstream_manager=manager)

            result = reg.call_tool("fake__list_pages", {})
            assert "content" in result
            assert "structuredContent" in result
            assert result["structuredContent"]["pages"][0]["title"] == "Example"
            assert result["content"][0]["type"] == "text"
            audit_text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
            assert '"channel":"fake"' in audit_text.replace(" ", "")
            assert "Example" not in audit_text
            assert "structuredContent" not in audit_text
        finally:
            manager.stop_all(timeout=5)


# ── Config loading tests ───────────────────────────────────────

class TestDownstreamConfig_Loading:
    def test_load_valid_toml(self, tmp_path):
        """Config loads from valid TOML"""
        from mcp4chatgpt.downstream.config import load_downstream_configs
        _make_toml(tmp_path, {
            "test_server": {
                "enabled": True,
                "transport": "stdio",
                "command": "echo",
                "args": ["hello"],
                "cwd": "/tmp",
                "startup_timeout": 15.0,
                "call_timeout": 30.0,
            },
        })
        configs = load_downstream_configs(tmp_path)
        assert len(configs) == 1
        assert configs[0].id == "test_server"
        assert configs[0].command == "echo"
        assert configs[0].args == ["hello"]
        assert configs[0].startup_timeout == 15.0

    def test_config_env_not_exposed_in_status(self, tmp_path):
        from mcp4chatgpt.downstream.config import load_downstream_configs
        config_path = tmp_path / "downstream_mcp.toml"
        config_path.write_text(
            '[downstream_mcp.env_test]\n'
            'enabled = false\ntransport = "stdio"\ncommand = "echo"\n'
            '[downstream_mcp.env_test.env]\nSAMPLE_VALUE = "fixture-value"\n',
            encoding="utf-8",
        )
        configs = load_downstream_configs(tmp_path)
        assert configs[0].env["SAMPLE_VALUE"] == "fixture-value"
        manager = DownstreamMCPManager()
        manager.start_all(tmp_path)
        status_text = json.dumps(manager.get_status())
        assert "fixture-value" not in status_text
        assert "SAMPLE_VALUE" not in status_text

    def test_load_no_file(self, tmp_path):
        """No config file → empty list, no error"""
        from mcp4chatgpt.downstream.config import load_downstream_configs
        configs = load_downstream_configs(tmp_path)
        assert configs == []

    def test_load_invalid_toml(self, tmp_path):
        """Invalid TOML → empty list, no error"""
        from mcp4chatgpt.downstream.config import load_downstream_configs
        (tmp_path / "downstream_mcp.toml").write_text(
            "this is not [valid toml!!!",
            encoding="utf-8",
        )
        configs = load_downstream_configs(tmp_path)
        assert configs == []


# ── Namespace parsing tests ────────────────────────────────────

class TestNamespaceParsing:
    def test_parse_valid(self):
        result = DownstreamMCPManager.parse_namespace("chrome_devtools__list_pages")
        assert result == ("chrome_devtools", "list_pages")

    def test_parse_no_separator(self):
        result = DownstreamMCPManager.parse_namespace("list_pages")
        assert result is None

    def test_parse_empty_parts(self):
        result = DownstreamMCPManager.parse_namespace("__list_pages")
        assert result is None
