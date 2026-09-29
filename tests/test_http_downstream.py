"""One existing /mcp instance can be reused over HTTP with target filtering."""
import threading
from dataclasses import replace

import pytest

from mcp4chatgpt.downstream.client import StdioMCPClientError
from mcp4chatgpt.downstream.http_client import HttpMCPClient
from mcp4chatgpt.downstream.manager import DownstreamMCPManager
from mcp4chatgpt.server import create_server
from test_core import make_config


def test_http_downstream_preserves_mcp_result_and_restricts_nested_target(tmp_path):
    config = replace(make_config(tmp_path), local_auth_disabled=True, tool_exposure="compact")
    target = config.allowed_roots[0] / "note.txt"
    target.write_text("browser bridge test")
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
    client = HttpMCPClient(downstream_id="remote", url=url)
    try:
        client.start()
        instance, tools = client.catalog_snapshot()
        assert "server_info" in {tool["name"] for tool in tools}
        result = client.call_tool("server_info", {}, expected_instance_id=instance)
        assert result["structuredContent"]["name"] == "mcp4chatgpt"
        with pytest.raises(StdioMCPClientError, match="capability_backend_changed"):
            client.call_tool("server_info", {}, expected_instance_id="stale")

        cfg = tmp_path / "consumer" / "downstream_mcp.toml"
        cfg.parent.mkdir()
        cfg.write_text(f'''[downstream_mcp.remote]
transport = "http"
url = "{url}"
allow_tools = ["server_info", "capability_call"]
allow_capability_targets = ["local_read_text", "local_expose_file"]
''')
        manager = DownstreamMCPManager()
        manager.start_all(cfg.parent)
        try:
            assert {tool.original_name for tool in manager.get_tools()} == {
                "server_info", "capability_call"}
            result = manager.call_tool("remote__capability_call", {
                "name": "local_read_text", "arguments": {"path": str(target)}})
            assert result["structuredContent"]["text"] == "browser bridge test"
            linked = manager.call_tool("remote__capability_call", {
                "name": "local_expose_file", "arguments": {"path": str(target)}})
            assert linked["content"][0]["type"] == "resource_link"
            with pytest.raises(StdioMCPClientError, match="capability_target_not_allowed"):
                manager.call_tool("remote__capability_call", {
                    "name": "local_run_command", "arguments": {"command": "pwd"}})
        finally:
            manager.stop_all()
    finally:
        client.stop()
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_http_backend_binding_changes_after_remote_restart(tmp_path):
    config = replace(make_config(tmp_path), local_auth_disabled=True, tool_exposure="compact")
    first = create_server(config)
    thread = threading.Thread(target=first.serve_forever, daemon=True)
    thread.start()
    port = first.server_address[1]
    client = HttpMCPClient(downstream_id="remote", url=f"http://127.0.0.1:{port}/mcp")
    try:
        client.start()
        old_instance, _tools = client.catalog_snapshot()
        first.shutdown()
        first.server_close()
        thread.join(5)
        restarted = create_server(replace(config, bind_port=port))
        second_thread = threading.Thread(target=restarted.serve_forever, daemon=True)
        second_thread.start()
        try:
            new_instance, _tools = client.catalog_snapshot()
            assert new_instance != old_instance
            with pytest.raises(StdioMCPClientError, match="capability_backend_changed"):
                client.call_tool("server_info", {}, expected_instance_id=old_instance)
        finally:
            restarted.shutdown()
            restarted.server_close()
            second_thread.join(5)
    finally:
        client.stop()
