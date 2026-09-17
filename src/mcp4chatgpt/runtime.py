from __future__ import annotations

"""Production runtime wrapper for MCP4ChatGPT.

This keeps the HTTP/MCP protocol implementation in ``server.py`` while owning
optional downstream MCP child processes with deterministic shutdown semantics.
It also enriches ``/health`` with independent Chrome Extension and downstream
status without coupling the protocol handler itself to downstream internals.
"""

import atexit
import json
import logging
import os
import ssl
from pathlib import Path
from urllib.parse import urlparse

from . import ext_bridge
from .config import load_config
from .downstream.manager import DownstreamMCPManager
from .server import Handler, create_server


class RuntimeHandler(Handler):
    """Adds runtime observability while preserving all existing HTTP routes."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if urlparse(self.path).path != "/health":
            super().do_GET()
            return

        payload: dict[str, object] = {
            "ok": True,
            "chrome_extension": ext_bridge.connection_info(),
        }
        manager = getattr(self.server, "downstream_manager", None)
        if manager is not None:
            payload.update(manager.get_status())
        else:
            payload["downstream"] = []

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    os.environ.setdefault("MCP_EXT_ASYNC_JOBS_ENABLED", "1")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config = load_config()
    project_root = Path(__file__).resolve().parents[2]
    manager = DownstreamMCPManager()
    try:
        manager.start_all(project_root)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "downstream manager startup failed (non-fatal): %s", exc
        )

    stopped = False

    def stop_downstream() -> None:
        nonlocal stopped
        if stopped:
            return
        stopped = True
        try:
            manager.stop_all(timeout=10)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "downstream manager shutdown failed: %s", exc
            )

    atexit.register(stop_downstream)

    server = create_server(config, downstream_manager=manager)
    server.RequestHandlerClass = RuntimeHandler
    server.downstream_manager = manager

    if config.tls_cert_path and config.tls_key_path:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(config.tls_cert_path, config.tls_key_path)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

    ext_bridge.start_bridge(auth_secret=config.auth_secret, port=config.ext_bridge_port)

    try:
        server.serve_forever()
    finally:
        try:
            server.server_close()
        finally:
            stop_downstream()


if __name__ == "__main__":
    main()
