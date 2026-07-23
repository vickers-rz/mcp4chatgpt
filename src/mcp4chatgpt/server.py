from __future__ import annotations

import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .audit import AuditLogger
from .config import Config, load_config
from .oauth import (
    create_auth_redirect,
    issue_token,
    metadata,
    protected_resource_metadata,
    register_client,
    render_authorize_form,
    verify_token,
)
from .tools import ToolRegistry
from . import ext_bridge
from . import web_ops


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _empty_response(handler: BaseHTTPRequestHandler, status: int, extra_headers: dict[str, str] | None = None) -> None:
    handler.send_response(status)
    for key, value in (extra_headers or {}).items():
        handler.send_header(key, value)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _mcp_json_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    payload: Any,
    protocol_version: str | None = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    if protocol_version:
        handler.send_header("MCP-Protocol-Version", protocol_version)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _html_response(handler: BaseHTTPRequestHandler, status: int, body: bytes) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _auth_required(handler: BaseHTTPRequestHandler, config: Config, message: str) -> None:
    body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
    handler.send_response(401)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("WWW-Authenticate", f'Bearer resource_metadata="{config.public_base_url}/.well-known/oauth-protected-resource"')
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


_MAX_REQUEST_BYTES = 8 * 1024 * 1024  # 8 MB hard cap per request


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    raw_length = handler.headers.get("Content-Length", "0")
    try:
        length = int(raw_length)
    except (ValueError, TypeError):
        length = 0
    if length <= 0:
        return {}
    if length > _MAX_REQUEST_BYTES:
        raise ValueError(f"Request body too large ({length} bytes).")
    raw = handler.rfile.read(length).decode("utf-8")
    content_type = handler.headers.get("Content-Type", "")
    if "application/x-www-form-urlencoded" in content_type:
        return {k: v[-1] for k, v in parse_qs(raw).items()}
    data = json.loads(raw or "{}")
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object.")
    return data


def _host_without_port(host: str) -> str:
    host = host.strip().lower()
    if not host:
        return ""
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host.strip("[]")
    if host.count(":") == 1:
        return host.split(":", 1)[0]
    return host


def _host_allowed(handler: BaseHTTPRequestHandler, config: Config) -> bool:
    host = _host_without_port(handler.headers.get("Host", ""))
    return host in {_host_without_port(item) for item in config.allowed_hosts}


def _is_local_request(handler: BaseHTTPRequestHandler) -> bool:
    remote = handler.client_address[0]
    host = _host_without_port(handler.headers.get("Host", ""))
    return remote in {"127.0.0.1", "::1"} and host in {"127.0.0.1", "localhost", "::1"}


def _forbidden_host(handler: BaseHTTPRequestHandler) -> None:
    _json_response(handler, 403, {"error": "forbidden_host"})


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _open_webui_search(config: Config, params: dict[str, Any]) -> dict[str, Any]:
    query = str(params.get("q") or params.get("query") or "").strip()
    if not query:
        raise ValueError("Missing search query. Use q or query.")
    limit = int(params.get("limit") or params.get("count") or 5)
    engine = str(params.get("engine") or config.open_webui_search_default_engine or "brave")
    fetch_content = _truthy(params.get("fetch") or params.get("fetch_content"))
    fetch_limit = int(params.get("fetch_limit") or 3)
    result = web_ops.combined_search(
        config,
        query,
        limit,
        engine=engine,
        fetch_content=fetch_content,
        fetch_limit=fetch_limit,
    )
    return {
        "query": query,
        "engine": result["engine"],
        "results": result["results"],
    }


def _make_error(code: int, message: str, request_id: Any = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _make_result(result: Any, request_id: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


_SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-03-26", "2024-11-05")


def _requested_protocol_version(handler: BaseHTTPRequestHandler) -> str:
    version = handler.headers.get("MCP-Protocol-Version", "").strip()
    return version or "2025-03-26"


def _negotiate_protocol_version(handler: BaseHTTPRequestHandler, params: dict[str, Any]) -> str:
    requested = str(params.get("protocolVersion") or _requested_protocol_version(handler))
    if requested in _SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    # Older clients may omit the field or send a future version before falling
    # back. Prefer the newest version this minimal transport advertises.
    return _SUPPORTED_PROTOCOL_VERSIONS[0]


class MCPServer(ThreadingHTTPServer):
    config: Config
    registry: ToolRegistry


class Handler(BaseHTTPRequestHandler):
    server: MCPServer

    def log_message(self, fmt: str, *args: Any) -> None:
        self.server.registry.audit.log("http", remote=self.client_address[0], message=fmt % args)

    def do_GET(self) -> None:
        if not _host_allowed(self, self.server.config):
            _forbidden_host(self)
            return
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            _json_response(self, 200, {"ok": True})
            return
        if parsed.path == "/.well-known/oauth-authorization-server":
            _json_response(self, 200, metadata(self.server.config))
            return
        if parsed.path == "/.well-known/oauth-protected-resource":
            _json_response(self, 200, protected_resource_metadata(self.server.config))
            return
        if parsed.path == "/oauth/authorize":
            params = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
            _html_response(self, 200, render_authorize_form(params))
            return
        if parsed.path == "/search":
            params = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
            try:
                _json_response(self, 200, _open_webui_search(self.server.config, params))
            except ValueError as exc:
                _json_response(self, 400, {"error": "invalid_request", "error_description": str(exc)})
            except Exception as exc:
                _json_response(self, 502, {"error": "search_failed", "error_description": str(exc)})
            return
        if parsed.path == "/mcp":
            try:
                self._client_id()
            except Exception as exc:
                _auth_required(self, self.server.config, str(exc))
                return
            _empty_response(
                self,
                405,
                {
                    "Allow": "POST",
                    "MCP-Protocol-Version": _requested_protocol_version(self),
                },
            )
            return
        _json_response(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:
        if not _host_allowed(self, self.server.config):
            _forbidden_host(self)
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/oauth/register":
                _json_response(self, 201, register_client(self.server.config, _read_json(self)))
                return
            if parsed.path == "/oauth/authorize":
                payload = _read_json(self)
                admin_secret = str(payload.pop("admin_secret", ""))
                redirect = create_auth_redirect(self.server.config, {k: str(v) for k, v in payload.items()}, admin_secret)
                self.send_response(302)
                self.send_header("Location", redirect)
                self.end_headers()
                return
            if parsed.path == "/oauth/token":
                _json_response(self, 200, issue_token(self.server.config, _read_json(self)))
                return
            if parsed.path == "/mcp":
                self._handle_mcp()
                return
            if parsed.path == "/search":
                payload = _read_json(self)
                try:
                    _json_response(self, 200, _open_webui_search(self.server.config, payload))
                except ValueError as exc:
                    _json_response(self, 400, {"error": "invalid_request", "error_description": str(exc)})
                except Exception as exc:
                    _json_response(self, 502, {"error": "search_failed", "error_description": str(exc)})
                return
            _json_response(self, 404, {"error": "not_found"})
        except ValueError as exc:
            # RFC 6749 §5.2: token-endpoint errors use a structured error object.
            # For non-MCP OAuth routes we surface a generic invalid_request.
            _json_response(self, 400, {"error": "invalid_request", "error_description": str(exc)})
        except Exception:
            _json_response(self, 500, {"error": "server_error", "error_description": "An internal error occurred."})

    def _client_id(self) -> str:
        if self.server.config.local_auth_disabled and _is_local_request(self):
            return "local-open-webui"
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise ValueError("Missing Authorization: Bearer token.")
        return verify_token(self.server.config, auth.removeprefix("Bearer ").strip())

    def _handle_mcp(self) -> None:
        # Keep auth at the transport boundary: no JSON-RPC method is allowed
        # to run unless the bearer token has already been validated.
        protocol_version = _requested_protocol_version(self)
        try:
            client_id = self._client_id()
        except Exception as exc:
            _auth_required(self, self.server.config, str(exc))
            return
        request_id = None
        try:
            request = _read_json(self)
            method = request.get("method")
            request_id = request.get("id")
            params = request.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("JSON-RPC params must be an object when provided.")
            self.server.registry.audit.log("mcp_request", client_id=client_id, method=method)
            if method == "initialize":
                protocol_version = _negotiate_protocol_version(self, params)
                # Minimal MCP handshake. Tool capability discovery happens via
                # tools/list so the server can keep protocol state stateless.
                result = {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mcp4chatgpt", "version": "0.1.0"},
                }
            elif request_id is None:
                _empty_response(self, 202, {"MCP-Protocol-Version": protocol_version})
                return
            elif method == "tools/list":
                result = self.server.registry.list_tools()
            elif method == "resources/list":
                # ChatGPT Apps discovery probes resources even when the app
                # is tool-only. Return an empty list rather than JSON-RPC
                # method-not-found so discovery can continue cleanly.
                result = {"resources": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            elif method == "tools/call":
                result = self.server.registry.call_tool(params.get("name", ""), params.get("arguments") or {}, client_id)
            else:
                _mcp_json_response(
                    self,
                    200,
                    _make_error(-32601, f"Method not found: {method}", request_id),
                    protocol_version,
                )
                return
            _mcp_json_response(self, 200, _make_result(result, request_id), protocol_version)
        except Exception as exc:
            _mcp_json_response(self, 200, _make_error(-32000, str(exc), request_id), protocol_version)


def create_server(config: Config | None = None) -> MCPServer:
    config = config or load_config()
    audit = AuditLogger(
        config.audit_log,
        rotate_bytes=config.log_rotate_bytes,
        retention_days=config.log_retention_days,
    )
    registry = ToolRegistry(config, audit)
    server = MCPServer((config.bind_host, config.bind_port), Handler)
    server.config = config
    server.registry = registry
    return server


def main() -> None:
    config = load_config()
    server = create_server(config)
    if config.tls_cert_path and config.tls_key_path:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(config.tls_cert_path, config.tls_key_path)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

    # Start the Chrome extension WebSocket bridge
    ext_bridge.start_bridge(
        auth_secret=config.auth_secret,
        port=config.ext_bridge_port,
    )
    token_hint = ext_bridge._derive_token(config.auth_secret)
    print(f"mcp4chatgpt listening on {config.bind_host}:{config.bind_port}")
    print(
        f"ext_bridge  listening on ws://127.0.0.1:{config.ext_bridge_port}  "
        f"(extension token: {token_hint[:8]}...)"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
