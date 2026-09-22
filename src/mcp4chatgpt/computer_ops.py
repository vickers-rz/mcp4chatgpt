"""Policy and MCP result framing for macOS Computer Use."""
from __future__ import annotations

import base64
import re
from typing import Any

from . import computer_backend, computer_cua_backend
from .config import Config
from .mcp_types import RawMCPToolResult


_BUNDLE_ID = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
_ACTIONS = {"click", "press_key", "type_text", "type_keyboard", "launch_app", "activate_window", "pointer_move", "pointer_click", "pointer_drag", "pointer_scroll"}
ALLOWED_KEYS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") | {
    "Return", "Tab", "Escape", "Left", "Right", "Up", "Down", "Backspace",
    "Space", "DeleteForward", "Home", "End", "PageUp", "PageDown",
}
ALLOWED_MODIFIERS = {"shift", "option", "control", "command"}


def _call(config: Config, operation: str, app_id: str | None = None, **args: Any) -> dict[str, Any]:
    if config.computer_mode == "off":
        raise ValueError("computer_mode_off")
    if operation in _ACTIONS and config.computer_mode != "interact":
        raise ValueError("computer_interaction_disabled")
    if not config.computer_allowed_apps:
        raise ValueError("computer_allowlist_empty")
    if args.get("pid") is not None and (type(args["pid"]) is not int or not 0 < args["pid"] < 2**31):
        raise ValueError("computer_pid_invalid")
    if app_id is not None:
        if not _BUNDLE_ID.fullmatch(app_id) or (
            "*" not in config.computer_allowed_apps
            and app_id not in config.computer_allowed_apps
        ):
            raise ValueError("computer_app_not_allowed")
        args["app_id"] = app_id
    args["allowed_apps"] = list(config.computer_allowed_apps)

    backend_mode = getattr(config, "computer_backend", "native")
    cua_supported = computer_cua_backend.supports(operation, args)
    if backend_mode == "cua" and not cua_supported:
        return {
            "success": False,
            "error": "cua_operation_unsupported",
            "effect": "not_started",
            "backend": "cua",
            "recovery": "none",
        }

    use_cua = backend_mode in {"auto", "cua"} and cua_supported
    if use_cua:
        try:
            return computer_cua_backend.call(operation, args, effectful=operation in _ACTIONS)
        except computer_cua_backend.ComputerCUABackendError as exc:
            cua_bound = (
                computer_cua_backend.is_cua_snapshot(args.get("snapshot_id"))
                or computer_cua_backend.is_cua_window(args.get("window_id"))
            )
            may_fallback = (
                backend_mode == "auto"
                and not cua_bound
                and exc.effect == "not_started"
                and getattr(exc, "fallback_allowed", False)
            )
            if not may_fallback:
                return {
                    "success": False,
                    "error": exc.code,
                    "effect": exc.effect,
                    "backend": "cua",
                    "recovery": "observe_before_retry" if exc.effect == "outcome_unknown" else "none",
                }
            # Auto fallback is reserved for explicit transport/unavailability
            # failures that prove CUA never started the requested effect.

    try:
        return computer_backend.call(operation, args, effectful=operation in _ACTIONS)
    except computer_backend.ComputerBackendError as exc:
        return {
            "success": False,
            "error": exc.code,
            "effect": exc.effect,
            "recovery": "observe_before_retry" if exc.effect == "outcome_unknown" else "none",
        }


def _require_full_desktop(config: Config) -> None:
    if "*" not in config.computer_allowed_apps:
        raise ValueError("computer_full_desktop_not_allowed")


def _image_result(result: dict[str, Any], *, error_statuses: set[str] | None = None) -> RawMCPToolResult:
    if result.get("success") is False or result.get("status") in (error_statuses or set()):
        message = result.get("error") or result.get("status") or "computer_image_unavailable"
        return RawMCPToolResult({"content": [{"type": "text", "text": str(message)}], "structuredContent": result})
    encoded = result.pop("image_base64", None)
    mime_type = result.get("mime_type")
    if encoded is None:
        encoded = result.pop("png_base64", None)
        mime_type = "image/png"
    if not isinstance(encoded, str) or len(encoded) > 12 * 1024 * 1024:
        raise ValueError("computer_screenshot_invalid_image")
    base64.b64decode(encoded, validate=True)
    if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise ValueError("computer_screenshot_invalid_mime")
    return RawMCPToolResult({"content": [{"type": "image", "mimeType": mime_type, "data": encoded}], "structuredContent": result})


def list_apps(config: Config, limit: int = 32) -> dict[str, Any]:
    return _call(config, "list_apps", limit=max(1, min(limit, 64)))


def get_state(config: Config, app_id: str, window_id: str | None = None, max_elements: int = 200, pid: int | None = None) -> dict[str, Any]:
    return _call(config, "get_state", app_id, window_id=window_id, max_elements=max(1, min(max_elements, 400)), pid=pid)


def screenshot(config: Config, app_id: str, window_id: str, pid: int | None = None) -> RawMCPToolResult:
    result = _call(config, "screenshot", app_id, window_id=window_id, pid=pid)
    return _image_result(result, error_statuses={"process_selection_required", "window_selection_required"})


def permission_status(config: Config) -> dict[str, Any]:
    return _call(config, "permission_status")


def request_permissions(
    config: Config,
    *,
    accessibility: bool = True,
    screen_recording: bool = True,
    event_posting: bool = True,
) -> dict[str, Any]:
    return _call(
        config,
        "request_permissions",
        accessibility=bool(accessibility),
        screen_recording=bool(screen_recording),
        event_posting=bool(event_posting),
    )


def list_displays(config: Config) -> dict[str, Any]:
    _require_full_desktop(config)
    return _call(config, "list_displays")


def screenshot_display(config: Config, display_id: str) -> RawMCPToolResult:
    _require_full_desktop(config)
    if not isinstance(display_id, str) or not display_id:
        raise ValueError("computer_display_invalid")
    return _image_result(_call(config, "screenshot_display", display_id=display_id))


def launch_app(config: Config, app_id: str) -> dict[str, Any]:
    return _call(config, "launch_app", app_id)


def activate_window(config: Config, args: dict[str, Any]) -> dict[str, Any]:
    return _call(config, "activate_window", args["app_id"], window_id=args["window_id"], pid=args.get("pid"))


def _pointer(config: Config, operation: str, args: dict[str, Any]) -> dict[str, Any]:
    _require_full_desktop(config)
    x, y = args["x"], args["y"]
    if type(x) is not int or type(y) is not int or x < 0 or y < 0:
        raise ValueError("computer_pointer_coordinates_invalid")
    forwarded: dict[str, Any] = {
        "display_id": args["display_id"],
        "display_snapshot_id": args["display_snapshot_id"],
        "x": x,
        "y": y,
    }
    if operation == "pointer_click":
        button = args.get("button", "left")
        click_count = args.get("click_count", 1)
        if button not in {"left", "right"} or type(click_count) is not int or click_count not in {1, 2}:
            raise ValueError("computer_pointer_click_invalid")
        forwarded.update(button=button, click_count=click_count)
    return _call(config, operation, **forwarded)


def pointer_move(config: Config, args: dict[str, Any]) -> dict[str, Any]:
    return _pointer(config, "pointer_move", args)


def pointer_click(config: Config, args: dict[str, Any]) -> dict[str, Any]:
    return _pointer(config, "pointer_click", args)


def pointer_drag(config: Config, args: dict[str, Any]) -> dict[str, Any]:
    _require_full_desktop(config)
    coords = (args["from_x"], args["from_y"], args["to_x"], args["to_y"])
    if any(type(value) is not int or value < 0 for value in coords):
        raise ValueError("computer_pointer_coordinates_invalid")
    return _call(
        config,
        "pointer_drag",
        display_id=args["display_id"],
        display_snapshot_id=args["display_snapshot_id"],
        from_x=coords[0],
        from_y=coords[1],
        to_x=coords[2],
        to_y=coords[3],
    )


def pointer_scroll(config: Config, args: dict[str, Any]) -> dict[str, Any]:
    _require_full_desktop(config)
    x, y = args["x"], args["y"]
    delta_y, delta_x = args["delta_y"], args.get("delta_x", 0)
    if any(type(value) is not int for value in (x, y, delta_y, delta_x)) or x < 0 or y < 0:
        raise ValueError("computer_pointer_coordinates_invalid")
    if (delta_x == 0 and delta_y == 0) or abs(delta_x) > 120 or abs(delta_y) > 120:
        raise ValueError("computer_scroll_delta_invalid")
    return _call(
        config,
        "pointer_scroll",
        display_id=args["display_id"],
        display_snapshot_id=args["display_snapshot_id"],
        x=x,
        y=y,
        delta_y=delta_y,
        delta_x=delta_x,
    )


def click(config: Config, args: dict[str, Any]) -> dict[str, Any]:
    return _call(config, "click", args["app_id"], window_id=args["window_id"], snapshot_id=args["snapshot_id"], element_id=args["element_id"], pid=args.get("pid"))


def press_key(config: Config, args: dict[str, Any]) -> dict[str, Any]:
    if args["key"] not in ALLOWED_KEYS:
        raise ValueError("computer_key_not_allowed")
    modifiers = args.get("modifiers", [])
    if not isinstance(modifiers, list) or len(modifiers) > 4 or len(set(modifiers)) != len(modifiers) or any(m not in ALLOWED_MODIFIERS for m in modifiers):
        raise ValueError("computer_modifiers_invalid")
    return _call(config, "press_key", args["app_id"], window_id=args["window_id"], snapshot_id=args["snapshot_id"], key=args["key"], modifiers=modifiers, pid=args.get("pid"))


def type_text(config: Config, args: dict[str, Any]) -> dict[str, Any]:
    if args.get("mode", "replace_value") != "replace_value" or len(args["text"]) > 20_000:
        raise ValueError("computer_text_invalid")
    return _call(config, "type_text", args["app_id"], window_id=args["window_id"], snapshot_id=args["snapshot_id"], element_id=args["element_id"], text=args["text"], mode="replace_value", pid=args.get("pid"))


def type_keyboard(config: Config, args: dict[str, Any]) -> dict[str, Any]:
    text = args.get("text")
    if not isinstance(text, str) or not text or len(text) > 4_000:
        raise ValueError("computer_keyboard_text_invalid")
    return _call(
        config,
        "type_keyboard",
        args["app_id"],
        window_id=args["window_id"],
        snapshot_id=args["snapshot_id"],
        text=text,
        pid=args.get("pid"),
    )
