from __future__ import annotations

import base64
import hashlib
import http.client
import json
import threading
import urllib.error
import urllib.request
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


def _post_http(host: str, port: int, path: str, body: bytes, content_type: str):
    conn = http.client.HTTPConnection(host, port, timeout=3)
    try:
        conn.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
            },
        )
        response = conn.getresponse()
        return response.status, dict(response.headers), response.read()
    finally:
        conn.close()


def test_http_oauth_dynamic_registration_pkce_and_bearer_roundtrip(tmp_path):
    config = make_config(tmp_path)
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address

    redirect_uri = "https://chat.openai.com/aip/callback"
    verifier = "chatgpt-oauth-pkce-verifier"
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("utf-8")).digest()
    ).decode("ascii").rstrip("=")

    try:
        registration_body = json.dumps({
            "client_name": "ChatGPT",
            "redirect_uris": [redirect_uri],
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
