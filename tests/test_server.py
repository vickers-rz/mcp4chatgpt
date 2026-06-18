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


def post_raw_json(url: str, payload: object, token: str | None = None, host: str | None = None) -> tuple[int, dict]:
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


def get_json(url: str, host: str | None = None) -> tuple[int, dict]:
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


class ServerTests(unittest.TestCase):
    def test_mcp_initialize_and_tools_list(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                from mcp4chatgpt import oauth

                client_id = "client-test"
                code = "server-code"
                oauth.AUTH_CODES[code] = {"client_id": client_id, "redirect_uri": "https://example.test/cb", "created_at": time.time()}
                token = issue_token(config, {"code": code, "client_id": client_id})["access_token"]
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


if __name__ == "__main__":
    unittest.main()
