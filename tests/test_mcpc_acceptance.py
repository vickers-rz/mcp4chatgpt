"""Opt-in acceptance through pinned mcpc; never uses the user's mcpc profile."""
from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from mcp4chatgpt.oauth import AUTH_CODES, issue_token
from mcp4chatgpt.server import create_server
from mcp4chatgpt.downstream.models import DownstreamToolInfo
from test_core import make_config


pytestmark = pytest.mark.skipif(
    os.getenv("MCP_MCPC_TESTS") != "1",
    reason="set MCP_MCPC_TESTS=1 to run the pinned external MCP client",
)


class FakeDownstream:
    def get_tools(self):
        return [DownstreamToolInfo(
            "lookup", "docs__lookup", "Return a controlled multimodal fixture.",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            "docs",
        )]

    def call_tool(self, name, arguments):
        return {"content": [
            {"type": "image", "mimeType": "image/png", "data": "AA=="},
            {"type": "resource_link", "uri": "file:///controlled", "name": "fixture"},
        ], "structuredContent": {"name": name, "query": arguments["query"]}}


def _create_private_file(path: Path) -> None:
    """Create a credential-bearing file with mode 0600 before any content is written."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def _assert_mcpc_home_clean(home: Path) -> None:
    sessions = home / "sessions.json"
    if sessions.exists():
        assert not json.loads(sessions.read_text(encoding="utf-8")).get("sessions")
    bridges = home / "bridges"
    if bridges.exists():
        assert not list(bridges.glob("*.sock"))


def _assert_no_mcpc_processes(*needles: str) -> None:
    """Best-effort POSIX check that no test bridge process survived cleanup."""
    if os.name == "nt":
        return
    deadline = time.monotonic() + 2.0
    matches: list[str] = []
    while time.monotonic() < deadline:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )
        matches = [
            line for line in proc.stdout.splitlines()
            if any(needle and needle in line for needle in needles)
        ]
        if not matches:
            return
        time.sleep(0.05)
    pytest.fail("mcpc bridge process survived cleanup:\n" + "\n".join(matches[-10:]))


@pytest.mark.parametrize("exposure", ["full", "compact"])
def test_pinned_mcpc_http_protocol_roundtrip(tmp_path, exposure, request):
    _exercise_roundtrip(tmp_path, exposure, request)


class InjectedFailure(RuntimeError):
    pass


def _exercise_roundtrip(tmp_path, exposure, request, fault=None):
    def checkpoint(stage):
        if fault == stage:
            raise InjectedFailure(stage)

    binary = Path(__file__).parent / "mcp_protocol" / "node_modules" / ".bin" / "mcpc"
    if platform.system() == "Windows":
        binary = binary.with_suffix(".cmd")
    if not binary.exists():
        pytest.fail("install pinned mcpc with npm ci --prefix tests/mcp_protocol")

    cleanup_sessions: list[tuple[Path, str]] = []
    servers = []
    cleaned = False

    def cleanup():
        nonlocal cleaned
        if cleaned:
            return
        cleaned = True
        errors = []
        try:
            for home, name in reversed(cleanup_sessions):
                try:
                    subprocess.run([str(binary), "--json", "close", name], cwd=tmp_path,
                        env={**os.environ, "MCPC_HOME_DIR": str(home)},
                        capture_output=True, text=True, timeout=15)
                except Exception as exc:
                    errors.append(f"session cleanup: {type(exc).__name__}")
            # mcpc 0.7.0 stores session headers in the OS keychain when available.
            # Remove/read back only this run's unique names, never enumerate user credentials.
            if cleanup_sessions:
                module = binary.parent.parent / "@apify/mcpc/dist/lib/auth/keychain.js"
                script = """import {removeKeychainSessionHeaders, readKeychainSessionHeaders} from %s;
for (const name of JSON.parse(process.argv[1])) {
 await removeKeychainSessionHeaders(name);
 if (await readKeychainSessionHeaders(name) !== undefined) throw new Error('temporary credential remains');
}
""" % json.dumps(module.resolve().as_uri())
                for home in {h for h, _ in cleanup_sessions}:
                    names = [n for h, n in cleanup_sessions if h == home]
                    proc = subprocess.run([shutil.which('node') or 'node', '--input-type=module', '-e', script, json.dumps(names)],
                        env={**os.environ, "MCPC_HOME_DIR": str(home)}, capture_output=True, text=True, timeout=15)
                    if proc.returncode:
                        errors.append('temporary credential cleanup failed')
        finally:
            for server, thread in reversed(servers):
                if thread.is_alive():
                    server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            for filename in ('mcpc.json', 'mcpc-bad.json'):
                (tmp_path / filename).unlink(missing_ok=True)
        for home in {h for h, _ in cleanup_sessions}:
            _assert_mcpc_home_clean(home)
        _assert_no_mcpc_processes(*(n for _, n in cleanup_sessions))
        assert not errors, '; '.join(errors)

    # Registered before starting services or writing credentials, including
    # failures during setup (before the main test's try/finally).
    request.addfinalizer(cleanup)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    config = replace(make_config(tmp_path), auth_secret=secrets.token_urlsafe(32),
                     allowed_roots=[allowed], computer_mode="off", tool_exposure=exposure)
    server = create_server(config, downstream_manager=FakeDownstream())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    servers.append((server, thread))
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
    _create_private_file(config_path)
    config_path.write_text(json.dumps({"mcpServers": {"sut": {
        "url": f"http://{host}:{port}/mcp",
        "headers": {"Authorization": f"Bearer {token}"},
    }}}, ensure_ascii=False), encoding="utf-8")
    config_path.chmod(0o600)
    checkpoint("setup")
    env = {**os.environ, "MCPC_HOME_DIR": str(mcpc_home)}
    logs: list[str] = []
    cleanup_sessions.append((mcpc_home, session))

    def run(*args: str, success: bool = True, home: Path | None = None) -> subprocess.CompletedProcess[str]:
        if fault == "cli_timeout" and args == (session,):
            raise subprocess.TimeoutExpired("controlled mcpc CLI", 30)
        proc = subprocess.run(
            [str(binary), "--json", *args], cwd=tmp_path,
            env=env if home is None else {**env, "MCPC_HOME_DIR": str(home)},
            text=True, capture_output=True, timeout=30,
        )
        logs.append(proc.stdout[-4000:] + proc.stderr[-4000:])
        if success and proc.returncode:
            pytest.fail(f"mcpc command failed ({proc.returncode}): {args!r}\n{logs[-1]}")
        return proc

    try:
        connected = run("connect", f"{config_path}:sut", session)
        assert connected.returncode == 0
        checkpoint("after_connect")
        info = json.loads(run(session).stdout)
        assert "mcp4chatgpt" in json.dumps(info).lower(), info
        definitions = json.loads(run(session, "tools-list").stdout)
        listed_tools = definitions.get("tools") if isinstance(definitions, dict) else definitions
        assert isinstance(listed_tools, list), definitions
        names = [item["name"] for item in listed_tools]
        assert len(names) == len(set(names))
        # The pinned SDK strips the non-standard top-level securitySchemes,
        # while preserving its _meta mirror and every MCP definition field.
        expected_tools = server.registry.list_tools(auth_required=True)["tools"]
        assert listed_tools == [{k: v for k, v in t.items() if k != "securitySchemes"} for t in expected_tools]
        assert "server_info" in names
        assert "capability_search" in names
        if exposure == "full":
            assert "docs__lookup" in names
        if exposure == "compact":
            assert "local_read_text" not in names
        schema = json.loads(run(session, "tools-get", "server_info").stdout)
        assert schema == {k: v for k, v in server.registry.tools["server_info"].definition(auth_required=True).items() if k != "securitySchemes"}
        call = json.loads(run(session, "tools-call", "server_info").stdout)
        assert call.get("isError") is not True, call
        rendered = json.dumps(call, ensure_ascii=False)
        assert config.auth_secret not in rendered
        assert "mcp4chatgpt" in rendered.lower()
        fixture_file = allowed / "mcpc-fixture.txt"
        fixture_file.write_text("mcpc local call", encoding="utf-8")
        regular = json.loads(run(session, "tools-call", "local_read_text",
                                 json.dumps({"path": str(fixture_file)})).stdout)
        assert "mcpc local call" in json.dumps(regular)
        found = json.loads(run(session, "tools-call", "capability_search",
                               '{"query":"pdf inspect","limit":1}').stdout)
        assert "pdf_inspect" in json.dumps(found)
        downstream_found = json.loads(run(session, "tools-call", "capability_search",
                                          '{"query":"controlled multimodal"}').stdout)
        assert "docs__lookup" in json.dumps(downstream_found)
        downstream_schema = json.loads(run(session, "tools-call", "capability_get",
                                           '{"name":"docs__lookup"}').stdout)
        assert "docs__lookup" in json.dumps(downstream_schema)
        got = json.loads(run(session, "tools-call", "capability_get",
                             '{"name":"server_info"}').stdout)
        assert "server_info" in json.dumps(got)
        called = json.loads(run(session, "tools-call", "capability_call",
                                '{"name":"server_info","arguments":{}}').stdout)
        assert "tool_catalog" in json.dumps(called)
        multimedia = json.loads(run(session, "tools-call", "capability_call",
                                    '{"name":"docs__lookup","arguments":{"query":"fixture"}}').stdout)
        assert any(block.get("type") == "image" for block in multimedia["content"]), multimedia
        assert any(block.get("type") == "resource_link" for block in multimedia["content"]), multimedia
        assert multimedia["structuredContent"]["query"] == "fixture"
        resources = json.loads(run(session, "resources-list").stdout)
        listed_resources = resources.get("resources") if isinstance(resources, dict) else resources
        uris = [resource["uri"] for resource in listed_resources]
        assert set(uris) == {r["uri"] for r in server.registry.list_tool_resources()["resources"]}
        resource = json.loads(run(session, "resources-read", "mcp4chatgpt://tools/server_info").stdout)
        assert json.loads(resource["contents"][0]["text"]) == server.registry.tools["server_info"].definition(auth_required=True)
        unknown = run(session, "tools-call", "definitely_not_a_tool", success=False)
        assert unknown.returncode != 0
        invalid_home = tmp_path / "invalid-home"
        invalid_home.mkdir(mode=0o700)
        (invalid_home / "logs").mkdir(mode=0o700)
        bad_config = tmp_path / "mcpc-bad.json"
        _create_private_file(bad_config)
        bad_config.write_text(json.dumps({"mcpServers": {"sut": {
            "url": f"http://{host}:{port}/mcp",
            "headers": {"Authorization": "Bearer invalid-token"},
        }}}), encoding="utf-8")
        bad_config.chmod(0o600)
        bad_session = session + "-bad"
        cleanup_sessions.append((invalid_home, bad_session))
        invalid = run("connect", f"{bad_config}:sut", bad_session, success=False, home=invalid_home)
        assert invalid.returncode != 0
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        old = run("--timeout", "2", session, "tools-call", "server_info", success=False)
        assert old.returncode != 0
        restarted_config = replace(config, bind_port=port)
        server = create_server(restarted_config, downstream_manager=FakeDownstream())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        servers.append((server, thread))
        thread.start()
        restarted = session + "-restart"
        cleanup_sessions.append((mcpc_home, restarted))
        run("connect", f"{config_path}:sut", restarted)
        checkpoint("after_restart")
        assert "mcp4chatgpt" in json.dumps(json.loads(run(restarted).stdout)).lower()
        assert "mcp4chatgpt" in json.dumps(json.loads(run(restarted, "tools-call", "server_info").stdout)).lower()
        run("close", restarted)
        run("close", session, success=False)
    finally:
        cleanup()


@pytest.mark.parametrize("fault", ["setup", "after_connect", "after_restart", "cli_timeout"])
def test_mcpc_failure_paths_cleanup(tmp_path, fault):
    callbacks = []
    class Finalizers:
        def addfinalizer(self, callback):
            callbacks.append(callback)
    try:
        with pytest.raises((InjectedFailure, subprocess.TimeoutExpired)):
            _exercise_roundtrip(tmp_path, "compact", Finalizers(), fault=fault)
    finally:
        for callback in reversed(callbacks):
            callback()
    assert not (tmp_path / "mcpc.json").exists()
    assert not (tmp_path / "mcpc-bad.json").exists()
