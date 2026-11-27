from __future__ import annotations

import base64
import hashlib
import http.client
import json
import threading
import urllib.error
import urllib.request
import pytest
from dataclasses import replace
from urllib.parse import parse_qs, urlencode, urlparse

from mcp4chatgpt.server import _protected_resource_metadata_url, create_server
from test_core import make_config


def _get(url: str):
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_rfc9728_path_specific_protected_resource_discovery(tmp_path):
    config = make_config(tmp_path)
    assert _protected_resource_metadata_url(config) == (
        "http://127.0.0.1:8766/.well-known/oauth-protected-resource/mcp"
    )

    prefixed = replace(config, public_base_url="https://mcp.example.test/base")
    assert _protected_resource_metadata_url(prefixed) == (
        "https://mcp.example.test/.well-known/oauth-protected-resource/base/mcp"
    )


def test_server_serves_canonical_and_legacy_resource_metadata(tmp_path):
    config = make_config(tmp_path)
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}"

    try:
        canonical = "/.well-known/oauth-protected-resource/mcp"
        status, headers, body = _get(base + canonical)
        assert status == 200
        assert headers["Content-Type"].startswith("application/json")
        payload = json.loads(body)
        assert payload["resource"] == config.mcp_url
        assert payload["authorization_servers"] == [config.public_base_url]

        status, _, legacy_body = _get(base + "/.well-known/oauth-protected-resource")
        assert status == 200
        assert json.loads(legacy_body) == payload

        status, headers, _ = _get(base + "/mcp")
        assert status == 401
        assert headers["WWW-Authenticate"] == (
            'Bearer resource_metadata="'
            + _protected_resource_metadata_url(config)
            + '"'
        )

        status, _, auth_body = _get(base + "/.well-known/oauth-authorization-server")
        assert status == 200
        auth = json.loads(auth_body)
        assert auth["issuer"] == config.public_base_url
        assert auth["registration_endpoint"] == config.public_base_url + "/oauth/register"
        assert "S256" in auth["code_challenge_methods_supported"]
    finally:
        server.shutdown()
        server.server_close()


def _post_http(
    host: str, port: int, path: str, body: bytes, content_type: str, headers: dict[str, str] | None = None,
):
    conn = http.client.HTTPConnection(host, port, timeout=3)
    try:
        conn.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                **(headers or {}),
            },
        )
        response = conn.getresponse()
        return response.status, dict(response.headers), response.read()
    finally:
        conn.close()


@pytest.mark.parametrize("auth_method", ["client_secret_post", "none"])
def test_http_oauth_dynamic_registration_pkce_and_bearer_roundtrip(tmp_path, auth_method):
    config = replace(make_config(tmp_path), static_mcp_token="a" * 64)
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address

    redirect_uri = "https://chat.openai.com/aip/callback"
    verifier = "chatgpt-oauth-pkce-verifier-" + "x" * 32
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("utf-8")).digest()
    ).decode("ascii").rstrip("=")

    try:
        registration_body = json.dumps({
            "client_name": "ChatGPT",
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": auth_method,
        }).encode("utf-8")
        status, _, body = _post_http(
            host,
            port,
            "/oauth/register",
            registration_body,
            "application/json",
        )
        assert status == 201
        client = json.loads(body)
        client_id = client["client_id"]

        authorize_body = urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "local web knowledge",
            "state": "state-test",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": config.mcp_url,
            "admin_secret": config.auth_secret,
        }).encode("utf-8")
        status, headers, _ = _post_http(
            host,
            port,
            "/oauth/authorize",
            authorize_body,
            "application/x-www-form-urlencoded",
        )
        assert status == 302
        location = headers["Location"]
        parsed_location = urlparse(location)
        values = parse_qs(parsed_location.query)
        assert values["state"] == ["state-test"]
        code = values["code"][0]

        token_body = urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            **({"client_secret": client["client_secret"]} if auth_method == "client_secret_post" else {}),
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "resource": config.mcp_url,
        }).encode("utf-8")
        status, _, body = _post_http(
            host,
            port,
            "/oauth/token",
            token_body,
            "application/x-www-form-urlencoded",
        )
        assert status == 200
        token = json.loads(body)
        assert token["token_type"] == "Bearer"
        assert token["access_token"]

        mcp_body = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "oauth-e2e-test", "version": "1"},
            },
        }).encode("utf-8")
        conn = http.client.HTTPConnection(host, port, timeout=3)
        try:
            conn.request(
                "POST",
                "/mcp",
                body=mcp_body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Authorization": "Bearer " + token["access_token"],
                },
            )
            response = conn.getresponse()
            response_body = response.read()
            assert response.status == 200
            payload = json.loads(response_body)
            assert payload["result"]["serverInfo"]["name"] == "mcp4chatgpt"
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("enabled", [True, False])
def test_static_mcp_token_query_and_header(tmp_path, enabled):
    token = "static-test-token-" + "a" * 48
    wrong = "invalid-test-token-" + "b" * 48
    config = replace(make_config(tmp_path), static_mcp_token=token if enabled else "")
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    headers = {"Accept": "application/json, text/event-stream"}
    query_path = "/mcp?mcp_token=" + token

    def call(path, method, params=None, authorization=None):
        request_headers = dict(headers)
        if authorization:
            request_headers["Authorization"] = "Bearer " + authorization
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        return _post_http(host, port, path, body.encode(), "application/json", request_headers)

    try:
        status, response_headers, _ = call("/mcp", "tools/list")
        assert status == 401
        assert "resource_metadata" in response_headers["WWW-Authenticate"]
        for path in ("/mcp?mcp_token=" + wrong, query_path + "&mcp_token=", "/mcp?mcp_token="):
            assert call(path, "tools/list")[0] == 401
        assert call(query_path, "tools/list", authorization=wrong)[0] == 401

        status, _, body = call(query_path, "initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "static-token-test", "version": "1"},
        })
        assert status == (200 if enabled else 401)
        if enabled:
            assert json.loads(body)["result"]["serverInfo"]["name"] == "mcp4chatgpt"

        status, _, body = call("/mcp?other='value'&%6Dcp_token=" + token, "tools/list")
        assert status == (200 if enabled else 401)
        if enabled:
            assert any(tool["name"] == "server_info" for tool in json.loads(body)["result"]["tools"])

        status, _, body = call("/mcp", "tools/call", {"name": "server_info"}, authorization=token)
        assert status == (200 if enabled else 401)
        if enabled:
            assert json.loads(body)["result"]["structuredContent"]["tool_catalog"]["listed_count"] > 0

        assert _get(f"http://{host}:{port}{query_path}")[0] == (405 if enabled else 401)
        static_path = "/gemini/mcp"
        status, headers_out, _ = call(static_path, "tools/list")
        assert status == 403
        assert "WWW-Authenticate" not in headers_out
        status, _, body = call(static_path + "?mcp_token=" + token, "tools/list")
        assert status == (200 if enabled else 403)
        if enabled:
            assert all("securitySchemes" not in tool for tool in json.loads(body)["result"]["tools"])
        base = f"http://{host}:{port}"
        assert _get(base + "/.well-known/oauth-protected-resource" + static_path)[0] == 404
        for discovery in ("oauth-protected-resource", "oauth-authorization-server"):
            assert _get(base + "/.well-known/" + discovery)[0] == (404 if enabled else 200)
        status, _, body = _get(base + "/.well-known/oauth-protected-resource/mcp")
        assert status == 200
        expected_issuer = config.public_base_url + ("/oauth" if enabled else "")
        assert json.loads(body)["authorization_servers"] == [expected_issuer]
        status, _, body = _get(base + "/.well-known/oauth-authorization-server" + urlparse(expected_issuer).path)
        assert status == 200
        assert json.loads(body)["issuer"] == expected_issuer
        assert json.loads(body)["token_endpoint"] == config.public_base_url + "/oauth/token"
        audit = config.audit_log.read_text()
        assert token not in audit
        assert wrong not in audit
        assert query_path not in audit
        requests = [row for line in audit.splitlines() if (row := json.loads(line))["event"] == "mcp_request"]
        assert {row["auth_mode"] for row in requests} == ({"static_query", "static_bearer"} if enabled else set())
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("clients", [
    "phone=" + "a" * 32 + ",desktop=" + "a" * 32,
    "phone=" + "a" * 32 + ",phone=" + "b" * 32,
    "phone=" + "L" * 32,
    "phone=" + "S" * 32,
    "phone=short",
])
def test_static_client_configuration_rejects_ambiguous_credentials(monkeypatch, clients):
    from mcp4chatgpt.config import load_config

    monkeypatch.setenv("MCP_STATIC_CLIENTS", clients)
    monkeypatch.setenv("MCP_STATIC_TOKEN", "L" * 32)
    monkeypatch.setenv("MCP_AUTH_SECRET", "S" * 32)
    with pytest.raises(ValueError, match="MCP_STATIC_CLIENTS"):
        load_config()


def test_named_static_clients_own_independent_tasks_after_rotation(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from mcp4chatgpt.config import load_config
    from test_coding_tasks import _init_repo

    base = make_config(tmp_path)
    repo = base.allowed_roots[0] / "repo"
    repo.mkdir()
    _init_repo(repo)
    task = {"task_id": "shared-name", "cwd": str(repo)}
    phone, desktop, replacement = ("a" * 48, "b" * 48, "c" * 48)
    monkeypatch.setenv("MCP_STATIC_TOKEN", "")

    @contextmanager
    def running(phone_token):
        monkeypatch.setenv("MCP_STATIC_CLIENTS", f"phone={phone_token},desktop={desktop}")
        config = replace(base, static_mcp_clients=load_config().static_mcp_clients, personal_full_access=True)
        server = create_server(config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def call(addr, token, name, arguments, *, query=False):
        path = "/gemini/mcp"
        headers = {"Accept": "application/json"}
        if query:
            path += "?mcp_token=" + token
        else:
            headers["Authorization"] = "Bearer " + token
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}
        status, _, response = _post_http(*addr, path, json.dumps(body).encode(), "application/json", headers)
        return status, json.loads(response)

    def success(addr, token, name, args, **kwargs):
        status, body = call(addr, token, name, args, **kwargs)
        assert status == 200
        return body["result"]["structuredContent"]

    with running(phone) as addr:
        first = success(addr, phone, "coding_task_create", {**task, "paths": ["app.txt"]})
        _, other = call(addr, desktop, "coding_task_inspect", task, query=True)
        assert "coding_task_not_found" in json.dumps(other)
        second = success(addr, desktop, "coding_task_create", {**task, "paths": ["app.txt"]}, query=True)
        assert first["worktree_id"] != second["worktree_id"]
        assert _get(f"http://{addr[0]}:{addr[1]}/.well-known/oauth-authorization-server/oauth")[0] == 200

    with running(replacement) as addr:
        assert call(addr, phone, "coding_task_inspect", task)[0] == 403
        assert success(addr, replacement, "coding_task_inspect", task, query=True)["worktree_id"] == first["worktree_id"]
        assert success(addr, desktop, "coding_task_inspect", task)["worktree_id"] == second["worktree_id"]
