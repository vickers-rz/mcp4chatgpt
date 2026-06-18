"""ext_bridge.py — local WebSocket bridge between MCP4ChatGPT and the Chrome extension.

The extension Service Worker connects to ws://127.0.0.1:<port>?token=<token>.
The MCP tool handlers call `send_command(cmd, args)` which routes a JSON-RPC
style request over the live WebSocket and awaits the response.

Design notes
------------
* One extension connection at a time (single active WebSocket).  If a second
  client connects with a valid token the old one is cleanly closed.
* Each request carries a unique ``id``; the extension must echo it in the reply.
* Timeouts default to 15 s; callers can override per-call.
* The bridge runs in a dedicated daemon thread with its own asyncio event loop
  so it does not block the threaded HTTP server.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import threading
import time
import uuid
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state (set by start_bridge)
# ---------------------------------------------------------------------------
_loop: asyncio.AbstractEventLoop | None = None
_websocket: Any | None = None          # active websocket connection
_pending: dict[str, asyncio.Future[Any]] = {}  # id → Future
_connected_at: float | None = None
_client_info: dict[str, Any] = {}
_bridge_token: str = ""
_bridge_lock = threading.Lock()

# Page-change subscriptions: callback(event_dict) → None
_change_subscribers: list[Any] = []

# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def _derive_token(auth_secret: str) -> str:
    """Derive a stable 32-hex-char bridge token from the server auth secret."""
    return hmac.new(
        auth_secret.encode("utf-8"),
        b"ext_bridge_token_v1",
        hashlib.sha256,
    ).hexdigest()


def _verify_token(token: str) -> bool:
    return hmac.compare_digest(token, _bridge_token)


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

async def _handler(websocket: Any) -> None:
    global _websocket, _connected_at, _client_info

    # --- Handshake: first message must be {"type":"auth","token":"..."} -----
    try:
        raw = await asyncio.wait_for(websocket.recv(), timeout=10)
        msg = json.loads(raw)
    except Exception as exc:
        log.warning("ext_bridge: handshake error: %s", exc)
        await websocket.close(1008, "auth required")
        return

    if msg.get("type") != "auth" or not _verify_token(str(msg.get("token", ""))):
        log.warning("ext_bridge: invalid auth token from %s", websocket.remote_address)
        await websocket.close(1008, "invalid token")
        return

    # --- Accept; close previous connection if any ---------------------------
    old = _websocket
    if old is not None:
        try:
            await old.close(1001, "replaced by new connection")
        except Exception:
            pass

    with _bridge_lock:
        _websocket = websocket
        _connected_at = time.time()
        _client_info = {
            "version": str(msg.get("version", "")),
            "platform": str(msg.get("platform", "")),
        }

    log.info("ext_bridge: extension connected (version=%s)", _client_info.get("version"))

    # Send welcome ACK
    await websocket.send(json.dumps({"type": "auth_ok", "server": "mcp4chatgpt"}))

    # --- Message loop -------------------------------------------------------
    try:
        async for raw in websocket:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")
            msg_id = data.get("id")

            if msg_type == "response" and msg_id in _pending:
                fut = _pending.pop(msg_id)
                if not fut.done():
                    error = data.get("error")
                    if error:
                        fut.set_exception(RuntimeError(str(error)))
                    else:
                        fut.set_result(data.get("result"))

            elif msg_type == "event":
                # Async event pushed by the extension (e.g. page change)
                _dispatch_event(data)

            elif msg_type == "ping":
                await websocket.send(json.dumps({"type": "pong"}))

            else:
                log.debug("ext_bridge: unhandled message type=%s", msg_type)

    except Exception as exc:
        log.info("ext_bridge: connection closed: %s", exc)
    finally:
        with _bridge_lock:
            if _websocket is websocket:
                _websocket = None
                _connected_at = None
        # Cancel all pending futures
        for fut in list(_pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("Extension disconnected"))
        _pending.clear()
        log.info("ext_bridge: extension disconnected")


def _dispatch_event(event: dict[str, Any]) -> None:
    """Forward extension-pushed events to registered Python subscribers."""
    for cb in list(_change_subscribers):
        try:
            cb(event)
        except Exception as exc:
            log.warning("ext_bridge: subscriber error: %s", exc)


# ---------------------------------------------------------------------------
# Public API — called from ext_ops.py (runs in HTTP server threads)
# ---------------------------------------------------------------------------

def is_connected() -> bool:
    return _websocket is not None


def connection_info() -> dict[str, Any]:
    with _bridge_lock:
        if _websocket is None:
            return {"connected": False}
        return {
            "connected": True,
            "connected_at": _connected_at,
            "uptime_sec": round(time.time() - (_connected_at or time.time()), 1),
            **_client_info,
        }


async def _async_send_command(
    cmd: str, args: dict[str, Any], timeout: float
) -> Any:
    ws = _websocket
    if ws is None:
        raise RuntimeError(
            "Chrome extension is not connected. "
            "Install the MCP4ChatGPT extension and ensure it shows 'Connected'."
        )
    req_id = str(uuid.uuid4())
    fut: asyncio.Future[Any] = _loop.create_future()  # type: ignore[union-attr]
    _pending[req_id] = fut
    payload = json.dumps({"type": "command", "id": req_id, "cmd": cmd, "args": args})
    try:
        await ws.send(payload)
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _pending.pop(req_id, None)
        raise RuntimeError(f"Extension command '{cmd}' timed out after {timeout}s")
    except Exception:
        _pending.pop(req_id, None)
        raise


def send_command(
    cmd: str, args: dict[str, Any] | None = None, timeout: float = 15.0
) -> Any:
    """Synchronous wrapper: schedule command on bridge loop, block until done."""
    if _loop is None:
        raise RuntimeError("ext_bridge is not running. Call start_bridge() first.")
    future = asyncio.run_coroutine_threadsafe(
        _async_send_command(cmd, args or {}, timeout), _loop
    )
    try:
        return future.result(timeout=timeout + 2)
    except TimeoutError:
        raise RuntimeError(f"Extension command '{cmd}' timed out (bridge timeout)")


def subscribe_changes(callback: Any) -> None:
    """Register a callable that receives page-change event dicts."""
    if callback not in _change_subscribers:
        _change_subscribers.append(callback)


def unsubscribe_changes(callback: Any) -> None:
    try:
        _change_subscribers.remove(callback)
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# Bridge lifecycle
# ---------------------------------------------------------------------------

def start_bridge(auth_secret: str, port: int = 8765) -> threading.Thread:
    """Start the WebSocket bridge in a background daemon thread.

    Returns the thread so callers can join it during shutdown if desired.
    """
    global _bridge_token, _loop

    _bridge_token = _derive_token(auth_secret)

    def _run() -> None:
        global _loop
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)

        try:
            import websockets  # type: ignore[import]
        except ImportError:
            log.error(
                "ext_bridge: 'websockets' package not installed. "
                "Run: pip install websockets"
            )
            return

        async def _serve() -> None:
            async with websockets.serve(
                _handler,
                "127.0.0.1",
                port,
                ping_interval=20,
                ping_timeout=20,
                max_size=50 * 1024 * 1024,  # 50 MB (for screenshots)
            ):
                log.info("ext_bridge: listening on ws://127.0.0.1:%d", port)
                await asyncio.Future()  # run forever

        _loop.run_until_complete(_serve())

    thread = threading.Thread(target=_run, name="ext-bridge", daemon=True)
    thread.start()
    return thread
