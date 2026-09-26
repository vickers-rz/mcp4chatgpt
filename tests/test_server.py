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

from mcp4chatgpt import __version__
from mcp4chatgpt.oauth import issue_token
from mcp4chatgpt.server import create_server
from mcp4chatgpt.tools import build_tools

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

    @staticmethod
    def modern_params(**values) -> dict:
        return {
            **values,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1.0.0"},
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        }

    @staticmethod
    def modern_headers(method: str, name: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": method,
        }
        if name is not None:
            headers["Mcp-Name"] = name
        return headers

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
                self.assertEqual(init["result"]["serverInfo"]["version"], __version__)
                self.assertEqual(
                    init["result"]["capabilities"],
                    {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                        "prompts": {"listChanged": False},
                    },
                )
                instructions = init["result"]["instructions"]
                self.assertLessEqual(len(instructions), 512)
                self.assertLess(instructions.index("ext_connection_status"), instructions.index("ext_run_js"))
                self.assertIn("least-privileged", instructions)
                tools = post_json(base + "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, token)
                definitions = tools["result"]["tools"]
                names = [tool["name"] for tool in definitions]
                self.assertEqual(names, [tool.name for tool in build_tools()] + ["capability_search", "capability_get", "capability_call"])
                self.assertEqual(len(names), len(build_tools()) + 3)
                self.assertEqual(len(set(names)), len(build_tools()) + 3)
                for tool in definitions:
                    self.assertIn("securitySchemes", tool)
                    self.assertEqual(tool["securitySchemes"], tool["_meta"]["securitySchemes"])
                expected_browser = {
                    "browser_list_tabs",
                    "browser_current_tab",
                    "browser_get_page_text",
                    "browser_get_selection",
                    "browser_get_links",
                }
                expected_ext = {
                    "ext_connection_status",
                    "ext_list_tabs",
                    "ext_get_active_tab",
                    "ext_get_dom",
                    "ext_get_selection",
                    "ext_screenshot",
                    "ext_navigate",
                    "ext_click_element",
                    "ext_fill_input",
                    "ext_run_js",
                    "ext_listen_changes",
                }
                self.assertTrue(expected_browser <= set(names))
                self.assertTrue(expected_ext <= set(names))
                self.assertNotIn("web_search", names)
                self.assertNotIn("MCP4ChatGPT.server_info", names)

                alias_call = post_json(
                    base + "/mcp",
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "MCP4ChatGPT.server_info", "arguments": {}},
                    },
                    token,
                )
                self.assertEqual(alias_call["result"]["structuredContent"]["name"], "mcp4chatgpt")

                audit_events = [
                    json.loads(line)
                    for line in config.audit_log.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                list_event = next(event for event in audit_events if event.get("method") == "tools/list")
                self.assertEqual(list_event["auth_mode"], "bearer")
                self.assertEqual(list_event["tool_count"], len(build_tools()) + 3)
                self.assertEqual(len(list_event["toolset_hash"]), 64)
            finally:
                server.shutdown()
                server.server_close()

    def test_mcp_resources_expose_tool_definitions_for_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                token = self.issue_test_token(config)
                base = f"http://{host}:{port}"
                resources = post_json(base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": {}}, token)
                names = {resource["name"] for resource in resources["result"]["resources"]}
                self.assertIn("MCP4ChatGPT.terminal_run_command", names)

                read = post_json(
                    base + "/mcp",
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "resources/read",
                        "params": {"uri": "mcp4chatgpt://tools/terminal_run_command"},
                    },
                    token,
                )
                definition = json.loads(read["result"]["contents"][0]["text"])
                self.assertEqual(definition["name"], "terminal_run_command")
                self.assertIn("single-line", definition["description"])
                self.assertIn("securitySchemes", definition)
                self.assertEqual(definition["securitySchemes"], definition["_meta"]["securitySchemes"])
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
                definitions = tools["result"]["tools"]
                self.assertEqual(len(definitions), len(build_tools()) + 3)
                for tool in definitions:
                    self.assertNotIn("securitySchemes", tool)
                    self.assertNotIn("securitySchemes", tool["_meta"])
            finally:
                server.shutdown()
                server.server_close()

    def test_local_request_with_bearer_keeps_oauth_tool_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = replace(make_config(Path(d)), local_auth_disabled=True)
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                token = self.issue_test_token(config)
                tools = post_json(
                    f"http://{host}:{port}/mcp",
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    token,
                )
                for tool in tools["result"]["tools"]:
                    self.assertIn("securitySchemes", tool)
                    self.assertIn("securitySchemes", tool["_meta"])
                events = [json.loads(line) for line in config.audit_log.read_text(encoding="utf-8").splitlines()]
                event = next(item for item in events if item.get("method") == "tools/list")
                self.assertEqual(event["auth_mode"], "bearer")
            finally:
                server.shutdown()
                server.server_close()

    def test_oauth_discovery_advertises_tool_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                base = f"http://{host}:{port}"
                status, auth_metadata = get_json(base + "/.well-known/oauth-authorization-server")
                self.assertEqual(status, 200)
                self.assertEqual(auth_metadata["scopes_supported"], ["local", "web", "knowledge"])
                status, resource_metadata = get_json(base + "/.well-known/oauth-protected-resource")
                self.assertEqual(status, 200)
                self.assertEqual(resource_metadata["scopes_supported"], ["local", "web", "knowledge"])
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

    def test_mcp_2026_dual_era_discovery_and_toolset_contract(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                token = self.issue_test_token(config)
                base = f"http://{host}:{port}/mcp"

                legacy = post_json(
                    base,
                    {
                        "jsonrpc": "2.0",
                        "id": "legacy-init",
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"},
                    },
                    token,
                )
                self.assertEqual(legacy["result"]["protocolVersion"], "2025-06-18")
                legacy_tools = post_json(
                    base,
                    {"jsonrpc": "2.0", "id": "legacy-list", "method": "tools/list", "params": {}},
                    token,
                )["result"]

                status, body, headers = post_raw_response(
                    base,
                    {
                        "jsonrpc": "2.0",
                        "id": "discover",
                        "method": "server/discover",
                        "params": self.modern_params(),
                    },
                    token=token,
                    extra_headers=self.modern_headers("server/discover"),
                )
                self.assertEqual(status, 200)
                self.assertEqual(headers["MCP-Protocol-Version"], "2026-07-28")
                discover = json.loads(body)["result"]
                self.assertEqual(discover["resultType"], "complete")
                self.assertEqual(discover["supportedVersions"][0], "2026-07-28")
                self.assertIn("2025-06-18", discover["supportedVersions"])
                self.assertEqual(discover["ttlMs"], 0)
                self.assertEqual(discover["cacheScope"], "private")
                self.assertEqual(
                    discover["_meta"]["io.modelcontextprotocol/serverInfo"],
                    {"name": "mcp4chatgpt", "version": __version__},
                )

                status, body, _ = post_raw_response(
                    base,
                    {
                        "jsonrpc": "2.0",
                        "id": "modern-list",
                        "method": "tools/list",
                        "params": self.modern_params(),
                    },
                    token=token,
                    extra_headers=self.modern_headers("tools/list"),
                )
                self.assertEqual(status, 200)
                modern_tools = json.loads(body)["result"]
                self.assertEqual(
                    [tool["name"] for tool in modern_tools["tools"]],
                    [tool["name"] for tool in legacy_tools["tools"]],
                )
                self.assertEqual(len(modern_tools["tools"]), len(build_tools()) + 3)
                self.assertEqual(modern_tools["resultType"], "complete")
                self.assertEqual((modern_tools["ttlMs"], modern_tools["cacheScope"]), (0, "private"))

                status, body, _ = post_raw_response(
                    base,
                    {
                        "jsonrpc": "2.0",
                        "id": "modern-call",
                        "method": "tools/call",
                        "params": self.modern_params(name="server_info", arguments={}),
                    },
                    token=token,
                    extra_headers=self.modern_headers("tools/call", "server_info"),
                )
                self.assertEqual(status, 200)
                call_result = json.loads(body)["result"]
                self.assertEqual(call_result["resultType"], "complete")
                self.assertNotIn("ttlMs", call_result)
                self.assertEqual(call_result["structuredContent"]["name"], "mcp4chatgpt")

                list_events = [
                    json.loads(line)
                    for line in config.audit_log.read_text(encoding="utf-8").splitlines()
                    if line.strip() and json.loads(line).get("method") == "tools/list"
                ]
                self.assertEqual(len(list_events), 2)
                self.assertEqual({event["toolset_hash"] for event in list_events}, {server.registry.toolset_hash})
                self.assertEqual({event["auth_mode"] for event in list_events}, {"bearer"})
                self.assertEqual({event["tool_count"] for event in list_events}, {len(build_tools()) + 3})
            finally:
                server.shutdown()
                server.server_close()

    def test_mcp_2026_rejects_protocol_header_and_method_errors(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                token = self.issue_test_token(config)
                base = f"http://{host}:{port}/mcp"
                request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": self.modern_params(),
                }

                status, body, _ = post_raw_response(base, request, token=token)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"]["code"], -32020)

                status, body, _ = post_raw_response(
                    base,
                    request,
                    token=token,
                    extra_headers={"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "resources/list"},
                )
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"]["code"], -32020)

                missing_capabilities = self.modern_params()
                del missing_capabilities["_meta"]["io.modelcontextprotocol/clientCapabilities"]
                status, body, _ = post_raw_response(
                    base,
                    {**request, "params": missing_capabilities},
                    token=token,
                    extra_headers=self.modern_headers("tools/list"),
                )
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"]["code"], -32602)

                unknown_params = self.modern_params()
                unknown_params["_meta"]["io.modelcontextprotocol/protocolVersion"] = "2099-01-01"
                status, body, _ = post_raw_response(
                    base,
                    {**request, "params": unknown_params},
                    token=token,
                    extra_headers={"MCP-Protocol-Version": "2099-01-01", "Mcp-Method": "tools/list"},
                )
                self.assertEqual(status, 400)
                unsupported = json.loads(body)["error"]
                self.assertEqual(unsupported["code"], -32022)
                self.assertEqual(unsupported["data"]["requested"], "2099-01-01")
                self.assertIn("2026-07-28", unsupported["data"]["supported"])

                status, body, _ = post_raw_response(
                    base,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "example/unknown",
                        "params": self.modern_params(),
                    },
                    token=token,
                    extra_headers=self.modern_headers("example/unknown"),
                )
                self.assertEqual(status, 404)
                self.assertEqual(json.loads(body)["error"]["code"], -32601)

                status, body, _ = post_raw_response(
                    base,
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": self.modern_params(name="server_info", arguments={}),
                    },
                    token=token,
                    extra_headers=self.modern_headers("tools/call"),
                )
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"]["code"], -32020)
            finally:
                server.shutdown()
                server.server_close()

    def test_mcp_2026_rollback_switch_preserves_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = replace(make_config(Path(d)), modern_protocol_enabled=False)
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                token = self.issue_test_token(config)
                base = f"http://{host}:{port}/mcp"
                status, body, _ = post_raw_response(
                    base,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "server/discover",
                        "params": self.modern_params(),
                    },
                    token=token,
                    extra_headers=self.modern_headers("server/discover"),
                )
                self.assertEqual(status, 400)
                error = json.loads(body)["error"]
                self.assertEqual(error["code"], -32022)
                self.assertNotIn("2026-07-28", error["data"]["supported"])

                legacy = post_json(
                    base,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25"},
                    },
                    token,
                )
                self.assertEqual(legacy["result"]["protocolVersion"], "2025-11-25")
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
                        {"query": "test", "count": 2, "engine": "firecrawl"},
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
