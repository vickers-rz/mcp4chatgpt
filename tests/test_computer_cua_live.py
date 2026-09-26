"""Owned-window CUA acceptance; opt in with MCP_CUA_GUI_TESTS=1.

This exercises the local adapter and real transport, not ChatGPT's connector.
"""
import os
import platform
from dataclasses import replace

import pytest

from mcp4chatgpt import computer_cua_backend, computer_ops
from test_computer_native import APP, fixture_binary, native

pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin" or os.getenv("MCP_CUA_GUI_TESTS") != "1",
    reason="explicit live CUA fixture opt-in required",
)


def test_owned_window_cua_roundtrip(native, monkeypatch):
    upstream_errors = []
    original = computer_cua_backend._map_tool_error
    def capture_error(result, *, effectful):
        upstream_errors.extend(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        return original(result, effectful=effectful)
    monkeypatch.setattr(computer_cua_backend, "_map_tool_error", capture_error)
    cfg, launch = native
    launch()
    cfg = replace(cfg, computer_backend="cua")
    computer_cua_backend.stop()
    try:
        apps = computer_ops.list_apps(cfg)
        assert apps.get("backend") == "cua", apps
        state = computer_ops.get_state(cfg, APP)
        assert state.get("backend") == "cua", state
        assert state.get("status") == "ok", state
        window = state["window_id"]
        assert window.startswith("cua-window:")

        def observe():
            value = computer_ops.get_state(cfg, APP, window)
            assert value.get("status") == "ok", value
            assert value.get("backend") == "cua", value
            return value

        def action(snapshot, **kwargs):
            return dict(app_id=APP, window_id=window,
                        snapshot_id=snapshot["snapshot_id"], **kwargs)

        fields = [e for e in state["elements"] if "fixture text" in e.get("role", "")]
        assert fields, state["elements"]
        field = fields[0]
        marker = "CUA_PDCA_中文_20260926"
        result = computer_ops.type_text(cfg, action(state, element_id=field["element_id"], text=marker))
        assert result.get("effect") == "completed", result
        state = observe()
        field = next(e for e in state["elements"] if marker in e.get("role", ""))
        result = computer_ops.click(cfg, action(state, element_id=field["element_id"]))
        assert result.get("effect") == "completed", result
        state = observe()
        result = computer_ops.type_keyboard(cfg, action(state, text="_PASTE_"))
        state = observe()  # Observe even after an uncertain write; never replay it.
        assert result.get("effect") == "completed", (result, upstream_errors, state["elements"])
        assert result.get("strategy") == "CUABackgroundPaste", result
        state = observe()
        assert any("_PASTE_" in e.get("role", "") for e in state["elements"]), state
        shot = computer_ops.screenshot(cfg, APP, window).value
        assert shot["structuredContent"].get("backend") == "cua", shot["structuredContent"]
        assert any(c.get("type") == "image" and c.get("data") for c in shot["content"])
    finally:
        computer_cua_backend.stop()
        assert computer_cua_backend._process is None
