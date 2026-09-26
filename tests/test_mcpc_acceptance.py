"""Opt-in acceptance through pinned mcpc; never uses the user's mcpc profile."""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from mcp4chatgpt.oauth import AUTH_CODES, issue_token
from mcp4chatgpt.server import create_server
from test_core import make_config


pytestmark = pytest.mark.skipif(
    os.getenv("MCP_MCPC_TESTS") != "1",
    reason="set MCP_MCPC_TESTS=1 to run the pinned external MCP client",
)


def test_pinned_mcpc_http_protocol_roundtrip(tmp_path):
    binary = Path(__file__).parent / "mcp_protocol" / "node_modules" / ".bin" / "mcpc"
    if platform.system() == "Windows":
        binary = binary.with_suffix(".cmd")
    if not binary.exists():
        pytest.fail("install pinned mcpc with npm ci --prefix tests/mcp_protocol")

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    config = replace(make_config(tmp_path), allowed_roots=[allowed], computer_mode="off")
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = f"@mcp4-{os.getpid()}-{time.time_ns()}"
    mcpc_home = tmp_path / "mcpc-home"
    mcpc_home.mkdir(mode=0o700)
    (mcpc_home / "logs").mkdir(mode=0o700)
    config_path = tmp_path / "mcpc.json"
    code = f"test-{time.time_ns()}"
    AUTH_CODES[code] = {
        "client_id": "mcpc-acceptance",
        "redirect_uri": "https://test.invalid/callback",
        "created_at": time.time(),
    }
    token = issue_token(config, {"code": code, "client_id": "mcpc-acceptance"})["access_token"]
    host, port = server.server_address
    config_path.write_text(json.dumps({"mcpServers": {"sut": {
        "url": f"http://{host}:{port}/mcp",
        "headers": {"Authorization": f"Bearer {token}"},
    }}}, ensure_ascii=False), encoding="utf-8")
    config_path.chmod(0o600)
    env = {**os.environ, "MCPC_HOME_DIR": str(mcpc_home)}
    logs: list[str] = []

    def run(*args: str, success: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [str(binary), "--json", *args], cwd=tmp_path, env=env,
            text=True, capture_output=True, timeout=30,
        )
        logs.append(proc.stdout[-4000:] + proc.stderr[-4000:])
        if success and proc.returncode:
            pytest.fail(f"mcpc command failed ({proc.returncode}): {args!r}\n{logs[-1]}")
        return proc

    try:
        connected = run("connect", f"{config_path}:sut", session)
        assert connected.returncode == 0
        info = json.loads(run(session).stdout)
        assert "mcp4chatgpt" in json.dumps(info).lower(), info
        definitions = json.loads(run(session, "tools-list").stdout)
        listed_tools = definitions.get("tools") if isinstance(definitions, dict) else definitions
        assert isinstance(listed_tools, list), definitions
        names = [item["name"] for item in listed_tools]
        assert len(names) == len(set(names))
        assert "server_info" in names
        schema = json.loads(run(session, "tools-get", "server_info").stdout)
        assert schema["name"] == "server_info"
        call = json.loads(run(session, "tools-call", "server_info").stdout)
        assert call.get("isError") is not True, call
        rendered = json.dumps(call, ensure_ascii=False)
        assert "test-secret" not in rendered
        assert "mcp4chatgpt" in rendered.lower()
        resources = json.loads(run(session, "resources-list").stdout)
        listed_resources = resources.get("resources") if isinstance(resources, dict) else resources
        uris = [resource["uri"] for resource in listed_resources]
        assert "mcp4chatgpt://tools/server_info" in uris
        resource = json.loads(run(session, "resources-read", "mcp4chatgpt://tools/server_info").stdout)
        assert "server_info" in json.dumps(resource)
        unknown = run(session, "tools-call", "definitely_not_a_tool", success=False)
        assert unknown.returncode != 0
        invalid_home = tmp_path / "invalid-home"
        invalid_home.mkdir(mode=0o700)
        bad_config = tmp_path / "mcpc-bad.json"
        bad_config.write_text(json.dumps({"mcpServers": {"sut": {
            "url": f"http://{host}:{port}/mcp",
            "headers": {"Authorization": "Bearer invalid-token"},
        }}}), encoding="utf-8")
        bad_config.chmod(0o600)
        invalid = subprocess.run(
            [str(binary), "--json", "connect", f"{bad_config}:sut", session + "-bad"],
            cwd=tmp_path, env={**env, "MCPC_HOME_DIR": str(invalid_home)},
            text=True, capture_output=True, timeout=30,
        )
        assert invalid.returncode != 0
    finally:
        try:
            run("close", session, success=False)
        except Exception:
            pass
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        # The isolated state tree must not contain credentials or live bridge state.
        assert not (mcpc_home / "sessions.json").exists() or not json.loads(
            (mcpc_home / "sessions.json").read_text()
        ).get("sessions")
