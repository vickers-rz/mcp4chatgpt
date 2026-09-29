"""Small Streamable HTTP MCP client for an existing MCP4ChatGPT /mcp endpoint.

The transport is stateless JSON response mode. It does not start another
Extension bridge or browser sidecar. A caller must keep and pass through
backend, revision and page/tab handles from individual tool results.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
import threading
import time
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from .client import StdioMCPClientError


_PROTOCOL_VERSION = "2026-07-28"
_MAX_RESPONSE = 16 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        del request, fp, code, msg, headers, newurl
        raise StdioMCPClientError("http_downstream_redirect_refused")


class HttpMCPClient:
    def __init__(self, *, downstream_id: str, url: str, bearer_token_env: str = "",
                 call_timeout: float = 60.0):
        parsed = urlparse(url)
        if parsed.path != "/mcp" or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("http_downstream_requires_mcp_endpoint")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and
            parsed.hostname in {"127.0.0.1", "localhost", "::1"}):
            raise ValueError("http_downstream_requires_https_or_loopback")
        self._id = downstream_id
        self._url = url
        self._token_env = bearer_token_env
        self._token = ""
        self._timeout = call_timeout
        self._lock = threading.RLock()
        self._instance_id = uuid.uuid4().hex
        self._tools: list[dict] = []
        self._started_at: float | None = None
        self._transport_error: str | None = None
        self._running = False
        self._remote_instance_id = ""
        self._opener = build_opener(_NoRedirect())

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def pid(self) -> None:
        return None

    @property
    def started_at(self) -> float | None:
        return self._started_at

    @property
    def transport_error(self) -> str | None:
        return self._transport_error

    @property
    def catalog_error(self) -> None:
        return None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def tools(self) -> list[dict]:
        return self.catalog_snapshot()[1]

    def _request(self, method: str, params: dict, *, name: str = "") -> dict:
        body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method,
                "params": {**params, "_meta": {
                    "io.modelcontextprotocol/protocolVersion": _PROTOCOL_VERSION,
                    "io.modelcontextprotocol/clientInfo": {"name": "mcp4chatgpt-http-downstream", "version": "1"},
                    "io.modelcontextprotocol/clientCapabilities": {},
                }}}
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": _PROTOCOL_VERSION, "Mcp-Method": method}
        if name:
            headers["Mcp-Name"] = quote(name, safe="")
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = Request(self._url, data=json.dumps(body, ensure_ascii=False).encode(),
                          headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                data = response.read(_MAX_RESPONSE + 1)
                if len(data) > _MAX_RESPONSE:
                    raise StdioMCPClientError("http_downstream_response_too_large")
                if "application/json" not in response.headers.get("Content-Type", ""):
                    raise StdioMCPClientError("http_downstream_requires_json_response")
                payload = json.loads(data)
        except StdioMCPClientError:
            raise
        except Exception as exc:
            self._transport_error = type(exc).__name__
            self._running = False
            raise StdioMCPClientError(f"http_downstream_transport_error:{type(exc).__name__}") from exc
        if payload.get("id") != body["id"]:
            raise StdioMCPClientError("http_downstream_response_id_mismatch")
        if "error" in payload:
            error = payload["error"]
            code = error.get("code") if isinstance(error, dict) else "unknown"
            raise StdioMCPClientError(f"http_downstream_rpc_error:{code}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise StdioMCPClientError("http_downstream_result_invalid")
        return result

    def start(self) -> None:
        with self._lock:
            if self._running:
                raise StdioMCPClientError("http_downstream_already_started")
            self._token = os.environ.get(self._token_env, "") if self._token_env else ""
            if self._token_env and not self._token:
                raise StdioMCPClientError("http_downstream_bearer_env_missing")
            self._instance_id = uuid.uuid4().hex
            self._request("server/discover", {})
            self._running = True
            self._started_at = time.time()
            self.catalog_snapshot()

    def stop(self, timeout: float = 10.0) -> None:
        del timeout
        with self._lock:
            self._running = False
            self._token = ""

    def catalog_snapshot(self) -> tuple[str, list[dict]]:
        with self._lock:
            if not self._running:
                raise StdioMCPClientError("http_downstream_not_running")
            info = self._request("tools/call", {"name": "server_info", "arguments": {}},
                                 name="server_info")
            remote_id = (info.get("structuredContent") or {}).get("server_instance_id")
            if not isinstance(remote_id, str) or not remote_id:
                raise StdioMCPClientError("http_downstream_instance_identity_missing")
            self._remote_instance_id = remote_id
            result = self._request("tools/list", {})
            tools = result.get("tools")
            if not isinstance(tools, list):
                raise StdioMCPClientError("http_downstream_tools_invalid")
            digest = hashlib.sha256(json.dumps(tools, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            self._tools = deepcopy(tools)
            return f"{self._instance_id}:{self._remote_instance_id}:{digest}", deepcopy(tools)

    def call_tool(self, tool_name: str, arguments: dict, timeout: float | None = None,
                  *, expected_instance_id: str | None = None) -> dict:
        del timeout
        with self._lock:
            instance_id, tools = self.catalog_snapshot()
            if expected_instance_id is not None and expected_instance_id != instance_id:
                raise StdioMCPClientError("capability_backend_changed")
            if tool_name not in {tool.get("name") for tool in tools}:
                raise StdioMCPClientError("capability_not_found")
            return self._request("tools/call", {"name": tool_name, "arguments": arguments},
                                 name=tool_name)
