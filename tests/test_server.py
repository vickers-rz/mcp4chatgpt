from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest import mock

from mcp4chatgpt.oauth import issue_token
from mcp4chatgpt.server import create_server

from test_core import make_config


def post_json(url: str, payload: dict, token: str | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def post_raw_json(url: str, payload: object, token: str | None = None, host: str | None = None) -> tuple[int, object]:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if host:
        headers["Host"] = host
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
        finally:
            exc.close()


def post_raw_response(
    url: str,
    payload: object,
    token: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", **(extra_headers or {})}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read(), dict(exc.headers.items())
        finally:
            exc.close()


def get_json(url: str, host: str | None = None) -> tuple[int, object]:
    headers = {}
    if host:
        headers["Host"] = host
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
        finally:
            exc.close()


def get_raw_response(
    url: str,
    token: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    headers = dict(extra_headers or {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read(), dict(exc.headers.items())
        finally:
            exc.close()


class ServerTests(unittest.TestCase):
    def issue_test_token(self, config) -> str:
        from mcp4chatgpt import oauth

        client_id = "client-test"
        code = f"server-code-{time.time_ns()}"
        oauth.AUTH_CODES[code] = {"client_id": client_id, "redirect_uri": "https://example.test/cb", "created_at": time.time()}
        return issue_token(config, {"code": code, "client_id": client_id})["access_token"]

    def test_mcp_initialize_and_tools_list(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                token = self.issue_test_token(config)
                base = f"http://{host}:{port}"
                init = post_json(base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, token)
                self.assertEqual(init["result"]["serverInfo"]["name"], "mcp4chatgpt")
                tools = post_json(base + "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, token)
                names = {tool["name"] for tool in tools["result"]["tools"]}
                self.assertIn("knowledge_search", names)
                self.assertIn("web_scrape", names)
            finally:
                server.shutdown()
                server.server_close()

    def test_local_auth_bypass_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = replace(make_config(Path(d)), local_auth_disabled=True)
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                base = f"http://{host}:{port}"
                init = post_json(base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
                self.assertEqual(init["result"]["serverInfo"]["name"], "mcp4chatgpt")
            finally:
                server.shutdown()
                server.server_close()

    def test_local_auth_bypass_omits_oauth_tool_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = replace(make_config(Path(d)), local_auth_disabled=True)
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                tools = post_json(f"http://{host}:{port}/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
                first_tool = tools["result"]["tools"][0]
                self.assertNotIn("securitySchemes", first_tool)
                self.assertNotIn("securitySchemes", first_tool["_meta"])
            finally:
                server.shutdown()
                server.server_close()

    def test_mcp_streamable_http_headers_and_notification_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                token = self.issue_test_token(config)
                base = f"http://{host}:{port}"
                status, body, headers = post_raw_response(
                    base + "/mcp",
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25"},
                    },
                    token=token,
                    extra_headers={
                        "Accept": "application/json, text/event-stream",
                        "MCP-Protocol-Version": "2025-11-25",
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
                self.assertEqual(headers["MCP-Protocol-Version"], "2025-11-25")
                payload = json.loads(body.decode("utf-8"))
                self.assertEqual(payload["result"]["protocolVersion"], "2025-11-25")

                status, body, headers = post_raw_response(
                    base + "/mcp",
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    token=token,
                    extra_headers={"MCP-Protocol-Version": "2025-11-25"},
                )
                self.assertEqual(status, 202)
                self.assertEqual(body, b"")
                self.assertEqual(headers["Content-Length"], "0")
                self.assertEqual(headers["MCP-Protocol-Version"], "2025-11-25")
            finally:
                server.shutdown()
                server.server_close()

    def test_mcp_get_endpoint_declines_sse_stream_with_405(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                token = self.issue_test_token(config)
                status, body, headers = get_raw_response(
                    f"http://{host}:{port}/mcp",
                    token=token,
                    extra_headers={"Accept": "text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
                )
                self.assertEqual(status, 405)
                self.assertEqual(body, b"")
                self.assertEqual(headers["Allow"], "POST")
                self.assertEqual(headers["MCP-Protocol-Version"], "2025-11-25")
            finally:
                server.shutdown()
                server.server_close()

    def test_oauth_register_rejects_array_json_body(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                status, payload = post_raw_json(f"http://{host}:{port}/oauth/register", [])
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"], "invalid_request")
                self.assertIn("object", payload["error_description"])
            finally:
                server.shutdown()
                server.server_close()

    def test_mcp_array_json_body_returns_jsonrpc_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                from mcp4chatgpt import oauth

                client_id = "client-test"
                code = "array-body-code"
                oauth.AUTH_CODES[code] = {"client_id": client_id, "redirect_uri": "https://example.test/cb", "created_at": time.time()}
                token = issue_token(config, {"code": code, "client_id": client_id})["access_token"]
                status, payload = post_raw_json(f"http://{host}:{port}/mcp", [], token=token)
                self.assertEqual(status, 200)
                self.assertEqual(payload["error"]["code"], -32000)
                self.assertIn("object", payload["error"]["message"])
            finally:
                server.shutdown()
                server.server_close()

    def test_host_allowlist_allows_localhost_and_rejects_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                status, payload = get_json(f"http://{host}:{port}/health", host=f"127.0.0.1:{port}")
                self.assertEqual(status, 200)
                self.assertTrue(payload["ok"])
                status, payload = get_json(f"http://{host}:{port}/health", host=f"localhost:{port}")
                self.assertEqual(status, 200)
                self.assertTrue(payload["ok"])
                status, payload = get_json(f"http://{host}:{port}/health", host="evil.com")
                self.assertEqual(status, 403)
                self.assertEqual(payload["error"], "forbidden_host")
            finally:
                server.shutdown()
                server.server_close()

    def test_host_allowlist_allows_configured_host(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = replace(make_config(Path(d)), allowed_hosts=["example.test"])
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                status, payload = get_json(f"http://{host}:{port}/health", host="example.test")
                self.assertEqual(status, 200)
                self.assertTrue(payload["ok"])
            finally:
                server.shutdown()
                server.server_close()

    def test_open_webui_search_endpoint_returns_results(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                with mock.patch("mcp4chatgpt.server.web_ops.combined_search", return_value={
                    "query": "test",
                    "engine": "brave",
                    "results": [{"title": "Example", "url": "https://example.test", "link": "https://example.test", "snippet": "Snippet"}],
                    "raw": {},
                }) as combined:
                    status, payload = post_raw_json(
                        f"http://{host}:{port}/search?engine=brave",
                        {"query": "test", "count": 2},
                    )
                self.assertEqual(status, 200)
                self.assertIsInstance(payload, list)
                self.assertEqual(payload[0], {
                    "link": "https://example.test",
                    "title": "Example",
                    "snippet": "Snippet",
                })
                combined.assert_called_once_with(
                    config,
                    "test",
                    2,
                    engine="brave",
                    fetch_content=False,
                    fetch_limit=3,
                )
            finally:
                server.shutdown()
                server.server_close()

    def test_open_webui_search_rejects_public_host_before_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = replace(make_config(Path(d)), allowed_hosts=["127.0.0.1", "public.example"])
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                with mock.patch("mcp4chatgpt.server.web_ops.combined_search") as combined:
                    status, payload = post_raw_json(
                        f"http://{host}:{port}/search",
                        {"query": "test", "count": 2},
                        host="public.example",
                    )
                self.assertEqual(status, 403)
                self.assertEqual(payload["error"], "local_search_only")
                combined.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
