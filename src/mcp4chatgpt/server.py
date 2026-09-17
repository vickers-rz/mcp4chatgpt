from __future__ import annotations

import base64
import binascii
import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
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


def _open_webui_search(config: Config, params: dict[str, Any]) -> list[dict[str, str]]:
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
    return [
        {
            "link": str(item.get("link") or item.get("url") or ""),
            "title": str(item.get("title") or ""),
            "snippet": str(item.get("markdown") or item.get("snippet") or item.get("content") or ""),
        }
        for item in result["results"]
    ]


def _make_error(
    code: int,
    message: str,
    request_id: Any = None,
    data: Any | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _make_result(result: Any, request_id: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


_LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_MODERN_PROTOCOL_VERSION = "2026-07-28"
_PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
_CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
_CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
_SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
_CACHEABLE_MODERN_METHODS = frozenset(
    {"server/discover", "tools/list", "resources/list", "resources/read", "prompts/list"}
)
_NAMED_MODERN_METHODS = {
    "tools/call": "name",
    "resources/read": "uri",
    "prompts/get": "name",
}

_SERVER_INSTRUCTIONS = (
    "Use MCP4ChatGPT as a browser/file gateway. Before ext_* Chrome work call ext_connection_status; prefer "
    "explicit IDs and the least-privileged tool that fits. Use ext_* for the extension bridge and chrome_devtools__* for snapshots, "
    "network inspection, and interaction; use ext_run_js only when no dedicated tool fits. For non-text local "
    "files use local_expose_file, then resource_link/resources/read, instead of local RAG. Use browser_*/chrome_* "
    "only as read-only fallback when the extension is unavailable."
)


def _requested_protocol_version(handler: BaseHTTPRequestHandler) -> str:
    version = handler.headers.get("MCP-Protocol-Version", "").strip()
    return version or "2025-03-26"


def _negotiate_protocol_version(handler: BaseHTTPRequestHandler, params: dict[str, Any]) -> str:
    requested = str(params.get("protocolVersion") or _requested_protocol_version(handler))
    if requested in _LEGACY_PROTOCOL_VERSIONS:
        return requested
    # Older clients may omit the field or send a future version before falling
    # back. Prefer the newest version this minimal transport advertises.
    return _LEGACY_PROTOCOL_VERSIONS[0]


def _server_capabilities() -> dict[str, Any]:
    return {
        "tools": {"listChanged": False},
        "resources": {"subscribe": False, "listChanged": False},
        "prompts": {"listChanged": False},
    }


def _decode_mcp_header_value(value: str) -> str:
    if value.startswith("=?base64?") and value.endswith("?="):
        encoded = value[len("=?base64?") : -2]
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ValueError("malformed Base64 sentinel value") from exc
    if value != value.strip() or any(ord(char) < 0x20 or ord(char) > 0x7E for char in value):
        raise ValueError("invalid plain-ASCII header value")
    return value


def _modernize_result(method: str, result: dict[str, Any]) -> dict[str, Any]:
    modern = dict(result)
    modern["resultType"] = "complete"
    response_meta = modern.get("_meta")
    response_meta = dict(response_meta) if isinstance(response_meta, dict) else {}
    response_meta[_SERVER_INFO_META_KEY] = {"name": "mcp4chatgpt", "version": __version__}
    modern["_meta"] = response_meta
    if method in _CACHEABLE_MODERN_METHODS:
        modern["ttlMs"] = 0
        modern["cacheScope"] = "private"
    return modern


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
            if not _is_local_request(self):
                _json_response(self, 403, {"error": "local_search_only"})
                return
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
                if not _is_local_request(self):
                    _json_response(self, 403, {"error": "local_search_only"})
                    return
                query_params = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                payload = {**_read_json(self), **query_params}
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

    def _client_context(self) -> tuple[str, bool, str]:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            client_id = verify_token(self.server.config, auth.removeprefix("Bearer ").strip())
            return client_id, True, "bearer"
        if self.server.config.local_auth_disabled and _is_local_request(self):
            return "local-open-webui", False, "local_bypass"
        raise ValueError("Missing Authorization: Bearer token.")

    def _client_id(self) -> str:
        return self._client_context()[0]

    def _send_modern_error(
        self,
        status: int,
        code: int,
        message: str,
        request_id: Any,
        *,
        data: Any | None = None,
        protocol_version: str = _MODERN_PROTOCOL_VERSION,
    ) -> None:
        _mcp_json_response(
            self,
            status,
            _make_error(code, message, request_id, data),
            protocol_version,
        )

    def _validate_modern_request(
        self,
        method: Any,
        params: dict[str, Any],
        request_id: Any,
    ) -> str | None:
        request_meta = params.get("_meta")
        request_meta = request_meta if isinstance(request_meta, dict) else {}
        body_version = request_meta.get(_PROTOCOL_VERSION_META_KEY)
        header_version = self.headers.get("MCP-Protocol-Version", "").strip()
        response_version = header_version or (str(body_version) if body_version is not None else _MODERN_PROTOCOL_VERSION)

        if not header_version or not isinstance(body_version, str) or header_version != body_version:
            self._send_modern_error(
                400,
                -32020,
                "Header mismatch: MCP-Protocol-Version must match the request _meta protocol version",
                request_id,
                protocol_version=response_version,
            )
            return None

        supported = [*_LEGACY_PROTOCOL_VERSIONS]
        if self.server.config.modern_protocol_enabled:
            supported.insert(0, _MODERN_PROTOCOL_VERSION)
        if body_version != _MODERN_PROTOCOL_VERSION or not self.server.config.modern_protocol_enabled:
            self._send_modern_error(
                400,
                -32022,
                "Unsupported protocol version",
                request_id,
                data={"requested": body_version, "supported": supported},
                protocol_version=response_version,
            )
            return None

        header_method = self.headers.get("Mcp-Method", "")
        if not isinstance(method, str) or not header_method or header_method != method:
            self._send_modern_error(
                400,
                -32020,
                "Header mismatch: Mcp-Method must match the JSON-RPC method",
                request_id,
            )
            return None

        name_field = _NAMED_MODERN_METHODS.get(method)
        if name_field is not None:
            body_name = params.get(name_field)
            raw_header_name = self.headers.get("Mcp-Name", "")
            try:
                header_name = _decode_mcp_header_value(raw_header_name) if raw_header_name else ""
            except ValueError as exc:
                self._send_modern_error(400, -32020, f"Header mismatch: Mcp-Name is {exc}", request_id)
                return None
            if not isinstance(body_name, str) or not raw_header_name or header_name != body_name:
                self._send_modern_error(
                    400,
                    -32020,
                    "Header mismatch: Mcp-Name must match the request name or uri",
                    request_id,
                )
                return None

        client_capabilities = request_meta.get(_CLIENT_CAPABILITIES_META_KEY)
        if not isinstance(client_capabilities, dict):
            self._send_modern_error(
                400,
                -32602,
                f"Invalid params: {_CLIENT_CAPABILITIES_META_KEY} must be an object",
                request_id,
            )
            return None
        client_info = request_meta.get(_CLIENT_INFO_META_KEY)
        if client_info is not None and not isinstance(client_info, dict):
            self._send_modern_error(
                400,
                -32602,
                f"Invalid params: {_CLIENT_INFO_META_KEY} must be an object when provided",
                request_id,
            )
            return None
        return body_version

    def _handle_mcp(self) -> None:
        # Keep auth at the transport boundary: no JSON-RPC method is allowed
        # to run unless the bearer token has already been validated.
        protocol_version = _requested_protocol_version(self)
        modern_request = False
        try:
            client_id, descriptor_auth_required, auth_mode = self._client_context()
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
            request_meta = params.get("_meta")
            body_protocol_version = (
                request_meta.get(_PROTOCOL_VERSION_META_KEY) if isinstance(request_meta, dict) else None
            )
            header_protocol_version = self.headers.get("MCP-Protocol-Version", "").strip()
            modern_request = body_protocol_version is not None or (
                method != "initialize"
                and bool(header_protocol_version)
                and header_protocol_version not in _LEGACY_PROTOCOL_VERSIONS
            )
            if modern_request:
                validated_version = self._validate_modern_request(method, params, request_id)
                if validated_version is None:
                    return
                protocol_version = validated_version
            audit_fields: dict[str, Any] = {
                "client_id": client_id,
                "method": method,
                "auth_mode": auth_mode,
                "protocol_version": protocol_version,
            }
            if method == "tools/list":
                audit_fields.update(
                    tool_count=len(self.server.registry._listed_tool_names),
                    toolset_hash=self.server.registry.toolset_hash,
                )
            self.server.registry.audit.log("mcp_request", **audit_fields)
            if modern_request and method == "server/discover":
                result = {
                    "supportedVersions": [_MODERN_PROTOCOL_VERSION, *_LEGACY_PROTOCOL_VERSIONS],
                    "capabilities": _server_capabilities(),
                    "instructions": _SERVER_INSTRUCTIONS,
                }
            elif not modern_request and method == "initialize":
                protocol_version = _negotiate_protocol_version(self, params)
                # Minimal MCP handshake. Tool capability discovery happens via
                # tools/list so the server can keep protocol state stateless.
                result = {
                    "protocolVersion": protocol_version,
                    "capabilities": _server_capabilities(),
                    "serverInfo": {"name": "mcp4chatgpt", "version": __version__},
                    "instructions": _SERVER_INSTRUCTIONS,
                }
            elif request_id is None:
                _empty_response(self, 202, {"MCP-Protocol-Version": protocol_version})
                return
            elif method == "tools/list":
                result = self.server.registry.list_tools(auth_required=descriptor_auth_required)
            elif method == "resources/list":
                result = self.server.registry.list_tool_resources()
            elif method == "resources/read":
                result = self.server.registry.read_tool_resource(
                    str(params.get("uri", "")),
                    auth_required=descriptor_auth_required,
                )
            elif method == "prompts/list":
                result = {"prompts": []}
            elif method == "tools/call":
                result = self.server.registry.call_tool(params.get("name", ""), params.get("arguments") or {}, client_id)
            else:
                _mcp_json_response(
                    self,
                    404 if modern_request else 200,
                    _make_error(-32601, f"Method not found: {method}", request_id),
                    protocol_version,
                )
                return
            if modern_request:
                result = _modernize_result(str(method), result)
            _mcp_json_response(self, 200, _make_result(result, request_id), protocol_version)
        except Exception as exc:
            _mcp_json_response(self, 200, _make_error(-32000, str(exc), request_id), protocol_version)


def create_server(config: Config | None = None, *, downstream_manager: Any | None = None) -> MCPServer:
    config = config or load_config()
    audit = AuditLogger(
        config.audit_log,
        rotate_bytes=config.log_rotate_bytes,
        retention_days=config.log_retention_days,
    )
    registry = ToolRegistry(config, audit, downstream_manager=downstream_manager)
    server = MCPServer((config.bind_host, config.bind_port), Handler)
    server.config = config
    server.registry = registry
    return server


def main() -> None:
    import atexit
    import logging
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config = load_config()

    # ── Start downstream MCP manager (fault-tolerant) ──────────
    downstream_manager = None
    try:
        from .downstream.manager import DownstreamMCPManager
        downstream_manager = DownstreamMCPManager()
        project_root = Path(__file__).resolve().parents[2]
        downstream_manager.start_all(project_root)

        ds_status = downstream_manager.get_status()
        ds_list = ds_status.get("downstream", [])
        if ds_list:
            for ds in ds_list:
                print(
                    f"downstream  {ds['id']}: state={ds['state']} "
                    f"tools={ds.get('tool_count', 0)}"
                )
        else:
            print("downstream  no downstream MCPs configured")

        # Ensure downstream processes are cleaned up on exit
        def _stop_downstream() -> None:
            try:
                downstream_manager.stop_all(timeout=10)
            except Exception:
                pass
        atexit.register(_stop_downstream)

    except Exception as exc:
        logging.getLogger(__name__).warning(
            "downstream manager startup failed (non-fatal): %s", exc
        )
        downstream_manager = None

    server = create_server(config, downstream_manager=downstream_manager)
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
