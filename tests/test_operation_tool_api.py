"""The registered MCP path must preserve strict file operation semantics."""
from dataclasses import replace
import os
import subprocess
import json
import threading
import urllib.request

import pytest

from mcp4chatgpt.audit import AuditLogger
from mcp4chatgpt.tools import ToolRegistry
from mcp4chatgpt.server import create_server
from test_core import make_config


def test_registered_strict_write_replay_status_and_principal_scope(tmp_path):
    config = replace(make_config(tmp_path), tool_exposure="compact")
    target = config.allowed_roots[0] / "note.txt"
    target.write_text("before\n")
    registry = ToolRegistry(config, AuditLogger(config.audit_log))
    assert "local_strict_write_file" not in registry._all_listed_names
    assert registry._capability_get({"name": "local_strict_write_file"})["tool"]["name"] == "local_strict_write_file"
    observed = registry.call_tool("local_read_text", {"path": str(target)}, "alice")["structuredContent"]
    request = {"operation_id": "replace-1", "path": str(target), "content": "after\n",
               "expected_sha256": observed["sha256"],
               "full_replace_token": observed["full_replace_token"]}
    first = registry.call_tool("local_strict_write_file", request, "alice")["structuredContent"]
    assert first["state"] == "succeeded" and first["effect"] == "committed"
    assert target.read_text() == "after\n"
    inode = target.stat().st_ino
    replay = registry.call_tool("local_strict_write_file", request, "alice")["structuredContent"]
    assert replay["replayed"] is True and target.stat().st_ino == inode
    status = registry.call_tool("local_operation_status", {"operation_id": "replace-1",
                                                         "path": str(target)}, "alice")["structuredContent"]
    assert status["state"] == "succeeded"
    with pytest.raises(KeyError, match="operation_not_found"):
        registry.call_tool("local_operation_status", {"operation_id": "replace-1",
                                                      "path": str(target)}, "bob")


def test_status_does_not_initialize_and_patch_conflict(tmp_path):
    config = make_config(tmp_path)
    target = config.allowed_roots[0] / "note.txt"
    target.write_text("before")
    registry = ToolRegistry(config, AuditLogger(config.audit_log))
    with pytest.raises(KeyError, match="operation_not_found"):
        registry.call_tool("local_operation_status", {"operation_id": "patch-1",
                                                      "path": str(target)}, "alice")
    assert not (config.data_dir / "operations").exists()
    observed = registry.call_tool("local_read_text", {"path": str(target)}, "alice")["structuredContent"]
    request = {"operation_id": "patch-1", "path": str(target), "old": "before",
               "new": "after", "expected_sha256": observed["sha256"]}
    row = registry.call_tool("local_strict_apply_patch", request, "alice")["structuredContent"]
    assert row["state"] == "succeeded" and target.read_text() == "after"
    with pytest.raises(ValueError, match="idempotency_conflict"):
        registry.call_tool("local_strict_apply_patch", {**request, "new": "different"}, "alice")
    assert target.read_text() == "after"


def test_strict_patch_rejects_unpreserved_user_xattr(tmp_path):
    config = make_config(tmp_path)
    target = config.allowed_roots[0] / "note.txt"
    target.write_text("before")
    if hasattr(os, "setxattr"):
        os.setxattr(target, "user.mcp4chatgpt-test", b"keep")
    elif os.path.exists("/usr/bin/xattr"):
        subprocess.run(["/usr/bin/xattr", "-w", "com.mcp4chatgpt.test", "keep", str(target)],
                       check=True)
    else:
        pytest.skip("extended attributes unavailable")
    registry = ToolRegistry(config, AuditLogger(config.audit_log))
    observed = registry.call_tool("local_read_text", {"path": str(target)}, "alice")["structuredContent"]
    row = registry.call_tool("local_strict_apply_patch", {
        "operation_id": "patch-xattr", "path": str(target), "old": "before", "new": "after",
        "expected_sha256": observed["sha256"]}, "alice")["structuredContent"]
    assert row["state"] == "failed" and row["effect"] == "none"
    assert target.read_text() == "before"


def test_strict_operation_round_trip_over_http_mcp(tmp_path):
    config = replace(make_config(tmp_path), local_auth_disabled=True, tool_exposure="compact")
    target = config.allowed_roots[0] / "note.txt"
    target.write_text("before")
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}/mcp"

    def call(name, args):
        request = urllib.request.Request(endpoint, data=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args}}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    try:
        observed = call("local_read_text", {"path": str(target)})["result"]["structuredContent"]
        args = {"operation_id": "http-1", "path": str(target), "old": "before",
                "new": "after", "expected_sha256": observed["sha256"]}
        committed = call("local_strict_apply_patch", args)["result"]["structuredContent"]
        assert committed["state"] == "succeeded" and target.read_text() == "after"
        status = call("local_operation_status", {"operation_id": "http-1",
                                                  "path": str(target)})["result"]["structuredContent"]
        assert status["state"] == "succeeded"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
