from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp4chatgpt import computer_ops
from mcp4chatgpt.audit import AuditLogger
from mcp4chatgpt.tools import ToolRegistry
from test_core import make_config


def config(tmp_path: Path, mode: str = "observe"):
    return replace(make_config(tmp_path), computer_mode=mode, computer_allowed_apps=("com.apple.TextEdit",))


def test_default_off_hides_tools(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    registry = ToolRegistry(cfg, AuditLogger(cfg.audit_log))
    assert not any(t["name"].startswith("computer_") for t in registry.list_tools(auth_required=False)["tools"])
    with pytest.raises(ValueError, match="computer_mode_off"):
        computer_ops.list_apps(cfg)


def test_allowlist_and_observe_mode_reject_actions(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    registry = ToolRegistry(cfg, AuditLogger(cfg.audit_log))
    names = {t["name"] for t in registry.list_tools(auth_required=False)["tools"]}
    assert "computer_screenshot" in names
    assert "computer_click" not in names
    with pytest.raises(ValueError, match="computer_app_not_allowed"):
        computer_ops.get_state(cfg, "com.apple.Terminal")
    with pytest.raises(ValueError, match="computer_interaction_disabled"):
        computer_ops.click(cfg, {"app_id": "com.apple.TextEdit", "window_id": "w", "snapshot_id": "s", "element_id": "e"})


def test_wildcard_allowlist_accepts_any_valid_bundle_id(tmp_path: Path) -> None:
    cfg = replace(make_config(tmp_path), computer_mode="observe", computer_allowed_apps=("*",))
    with patch.object(computer_ops.computer_backend, "call", return_value={"status": "ok"}) as backend:
        assert computer_ops.get_state(cfg, "com.apple.Terminal") == {"status": "ok"}
    assert backend.call_args.args[1]["allowed_apps"] == ["*"]
    with pytest.raises(ValueError, match="computer_app_not_allowed"):
        computer_ops.get_state(cfg, "not-a-bundle-id")


def test_full_desktop_tools_require_wildcard_and_mode_boundaries(tmp_path: Path) -> None:
    restricted = config(tmp_path)
    with pytest.raises(ValueError, match="computer_full_desktop_not_allowed"):
        computer_ops.list_displays(restricted)

    observe = replace(restricted, computer_mode="observe", computer_allowed_apps=("*",))
    registry = ToolRegistry(observe, AuditLogger(observe.audit_log))
    names = {t["name"] for t in registry.list_tools(auth_required=False)["tools"]}
    assert {"computer_list_displays", "computer_screenshot_display"} <= names
    assert "computer_activate_window" not in names
    assert "computer_pointer_move" not in names
    assert "computer_pointer_click" not in names
    with pytest.raises(ValueError, match="computer_interaction_disabled"):
        computer_ops.pointer_click(
            observe,
            {"display_id": "d", "display_snapshot_id": "s", "x": 1, "y": 1},
        )

    interact = replace(observe, computer_mode="interact")
    registry = ToolRegistry(interact, AuditLogger(interact.audit_log))
    names = {t["name"] for t in registry.list_tools(auth_required=False)["tools"]}
    assert {"computer_activate_window", "computer_pointer_move", "computer_pointer_click"} <= names


def test_display_screenshot_and_pointer_forward_snapshot_fence(tmp_path: Path) -> None:
    cfg = replace(make_config(tmp_path), computer_mode="interact", computer_allowed_apps=("*",))
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    with patch.object(
        computer_ops.computer_backend,
        "call",
        return_value={"png_base64": png, "display_snapshot_id": "snap", "source_width": 100, "source_height": 80},
    ) as backend:
        result = computer_ops.screenshot_display(cfg, "display-1").value
    assert result["content"] == [{"type": "image", "mimeType": "image/png", "data": png}]
    assert result["structuredContent"]["display_snapshot_id"] == "snap"
    assert backend.call_args.args[0] == "screenshot_display"
    assert backend.call_args.args[1]["display_id"] == "display-1"
    assert backend.call_args.args[1]["allowed_apps"] == ["*"]

    with patch.object(computer_ops.computer_backend, "call", return_value={"success": True, "effect": "completed"}) as backend:
        assert computer_ops.pointer_click(
            cfg,
            {"display_id": "display-1", "display_snapshot_id": "snap", "x": 25, "y": 30},
        )["success"] is True
    assert backend.call_args.args[0] == "pointer_click"
    assert backend.call_args.args[1]["display_snapshot_id"] == "snap"
    assert backend.call_args.kwargs["effectful"] is True


def test_screenshot_returns_image_content(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    with patch.object(computer_ops.computer_backend, "call", return_value={"png_base64": png, "width": 1, "height": 1}) as backend:
        result = computer_ops.screenshot(cfg, "com.apple.TextEdit", "w").value
    assert result["content"] == [{"type": "image", "mimeType": "image/png", "data": png}]
    assert result["structuredContent"] == {"width": 1, "height": 1}
    assert backend.call_args.args[1]["allowed_apps"] == ["com.apple.TextEdit"]


def test_screenshot_returns_process_selection(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    selection = {"status": "process_selection_required", "processes": [{"pid": 123}, {"pid": 456}]}
    with patch.object(computer_ops.computer_backend, "call", return_value=selection):
        result = computer_ops.screenshot(cfg, "com.apple.TextEdit", "w").value
    assert result["structuredContent"] == selection
    assert result["content"] == [{"type": "text", "text": "process_selection_required"}]


def test_uncertain_action_is_not_retried(tmp_path: Path) -> None:
    cfg = config(tmp_path, "interact")
    with patch.object(computer_ops.computer_backend, "call", side_effect=computer_ops.computer_backend.ComputerBackendError("helper_timeout", "outcome_unknown")) as backend:
        result = computer_ops.click(cfg, {"app_id": "com.apple.TextEdit", "window_id": "w", "snapshot_id": "s", "element_id": "e"})
    assert result == {"success": False, "error": "helper_timeout", "effect": "outcome_unknown", "recovery": "observe_before_retry"}
    assert backend.call_count == 1


def test_unsupported_key_modifier_is_rejected_before_helper(tmp_path: Path) -> None:
    cfg = config(tmp_path, "interact")
    args = {"app_id": "com.apple.TextEdit", "window_id": "w", "snapshot_id": "s", "key": "Tab", "modifiers": ["fn"]}
    with patch.object(computer_ops.computer_backend, "call") as backend:
        with pytest.raises(ValueError, match="computer_modifiers_invalid"):
            computer_ops.press_key(cfg, args)
    backend.assert_not_called()


def test_text_is_not_recorded_in_audit(tmp_path: Path) -> None:
    cfg = config(tmp_path, "interact")
    registry = ToolRegistry(cfg, AuditLogger(cfg.audit_log))
    with patch.object(computer_ops.computer_backend, "call", return_value={"success": True, "effect": "completed"}):
        registry.call_tool("computer_type_text", {"app_id": "com.apple.TextEdit", "window_id": "w", "snapshot_id": "s", "element_id": "e", "text": "private-test-value"})
    assert "private-test-value" not in cfg.audit_log.read_text()


@pytest.mark.parametrize("tool,args", [
    ("computer_click", {"app_id": "com.apple.TextEdit", "window_id": "w", "snapshot_id": "s", "element_id": "e"}),
    ("computer_screenshot", {"app_id": "com.apple.TextEdit", "window_id": "w"}),
])
def test_failure_audit_preserves_effect(tmp_path, tool, args):
    cfg = config(tmp_path, "interact")
    registry = ToolRegistry(cfg, AuditLogger(cfg.audit_log))
    with patch.object(computer_ops.computer_backend, "call", side_effect=computer_ops.computer_backend.ComputerBackendError("helper_timeout", "outcome_unknown")):
        result = registry.call_tool(tool, args)
    assert result["structuredContent"]["success"] is False
    event = json.loads(cfg.audit_log.read_text().splitlines()[-1])
    assert event["ok"] is False
    assert event["effect"] == "outcome_unknown"
    assert event["error"] == "helper_timeout"


def test_selection_and_pid_survive_registry(tmp_path):
    cfg = config(tmp_path)
    registry = ToolRegistry(cfg, AuditLogger(cfg.audit_log))
    selection = {"status": "window_selection_required", "windows": [{"window_id": "w"}]}
    with patch.object(computer_ops.computer_backend, "call", return_value=selection) as backend:
        result = registry.call_tool("computer_get_state", {"app_id": "com.apple.TextEdit", "pid": 123})
    assert result["structuredContent"] == selection
    assert backend.call_args.args[1]["pid"] == 123
    assert json.loads(cfg.audit_log.read_text().splitlines()[-1])["ok"] is True
