"""OpenAI Codex/ChatGPT Computer Use adapter.

This module talks to Codex's effective cua_repl MCP transport.  The OpenAI
SkyComputerUseClient is host-authenticated and is not a generic standalone MCP
server, so MCP4ChatGPT binds to the same transport Codex/ChatGPT already uses.
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
import platform
import re
import selectors
import shutil
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import computer_backend

IO_TIMEOUT = 35.0
MAX_LINE = 16 * 1024 * 1024
SNAPSHOT_TTL = 45.0
MAX_SNAPSHOTS = 64

_lock = threading.RLock()
_process: subprocess.Popen[bytes] | None = None
_read_buffer = bytearray()
_request_seq = 0
_session_id = ""
_snapshots: dict[str, dict[str, Any]] = {}
_active_deadline: float | None = None


def _remaining_timeout(requested: float) -> float:
    if _active_deadline is None:
        return requested
    return max(0.0, min(requested, _active_deadline - time.monotonic()))

_CUA_OPERATIONS = {
    "list_apps", "get_state", "screenshot", "launch_app",
    "click", "press_key", "type_text", "type_keyboard",
}
_SNAPSHOT_OPERATIONS = {"click", "press_key", "type_text", "type_keyboard"}
_EXPECTED_APPROVAL_TOOLS = {
    "list_apps": {"list_apps"},
    "get_state": {"get_app_state"},
    "screenshot": {"get_app_state", "get_app_screenshot", "screenshot"},
    "launch_app": {"launch_app", "get_app_state"},
    "click": {"get_app_state", "click"},
    "press_key": {"get_app_state", "press_key"},
    "type_text": {"get_app_state", "set_value"},
    "type_keyboard": {"get_app_state", "paste", "type_text"},
}
_AX_LINE = re.compile(r"^(\t*)(\d+)\s+(.+)$")
_VALUE = re.compile(r"(?:^|, |[)] )Value:\s*(.*?)(?=, (?:ID|Secondary Actions|URL|Description|Help):|$)")
_DESCRIPTION = re.compile(r"(?:^|, )Description:\s*(.*?)(?=, (?:Help|Value|Secondary Actions|URL|ID):|$)")
_RESULT_MARKER = "__MCP4_CUA_RESULT__"


class CUABackendError(RuntimeError):
    def __init__(self, code: str, effect: str = "not_started", *, fallback_allowed: bool = False) -> None:
        self.code = code
        self.effect = effect
        self.fallback_allowed = fallback_allowed
        super().__init__(code)


ComputerCUABackendError = CUABackendError


def is_cua_snapshot(value: Any) -> bool:
    return isinstance(value, str) and (
        value.startswith("cua-snapshot:") or value.startswith("cua:")
    )


def is_cua_window(value: Any) -> bool:
    return isinstance(value, str) and (
        value.startswith("cua-window:") or value.startswith("cua-app:")
    )


def supports(operation: str, args: dict[str, Any]) -> bool:
    if operation not in _CUA_OPERATIONS:
        return False
    # The first CUA integration has no PID fence.  Exact-PID requests stay native.
    if args.get("pid") is not None:
        return False
    if operation in _SNAPSHOT_OPERATIONS:
        return is_cua_snapshot(args.get("snapshot_id"))
    if operation == "screenshot":
        window_id = args.get("window_id")
        return isinstance(window_id, str) and (
            is_cua_window(window_id) or window_id.isdigit()
        )
    if operation == "get_state":
        window_id = args.get("window_id")
        return window_id is None or (
            isinstance(window_id, str)
            and (is_cua_window(window_id) or window_id.isdigit())
        )
    return True


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _next_id() -> int:
    global _request_seq
    _request_seq += 1
    return _request_seq


def _codex_binary() -> str:
    explicit = (
        os.environ.get("MCP_CUA_CODEX_BINARY", "").strip()
        or os.environ.get("MCP_CODEX_BINARY", "").strip()
    )
    candidates = [
        explicit,
        shutil.which("codex") or "",
        str(Path.home() / ".codex" / "plugins" / ".plugin-appserver" / "codex"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise CUABackendError("cua_codex_unavailable", fallback_allowed=True)


def _effective_transport() -> tuple[list[str], dict[str, str], str]:
    codex = _codex_binary()
    discovery_timeout = _remaining_timeout(15)
    if discovery_timeout <= 0:
        raise CUABackendError("cua_timeout", fallback_allowed=True)
    try:
        completed = subprocess.run(
            [codex, "mcp", "get", "cua_repl", "--json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=discovery_timeout,
        )
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise CUABackendError("cua_transport_unavailable", fallback_allowed=True) from exc

    transport = payload.get("transport") if isinstance(payload, dict) else None
    if not isinstance(transport, dict) or transport.get("type") != "stdio":
        raise CUABackendError("cua_transport_unavailable", fallback_allowed=True)
    command = transport.get("command")
    raw_args = transport.get("args") or []
    if not isinstance(command, str) or not command or not isinstance(raw_args, list):
        raise CUABackendError("cua_transport_unavailable", fallback_allowed=True)

    env = os.environ.copy()
    for bucket in ("env", "env_vars"):
        values = transport.get(bucket) or {}
        if isinstance(values, dict):
            for key, value in values.items():
                if value is not None:
                    env[str(key)] = str(value)
    cwd = transport.get("cwd")
    return [command, *(str(value) for value in raw_args)], env, str(cwd or Path.home())


def _send(message: dict[str, Any], timeout: float = IO_TIMEOUT) -> None:
    process = _process
    if process is None or process.poll() is not None or process.stdin is None:
        raise CUABackendError("cua_not_running", fallback_allowed=True)
    try:
        payload = (_json(message) + "\n").encode("utf-8")
        view = memoryview(payload)
        selector = selectors.DefaultSelector()
        selector.register(process.stdin, selectors.EVENT_WRITE)
        deadline = time.monotonic() + _remaining_timeout(timeout)
        sent = 0
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                selector.close()
                raise CUABackendError("cua_timeout", "outcome_unknown" if sent else "not_started", fallback_allowed=sent == 0)
            written = os.write(process.stdin.fileno(), view)
            sent += written
            view = view[written:]
        selector.close()
    except CUABackendError:
        raise
    except (BrokenPipeError, OSError) as exc:
        raise CUABackendError("cua_disconnected", "outcome_unknown" if locals().get("sent", 0) else "not_started", fallback_allowed=not locals().get("sent", 0)) from exc


def _read_message(timeout: float) -> dict[str, Any]:
    process = _process
    if process is None or process.poll() is not None or process.stdout is None:
        raise CUABackendError("cua_not_running", fallback_allowed=True)

    global _read_buffer
    deadline = time.monotonic() + max(0, timeout)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while b"\n" not in _read_buffer:
            if len(_read_buffer) > MAX_LINE:
                raise CUABackendError("cua_protocol_error")
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise CUABackendError("cua_timeout", fallback_allowed=True)
            chunk = os.read(process.stdout.fileno(), min(65536, MAX_LINE + 1 - len(_read_buffer)))
            if not chunk:
                raise CUABackendError("cua_disconnected", fallback_allowed=True)
            _read_buffer.extend(chunk)
    finally:
        selector.close()
    line, _, tail = _read_buffer.partition(b"\n")
    _read_buffer = bytearray(tail)
    if len(line) > MAX_LINE:
        raise CUABackendError("cua_protocol_error")
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CUABackendError("cua_protocol_error") from exc
    if not isinstance(value, dict):
        raise CUABackendError("cua_protocol_error")
    return value


def _approval_allowed(operation: str, params: dict[str, Any], args: dict[str, Any]) -> bool:
    meta = params.get("_meta") or {}
    tool_name = meta.get("tool_name") if isinstance(meta, dict) else None
    if not isinstance(tool_name, str) or tool_name not in _EXPECTED_APPROVAL_TOOLS.get(operation, set()):
        return False

    allowed_apps = args.get("allowed_apps") or []
    app_id = args.get("app_id")
    if app_id is None or "*" in allowed_apps:
        return True
    # The OpenAI prompt normally names the application rather than exposing a
    # structured bundle id.  The router already validated app_id against the
    # allowlist, so expected CUA tool + validated app is sufficient here.
    return isinstance(app_id, str) and app_id in allowed_apps


def _wait_response(
    request_id: int,
    *,
    operation: str,
    args: dict[str, Any],
    timeout: float,
    effectful: bool,
) -> dict[str, Any]:
    end = time.monotonic() + _remaining_timeout(timeout)
    if _active_deadline is not None:
        end = min(end, _active_deadline)
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise CUABackendError(
                "cua_timeout",
                "outcome_unknown" if effectful else "not_started",
                fallback_allowed=not effectful,
            )
        try:
            message = _read_message(remaining)
        except CUABackendError as exc:
            if effectful and exc.code in {"cua_timeout", "cua_disconnected", "cua_not_running", "cua_protocol_error"}:
                raise CUABackendError(exc.code, "outcome_unknown", fallback_allowed=False) from exc
            raise

        if message.get("id") == request_id and ("result" in message or "error" in message):
            return message

        # cua_repl can request MCP elicitation approval for the underlying Sky
        # operation.  Accept only the operation-specific tool names expected for
        # the already validated MCP4ChatGPT request.
        if "id" in message and message.get("method") == "elicitation/create":
            params = message.get("params") or {}
            allowed = isinstance(params, dict) and _approval_allowed(operation, params, args)
            _send({
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "action": "accept" if allowed else "decline",
                    "content": {},
                },
            }, timeout=max(0, remaining))
            if not allowed:
                raise CUABackendError("cua_approval_required", fallback_allowed=False)
        elif "id" in message and isinstance(message.get("method"), str):
            _send({
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "unsupported client request"},
            })


def _ensure_started() -> None:
    global _process, _session_id, _read_buffer
    if platform.system() != "Darwin":
        raise CUABackendError("cua_unsupported_platform", fallback_allowed=True)
    if _process is not None and _process.poll() is None:
        return

    command, env, cwd = _effective_transport()
    try:
        _process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=False,
            bufsize=0,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
        os.set_blocking(_process.stdout.fileno(), False)
        os.set_blocking(_process.stdin.fileno(), False)
        _read_buffer.clear()
    except OSError as exc:
        _process = None
        raise CUABackendError("cua_start_failed", fallback_allowed=True) from exc

    request_id = _next_id()
    _send({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"elicitation": {}},
            "clientInfo": {"name": "MCP4ChatGPT", "version": "1"},
        },
    })
    response = _wait_response(
        request_id,
        operation="list_apps",
        args={"allowed_apps": ["*"]},
        timeout=15,
        effectful=False,
    )
    if "error" in response or not isinstance(response.get("result"), dict):
        stop()
        raise CUABackendError("cua_initialize_failed", fallback_allowed=True)
    _send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    _session_id = "mcp4chatgpt-" + uuid.uuid4().hex


def stop() -> None:
    global _process, _session_id, _read_buffer
    with _lock:
        process = _process
        _process = None
        _session_id = ""
        _read_buffer.clear()
        _snapshots.clear()
        if process is None:
            return
        try:
            os.killpg(process.pid, 15)
            process.wait(timeout=2)
        except Exception:
            try:
                os.killpg(process.pid, 9)
                process.wait(timeout=1)
            except Exception:
                try:
                    process.terminate()
                    process.wait(timeout=1)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass


atexit.register(stop)


def _extract_marked_result(result: dict[str, Any]) -> Any:
    for item in reversed(result.get("content") or []):
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        index = text.rfind(_RESULT_MARKER)
        if index < 0:
            continue
        payload = text[index + len(_RESULT_MARKER):].strip()
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            continue
    raise CUABackendError("cua_protocol_error")


def _map_tool_error(result: dict[str, Any], *, effectful: bool) -> CUABackendError:
    # An upstream error may be raised during post-action observation or cleanup.
    # Its wording cannot prove that a dispatched write never happened.
    if effectful:
        return CUABackendError("cua_operation_failed", "outcome_unknown")
    texts = "\n".join(
        str(item.get("text", ""))
        for item in (result.get("content") or [])
        if isinstance(item, dict) and item.get("type") == "text"
    )
    lowered = texts.lower()
    if "__mcp4_app_not_running__" in lowered:
        return CUABackendError("app_not_running", fallback_allowed=True)
    if "approval" in lowered or "permission request" in lowered or "declin" in lowered:
        return CUABackendError("cua_approval_required", fallback_allowed=False)
    return CUABackendError(
        "cua_operation_failed",
        "outcome_unknown" if effectful else "not_started",
        fallback_allowed=not effectful,
    )


def _call_js(
    operation: str,
    args: dict[str, Any],
    body: str,
    *,
    effectful: bool = False,
) -> Any:
    with _lock:
        deadline = time.monotonic() + IO_TIMEOUT
        _ensure_started()
        request_id = _next_id()
        code = (
            "const __mcp4_result=await (async()=>{" + body + "})();"
            "nodeRepl.write(" + _json(_RESULT_MARKER) + "+JSON.stringify(__mcp4_result));"
        )
        _send({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": "js",
                "arguments": {
                    "code": code,
                    "title": "MCP4ChatGPT Computer Use",
                    "timeout_ms": 30000,
                },
                "_meta": {
                    "x-codex-turn-metadata": {
                        "session_id": _session_id,
                        "turn_id": "mcp4chatgpt-" + uuid.uuid4().hex,
                        "model": "gpt-5.6-sol",
                    }
                },
            },
        }, timeout=max(0, deadline - time.monotonic()))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CUABackendError("cua_timeout", "outcome_unknown" if effectful else "not_started", fallback_allowed=not effectful)
        response = _wait_response(
            request_id,
            operation=operation,
            args=args,
            timeout=remaining,
            effectful=effectful,
        )
        if "error" in response:
            raise CUABackendError(
                "cua_protocol_error",
                "outcome_unknown" if effectful else "not_started",
                fallback_allowed=not effectful,
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise CUABackendError("cua_protocol_error", "outcome_unknown" if effectful else "not_started")
        if result.get("isError") is True:
            raise _map_tool_error(result, effectful=effectful)
        try:
            return _extract_marked_result(result)
        except CUABackendError as exc:
            if effectful:
                raise CUABackendError(exc.code, "outcome_unknown") from exc
            raise


def _running_guard(app_id: str) -> str:
    # getApp() launches a missing macOS app.  Observations must not do that, so
    # check inventory first and deliberately fail before getApp when not running.
    return (
        "const __inventory=await cua.listApps({emit:false});"
        "const __running=__inventory.find(x=>x&&x.id===" + _json(app_id) + "&&x.isRunning);"
        "if(!__running){throw new Error('__MCP4_APP_NOT_RUNNING__');}"
    )


def _window_title(state: str) -> str:
    match = re.search(r'^Window:\s*"([^"]*)"', state)
    return match.group(1) if match else ""


def _window_id(app_id: str, pid: int, native_window_id: str) -> str:
    """Bind a public CUA token to one exact native helper window identity."""
    digest = hashlib.sha256(
        (app_id + "\0" + str(pid) + "\0" + native_window_id).encode("utf-8")
    ).hexdigest()[:24]
    return "cua-window:" + digest


def _native_call(
    operation: str,
    args: dict[str, Any],
    *,
    effectful: bool = False,
) -> dict[str, Any]:
    attempts = 2 if operation == "list_windows" and not effectful else 1
    for attempt in range(attempts):
        try:
            return computer_backend.call(operation, args, effectful=effectful)
        except computer_backend.ComputerBackendError as exc:
            if (
                attempt == 0
                and attempts == 2
                and exc.code == "app_not_running"
                and exc.effect == "not_started"
            ):
                time.sleep(0.03)
                continue
            raise CUABackendError(
                "cua_window_" + exc.code,
                exc.effect,
                fallback_allowed=(not effectful and exc.effect == "not_started"),
            ) from exc
    raise CUABackendError("cua_window_inventory_unavailable")


def _window_inventory(
    args: dict[str, Any],
    app_id: str,
) -> tuple[int, list[dict[str, Any]], bool] | dict[str, Any]:
    native_args: dict[str, Any] = {
        "app_id": app_id,
        "allowed_apps": list(args.get("allowed_apps") or []),
    }
    if args.get("pid") is not None:
        native_args["pid"] = int(args["pid"])

    result = _native_call("list_windows", native_args)
    if result.get("status") == "process_selection_required":
        selection = dict(result)
        selection["backend"] = "cua"
        selection["background_capable"] = True
        return selection
    if result.get("status") != "ok":
        raise CUABackendError("cua_window_inventory_invalid")

    pid = result.get("pid")
    raw_windows = result.get("windows")
    if not isinstance(pid, int) or not isinstance(raw_windows, list):
        raise CUABackendError("cua_window_inventory_invalid")

    bindings: list[dict[str, Any]] = []
    for raw in raw_windows:
        if not isinstance(raw, dict):
            continue
        native_window_id = raw.get("window_id")
        if (
            not isinstance(native_window_id, str)
            or not native_window_id.startswith("native-window:")
        ):
            continue
        window_number = raw.get("window_number")
        identity = (
            f"cg-window:{window_number}"
            if isinstance(window_number, int)
            else native_window_id
        )
        binding: dict[str, Any] = {
            "window_id": _window_id(app_id, pid, identity),
            "native_window_id": native_window_id,
            "title": str(raw.get("title") or ""),
            "minimized": bool(raw.get("minimized", False)),
            "focused": bool(raw.get("focused", False)),
            "main": bool(raw.get("main", False)),
        }
        if isinstance(window_number, int):
            binding["window_number"] = window_number
        if isinstance(raw.get("on_screen"), bool):
            binding["on_screen"] = raw["on_screen"]
        if isinstance(raw.get("bounds"), dict):
            binding["bounds"] = dict(raw["bounds"])
        bindings.append(binding)

    return pid, bindings, bool(result.get("truncated", False))


def _public_windows(bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = (
        "window_id",
        "window_number",
        "title",
        "minimized",
        "focused",
        "main",
        "on_screen",
        "bounds",
    )
    return [
        {key: item[key] for key in keys if key in item}
        for item in bindings
    ]


def _resolve_window(
    args: dict[str, Any],
    app_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], int, bool] | dict[str, Any]:
    inventory = _window_inventory(args, app_id)
    if isinstance(inventory, dict):
        return inventory
    pid, bindings, truncated = inventory
    if not bindings:
        raise CUABackendError("no_window")

    requested = args.get("window_id")
    if requested is None:
        if len(bindings) > 1:
            return {
                "status": "window_selection_required",
                "app_id": app_id,
                "pid": pid,
                "windows": _public_windows(bindings),
                "truncated": truncated,
                "backend": "cua",
                "background_capable": True,
            }
        return bindings[0], bindings, pid, truncated

    if not isinstance(requested, str):
        raise CUABackendError("stale_window")
    for binding in bindings:
        if binding["window_id"] == requested:
            return binding, bindings, pid, truncated
    raise CUABackendError("stale_window")


def _select_window_background(
    args: dict[str, Any],
    app_id: str,
    binding: dict[str, Any],
    pid: int,
) -> None:
    if binding.get("minimized"):
        raise CUABackendError("window_not_visible")
    if binding.get("focused") and binding.get("main"):
        return
    result = _native_call(
        "select_window_background",
        {
            "app_id": app_id,
            "pid": pid,
            "window_id": binding["native_window_id"],
            "allowed_apps": list(args.get("allowed_apps") or []),
        },
        effectful=True,
    )
    if (
        result.get("success") is not True
        or result.get("window_id") != binding["native_window_id"]
    ):
        raise CUABackendError(
            "cua_window_selection_unverified",
            "outcome_unknown",
        )


def _verify_window_binding(
    args: dict[str, Any],
    app_id: str,
    *,
    native_window_id: str,
    expected_window_id: str,
    expected_pid: int | None = None,
) -> tuple[int, list[dict[str, Any]], bool]:
    verify_args = dict(args)
    if expected_pid is not None:
        verify_args["pid"] = expected_pid
    inventory = _window_inventory(verify_args, app_id)
    if isinstance(inventory, dict):
        raise CUABackendError("stale_window")
    pid, bindings, truncated = inventory
    match = next(
        (
            item
            for item in bindings
            if item["native_window_id"] == native_window_id
        ),
        None,
    )
    if (
        not isinstance(match, dict)
        or match["window_id"] != expected_window_id
        or not (match.get("focused") or match.get("main"))
    ):
        raise CUABackendError("stale_window")
    return pid, bindings, truncated


def _prune_snapshots() -> None:
    now = time.monotonic()
    for token, snap in list(_snapshots.items()):
        if now - float(snap.get("created", 0)) >= SNAPSHOT_TTL:
            _snapshots.pop(token, None)
    if len(_snapshots) > MAX_SNAPSHOTS:
        oldest = sorted(_snapshots.items(), key=lambda item: float(item[1].get("created", 0)))
        for token, _ in oldest[:len(_snapshots) - MAX_SNAPSHOTS]:
            _snapshots.pop(token, None)


def _invalidate_app_snapshots(app_id: str) -> None:
    for token, snap in list(_snapshots.items()):
        if snap.get("app_id") == app_id:
            _snapshots.pop(token, None)


def _new_snapshot(
    app_id: str,
    window_id: str,
    elements: dict[str, int],
    binding: str,
    *,
    native_window_id: str | None = None,
    pid: int | None = None,
    token: str | None = None,
) -> str:
    _prune_snapshots()
    _invalidate_app_snapshots(app_id)
    token = token or ("cua-snapshot:" + uuid.uuid4().hex)
    _snapshots[token] = {
        "app_id": app_id,
        "window_id": window_id,
        "native_window_id": native_window_id,
        "pid": pid,
        "created": time.monotonic(),
        "elements": dict(elements),
        "binding": binding,
    }
    return token


def _snapshot(args: dict[str, Any]) -> dict[str, Any]:
    token = args.get("snapshot_id")
    if not isinstance(token, str):
        raise CUABackendError("stale_snapshot")
    _prune_snapshots()
    snap = _snapshots.get(token)
    if not isinstance(snap, dict):
        raise CUABackendError("stale_snapshot")
    if snap.get("app_id") != args.get("app_id") or snap.get("window_id") != args.get("window_id"):
        raise CUABackendError("stale_snapshot")
    return snap


def _spend_snapshot(args: dict[str, Any]) -> None:
    token = args.get("snapshot_id")
    if isinstance(token, str):
        _snapshots.pop(token, None)


def _parse_elements(state: str, snapshot_id: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    elements: list[dict[str, Any]] = []
    mapping: dict[str, int] = {}
    for raw in state.splitlines():
        match = _AX_LINE.match(raw)
        if not match:
            continue
        tabs, index_text, role = match.groups()
        index = int(index_text)
        element_id = f"cua-element:{snapshot_id}:{index}"
        item: dict[str, Any] = {
            "element_id": element_id,
            "depth": len(tabs),
            "cua_index": index,
            "role": role,
        }
        value = _VALUE.search(role)
        if value:
            item["value"] = value.group(1)
        description = _DESCRIPTION.search(role)
        if description:
            item["description"] = description.group(1)
        if "(disabled" in role or " disabled" in role:
            item["enabled"] = False
        if "settable" in role:
            item["settable"] = True
        elements.append(item)
        mapping[element_id] = index
    return elements, mapping


def _image_metadata(encoded: str) -> tuple[str, tuple[int, int] | None]:
    try:
        data = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise CUABackendError("cua_protocol_error") from exc

    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return "image/png", (width, height)

    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", None

    if data.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            offset += 2
            if marker in {0xD8, 0xD9}:
                continue
            if offset + 2 > len(data):
                break
            length = int.from_bytes(data[offset:offset + 2], "big")
            if length < 2 or offset + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and length >= 7:
                height = int.from_bytes(data[offset + 3:offset + 5], "big")
                width = int.from_bytes(data[offset + 5:offset + 7], "big")
                return "image/jpeg", (width, height)
            offset += length
        return "image/jpeg", None

    raise CUABackendError("cua_protocol_error")


def _call_locked(operation: str, args: dict[str, Any], *, effectful: bool = False) -> dict[str, Any]:
    if not supports(operation, args):
        raise CUABackendError("cua_operation_unsupported", fallback_allowed=True)


    if operation == "list_apps":
        data = _call_js(
            operation,
            args,
            "const __apps=await cua.listApps({emit:false});return {apps:__apps};",
        )
        if not isinstance(data, dict) or not isinstance(data.get("apps"), list):
            raise CUABackendError("cua_protocol_error")
        allowlist = args.get("allowed_apps") or []
        limit = max(1, min(int(args.get("limit", 32)), 64))
        apps = [
            app for app in data["apps"]
            if isinstance(app, dict)
            and isinstance(app.get("id"), str)
            and ("*" in allowlist or app["id"] in allowlist)
        ]
        return {
            "apps": apps[:limit],
            "truncated": len(apps) > limit,
            "backend": "cua",
        }

    if operation == "get_state":
        app_id = str(args["app_id"])
        # Never mint CUA-bound window tokens until the primary runtime is known
        # to be available. This preserves auto -> native fallback semantics.
        _ensure_started()
        resolved = _resolve_window(args, app_id)
        if isinstance(resolved, dict):
            return resolved
        target, _, pid, _ = resolved
        _select_window_background(args, app_id, target, pid)

        binding = "__mcp4_" + uuid.uuid4().hex
        body = (
            _running_guard(app_id)
            + f"const __app=await cua.getApp({_json(app_id)});"
            + f"globalThis[{_json(binding)}]=__app;"
            + "const __state=await __app.getAXState({disableDiffing:true,emit:false});"
            + "return {state:__state};"
        )
        data = _call_js(operation, args, body)
        if not isinstance(data, dict) or not isinstance(data.get("state"), str):
            raise CUABackendError("cua_protocol_error")
        state = data["state"]

        verified_pid, refreshed, windows_truncated = _verify_window_binding(
            args,
            app_id,
            native_window_id=target["native_window_id"],
            expected_window_id=target["window_id"],
            expected_pid=pid,
        )
        if verified_pid != pid:
            raise CUABackendError("stale_process")

        current = next(
            item
            for item in refreshed
            if item["native_window_id"] == target["native_window_id"]
        )
        title = _window_title(state)
        if title and current.get("title") and title != current["title"]:
            raise CUABackendError("stale_window")

        window_id = target["window_id"]
        snapshot_id = "cua-snapshot:" + uuid.uuid4().hex
        elements, mapping = _parse_elements(state, snapshot_id)
        _new_snapshot(
            app_id,
            window_id,
            mapping,
            binding,
            native_window_id=target["native_window_id"],
            pid=pid,
            token=snapshot_id,
        )
        limit = max(1, min(int(args.get("max_elements", 200)), 400))
        return {
            "status": "ok",
            "app_id": app_id,
            "pid": pid,
            "window_id": window_id,
            "windows": _public_windows(refreshed),
            "windows_truncated": windows_truncated,
            "snapshot_id": snapshot_id,
            "elements": elements[:limit],
            "truncated": len(elements) > limit,
            "backend": "cua",
            "background_capable": True,
        }

    if operation == "screenshot":
        app_id = str(args["app_id"])
        # Prove the CUA runtime is available before touching window selection.
        _ensure_started()
        resolved = _resolve_window(args, app_id)
        if isinstance(resolved, dict):
            raise CUABackendError("window_id_required")
        target, _, pid, _ = resolved
        _select_window_background(args, app_id, target, pid)

        body = (
            _running_guard(app_id)
            + f"const __app=await cua.getApp({_json(app_id)});"
            + "const __state=await __app.getAXState({disableDiffing:true,emit:false});"
            + "const __png=await __app.getScreenshot({emit:false});"
            + "return {state:__state,png_base64:Buffer.from(__png).toString('base64')};"
        )
        data = _call_js(operation, args, body)
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("png_base64"), str)
            or not isinstance(data.get("state"), str)
        ):
            raise CUABackendError("cua_protocol_error")

        verified_pid, refreshed, _ = _verify_window_binding(
            args,
            app_id,
            native_window_id=target["native_window_id"],
            expected_window_id=target["window_id"],
            expected_pid=pid,
        )
        if verified_pid != pid:
            raise CUABackendError("stale_process")
        current = next(
            item
            for item in refreshed
            if item["native_window_id"] == target["native_window_id"]
        )
        title = _window_title(data["state"])
        if title and current.get("title") and title != current["title"]:
            raise CUABackendError("stale_window")

        encoded = data["png_base64"]
        mime_type, dims = _image_metadata(encoded)
        _invalidate_app_snapshots(app_id)
        result: dict[str, Any] = {
            "image_base64": encoded,
            "mime_type": mime_type,
            "app_id": app_id,
            "pid": pid,
            "window_id": target["window_id"],
            "backend": "cua",
        }
        if dims:
            result["width"], result["height"] = dims
        return result

    if operation == "launch_app":
        app_id = str(args["app_id"])
        data = _call_js(
            operation,
            args,
            f"await cua.getApp({_json(app_id)});return {{success:true}};",
            effectful=True,
        )
        if not isinstance(data, dict) or data.get("success") is not True:
            raise CUABackendError("cua_protocol_error", "outcome_unknown")
        return {
            "success": True,
            "effect": "completed",
            "app_id": app_id,
            "backend": "cua",
            "background": True,
        }

    if operation in _SNAPSHOT_OPERATIONS:
        snap = _snapshot(args)
        binding = snap.get("binding")
        native_window_id = snap.get("native_window_id")
        snapshot_pid = snap.get("pid")
        if (
            not isinstance(binding, str)
            or not isinstance(native_window_id, str)
            or not isinstance(snapshot_pid, int)
        ):
            raise CUABackendError("stale_snapshot")
        verified_pid, _, _ = _verify_window_binding(
            args,
            str(args["app_id"]),
            native_window_id=native_window_id,
            expected_window_id=str(args["window_id"]),
            expected_pid=snapshot_pid,
        )
        if verified_pid != snapshot_pid:
            raise CUABackendError("stale_process")
        app_expr = f"globalThis[{_json(binding)}]"
        if operation == "click":
            element_id = args.get("element_id")
            index = snap["elements"].get(element_id)
            if not isinstance(index, int):
                raise CUABackendError("stale_element")
            body = f"const __app={app_expr};await __app.click({index});return {{success:true}};"
            strategy = "CUAElementClick"
        elif operation == "type_text":
            element_id = args.get("element_id")
            index = snap["elements"].get(element_id)
            if not isinstance(index, int):
                raise CUABackendError("stale_element")
            body = (
                f"const __app={app_expr};"
                f"await __app.setValue({index},{_json(str(args['text']))});"
                "return {success:true};"
            )
            strategy = "CUASetValue"
        elif operation == "type_keyboard":
            body = (
                f"const __app={app_expr};"
                f"await __app.paste({_json(str(args['text']))},{{format:'text'}});"
                "return {success:true};"
            )
            strategy = "CUABackgroundPaste"
        else:
            key = str(args["key"])
            if len(key) == 1:
                key = key.lower()
            modifiers = [str(value) for value in args.get("modifiers", [])]
            prefix = {
                "command": "super",
                "control": "ctrl",
                "option": "alt",
                "shift": "shift",
            }
            chord = "+".join([*(prefix[value] for value in modifiers), key])
            body = (
                f"const __app={app_expr};"
                f"await __app.pressKey({_json(chord)});"
                "return {success:true};"
            )
            strategy = "CUAPressKey"
        try:
            data = _call_js(operation, args, body, effectful=True)
            if not isinstance(data, dict) or data.get("success") is not True:
                raise CUABackendError("cua_protocol_error", "outcome_unknown")
        finally:
            _spend_snapshot(args)
        return {
            "success": True,
            "effect": "completed",
            "strategy": strategy,
            "observe_again": True,
            "backend": "cua",
            "background": True,
        }

    raise CUABackendError("cua_operation_unsupported", fallback_allowed=True)

def call(operation: str, args: dict[str, Any], *, effectful: bool = False) -> dict[str, Any]:
    global _active_deadline
    # Window selection and the subsequent Sky observation/action are one
    # transaction. The RLock is re-entrant because _call_js also uses it.
    call_started = time.monotonic()
    acquired = _lock.acquire(timeout=IO_TIMEOUT)
    if not acquired:
        raise CUABackendError("cua_timeout", "not_started", fallback_allowed=not effectful)
    try:
        _active_deadline = call_started + IO_TIMEOUT
        try:
            return _call_locked(operation, args, effectful=effectful)
        except CUABackendError as exc:
            if exc.code in {"cua_timeout", "cua_disconnected", "cua_not_running", "cua_protocol_error"}:
                stop()
            raise
    finally:
        _active_deadline = None
        _lock.release()
