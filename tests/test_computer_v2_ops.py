from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mcp4chatgpt import computer_ops
from mcp4chatgpt.tools import build_tools


def cfg(mode: str = "interact", allowed: tuple[str, ...] = ("*",)):
    return SimpleNamespace(computer_mode=mode, computer_allowed_apps=allowed)


def test_v2_tool_visibility_by_mode() -> None:
    off = {tool.name for tool in build_tools(computer_mode="off")}
    observe = {tool.name for tool in build_tools(computer_mode="observe")}
    interact = {tool.name for tool in build_tools(computer_mode="interact")}

    assert not any(name.startswith("computer_") for name in off)
    assert {"computer_permissions", "computer_request_permissions", "computer_list_displays", "computer_screenshot_display"} <= observe
    assert "computer_launch_app" not in observe
    assert "computer_activate_window" not in observe
    assert "computer_pointer_move" not in observe
    assert "computer_pointer_click" not in observe
    assert "computer_pointer_drag" not in observe
    assert "computer_pointer_scroll" not in observe
    assert "computer_type_keyboard" not in observe
    assert {
        "computer_permissions",
        "computer_request_permissions",
        "computer_list_displays",
        "computer_screenshot_display",
        "computer_launch_app",
        "computer_activate_window",
        "computer_pointer_move",
        "computer_pointer_click",
        "computer_pointer_drag",
        "computer_pointer_scroll",
        "computer_type_keyboard",
    } <= interact


def test_v2_full_desktop_requires_wildcard() -> None:
    with pytest.raises(ValueError, match="computer_full_desktop_not_allowed"):
        computer_ops.list_displays(cfg(allowed=("com.apple.TextEdit",)))


def test_v2_display_screenshot_frames_image() -> None:
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    with patch.object(
        computer_ops.computer_backend,
        "call",
        return_value={
            "png_base64": png,
            "display_snapshot_id": "snapshot-1",
            "source_width": 200,
            "source_height": 100,
        },
    ) as backend:
        result = computer_ops.screenshot_display(cfg(), "display-1").value

    assert result["content"] == [{"type": "image", "mimeType": "image/png", "data": png}]
    assert result["structuredContent"]["display_snapshot_id"] == "snapshot-1"
    assert backend.call_args.args[0] == "screenshot_display"
    assert backend.call_args.args[1]["allowed_apps"] == ["*"]


def test_v2_pointer_is_effectful_and_observe_rejects_it() -> None:
    args = {"display_id": "display-1", "display_snapshot_id": "snapshot-1", "x": 20, "y": 30}
    with pytest.raises(ValueError, match="computer_interaction_disabled"):
        computer_ops.pointer_click(cfg(mode="observe"), args)

    with patch.object(
        computer_ops.computer_backend,
        "call",
        return_value={"success": True, "effect": "completed"},
    ) as backend:
        result = computer_ops.pointer_click(cfg(), args)

    assert result["success"] is True
    assert backend.call_args.args[0] == "pointer_click"
    assert backend.call_args.kwargs["effectful"] is True
    assert backend.call_args.args[1]["display_snapshot_id"] == "snapshot-1"


def test_v2_pointer_rejects_invalid_coordinates_before_helper() -> None:
    with patch.object(computer_ops.computer_backend, "call") as backend:
        with pytest.raises(ValueError, match="computer_pointer_coordinates_invalid"):
            computer_ops.pointer_move(
                cfg(),
                {"display_id": "display-1", "display_snapshot_id": "snapshot-1", "x": -1, "y": 0},
            )
    backend.assert_not_called()


def test_v2_keyboard_typing_is_effectful_and_snapshot_bound() -> None:
    args = {
        "app_id": "com.apple.TextEdit",
        "pid": 123,
        "window_id": "window-1",
        "snapshot_id": "snapshot-1",
        "text": "Hello 世界",
    }
    with patch.object(
        computer_ops.computer_backend,
        "call",
        return_value={"success": True, "effect": "completed", "strategy": "QuartzUnicode"},
    ) as backend:
        result = computer_ops.type_keyboard(cfg(), args)

    assert result["strategy"] == "QuartzUnicode"
    assert backend.call_args.args[0] == "type_keyboard"
    assert backend.call_args.args[1]["snapshot_id"] == "snapshot-1"
    assert backend.call_args.args[1]["text"] == "Hello 世界"
    assert backend.call_args.kwargs["effectful"] is True

    with pytest.raises(ValueError, match="computer_keyboard_text_invalid"):
        computer_ops.type_keyboard(cfg(), {**args, "text": ""})


def test_v2_permissions_and_new_actions_are_forwarded() -> None:
    with patch.object(
        computer_ops.computer_backend,
        "call",
        return_value={"accessibility_trusted": True, "screen_recording": False, "event_posting": True},
    ) as backend:
        status = computer_ops.permission_status(cfg(mode="observe"))
    assert status["accessibility_trusted"] is True
    assert backend.call_args.args[0] == "permission_status"
    assert backend.call_args.kwargs["effectful"] is False

    with patch.object(
        computer_ops.computer_backend,
        "call",
        return_value={"requested_screen_recording": True, "screen_recording": False},
    ) as backend:
        requested = computer_ops.request_permissions(
            cfg(mode="observe"),
            accessibility=False,
            screen_recording=True,
            event_posting=False,
        )
    assert requested["requested_screen_recording"] is True
    assert backend.call_args.args[0] == "request_permissions"
    assert backend.call_args.args[1]["screen_recording"] is True
    assert backend.call_args.kwargs["effectful"] is False

    with patch.object(computer_ops.computer_backend, "call", return_value={"success": True, "effect": "completed"}) as backend:
        computer_ops.launch_app(cfg(), "com.apple.TextEdit")
    assert backend.call_args.args[0] == "launch_app"
    assert backend.call_args.kwargs["effectful"] is True


def test_v2_drag_scroll_click_and_shortcuts_validation() -> None:
    with patch.object(computer_ops.computer_backend, "call", return_value={"success": True, "effect": "completed"}) as backend:
        computer_ops.pointer_click(
            cfg(),
            {
                "display_id": "d",
                "display_snapshot_id": "s",
                "x": 10,
                "y": 20,
                "button": "right",
                "click_count": 2,
            },
        )
    assert backend.call_args.args[1]["button"] == "right"
    assert backend.call_args.args[1]["click_count"] == 2

    with patch.object(computer_ops.computer_backend, "call", return_value={"success": True, "effect": "completed"}) as backend:
        computer_ops.pointer_drag(
            cfg(),
            {
                "display_id": "d",
                "display_snapshot_id": "s",
                "from_x": 1,
                "from_y": 2,
                "to_x": 30,
                "to_y": 40,
            },
        )
    assert backend.call_args.args[0] == "pointer_drag"
    assert backend.call_args.kwargs["effectful"] is True

    with patch.object(computer_ops.computer_backend, "call", return_value={"success": True, "effect": "completed"}) as backend:
        computer_ops.pointer_scroll(
            cfg(),
            {
                "display_id": "d",
                "display_snapshot_id": "s",
                "x": 1,
                "y": 2,
                "delta_y": -3,
                "delta_x": 0,
            },
        )
    assert backend.call_args.args[0] == "pointer_scroll"
    assert backend.call_args.kwargs["effectful"] is True

    key_args = {
        "app_id": "com.apple.TextEdit",
        "window_id": "w",
        "snapshot_id": "s",
        "key": "C",
        "modifiers": ["command", "shift"],
    }
    with patch.object(computer_ops.computer_backend, "call", return_value={"success": True, "effect": "completed"}) as backend:
        computer_ops.press_key(cfg(), key_args)
    assert backend.call_args.args[1]["modifiers"] == ["command", "shift"]

    with pytest.raises(ValueError, match="computer_pointer_click_invalid"):
        computer_ops.pointer_click(
            cfg(),
            {"display_id": "d", "display_snapshot_id": "s", "x": 0, "y": 0, "button": "middle", "click_count": 1},
        )
    with pytest.raises(ValueError, match="computer_scroll_delta_invalid"):
        computer_ops.pointer_scroll(
            cfg(),
            {"display_id": "d", "display_snapshot_id": "s", "x": 0, "y": 0, "delta_y": 0, "delta_x": 0},
        )

