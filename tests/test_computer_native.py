"""Opt-in native acceptance: only owned fixture windows are inspected or changed.

MCP_NATIVE_GUI_TESTS=1 PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_computer_native.py
"""
import os
import platform
import plistlib
import signal
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp4chatgpt import computer_backend, computer_ops
from test_core import make_config

pytestmark = pytest.mark.skipif(platform.system() != "Darwin" or os.getenv("MCP_NATIVE_GUI_TESTS") != "1", reason="explicit native GUI fixture opt-in required")
APP = "dev.mcp4chatgpt.ComputerFixture"


@pytest.fixture(scope="module")
def fixture_binary(tmp_path_factory):
    app_bundle = tmp_path_factory.mktemp("gui") / "ComputerFixture.app"
    contents = app_bundle / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": APP, "CFBundleExecutable": "ComputerFixture", "CFBundleName": "ComputerFixture", "CFBundlePackageType": "APPL"}))
    binary = contents / "MacOS" / "ComputerFixture"
    source = Path(__file__).parent / "native" / "ComputerFixture.swift"
    subprocess.run(["xcrun", "swiftc", str(source), "-o", str(binary)], check=True, capture_output=True, timeout=60)
    return app_bundle


@pytest.fixture
def native(tmp_path, fixture_binary):
    cfg = replace(make_config(tmp_path), computer_mode="interact", computer_allowed_apps=(APP,))
    processes: list[int] = []
    computer_backend.stop()

    def running_pids() -> set[int]:
        result = computer_ops.list_apps(cfg, limit=64)
        return {app["pid"] for app in result.get("apps", []) if app.get("app_id") == APP and isinstance(app.get("pid"), int)}

    def launch(*args):
        before = running_pids()
        subprocess.run(["open", "-n", str(fixture_binary), "--args", *args], check=True, capture_output=True, timeout=15)
        deadline = time.monotonic() + 12
        pid = None
        while time.monotonic() < deadline:
            current = running_pids()
            created = current - before
            if created:
                pid = max(created)
                break
            time.sleep(.1)
        if pid is None:
            pytest.fail("fixture failed to register with LaunchServices")
        processes.append(pid)
        proc = SimpleNamespace(pid=pid)
        last_result = None
        while time.monotonic() < deadline:
            result = computer_ops.get_state(cfg, APP, pid=pid)
            last_result = result
            if result.get("status") in {"ok", "window_selection_required"}:
                return proc, result
            if result.get("error") == "accessibility_permission_required":
                pytest.fail("Accessibility permission is required for this native acceptance run")
            time.sleep(.1)
        pytest.fail(f"fixture failed to expose windows; last_result={last_result!r}")
    yield cfg, launch
    computer_backend.stop()
    for pid in processes:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.05)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_two_windows_actions_and_failed_selection(native):
    cfg, launch = native
    proc, selection = launch("--two-windows")
    assert selection["status"] == "window_selection_required"
    assert len(selection["windows"]) == 2
    w = next(w["window_id"] for w in selection["windows"] if w["title"] == "MCP4ChatGPT Computer Fixture")
    assert w.startswith("native-window:")
    inventory = computer_backend.call("list_windows", {"app_id": APP, "pid": proc.pid, "allowed_apps": [APP]})
    assert w in {item["window_id"] for item in inventory["windows"]}
    state = computer_ops.get_state(cfg, APP, w, pid=proc.pid)
    button = next(e for e in state["elements"] if e.get("title") == "Increment 0")
    assert computer_ops.get_state(cfg, APP, "missing", pid=proc.pid)["error"] == "stale_window"
    action = {"app_id": APP, "pid": proc.pid, "window_id": w, "snapshot_id": state["snapshot_id"], "element_id": button["element_id"]}
    assert computer_ops.click(cfg, action)["effect"] == "completed"
    assert computer_ops.click(cfg, action)["error"] == "stale_snapshot"
    state = computer_ops.get_state(cfg, APP, w, pid=proc.pid)
    assert any(e.get("title") == "Increment 1" for e in state["elements"])
    field = next(e for e in state["elements"] if e.get("role") == "AXTextField")
    action.update(snapshot_id=state["snapshot_id"], element_id=field["element_id"], text="中文 😀\nfixture")
    assert computer_ops.type_text(cfg, action)["effect"] == "completed"
    state = computer_ops.get_state(cfg, APP, w, pid=proc.pid)
    assert any(e.get("value") == "中文 😀\nfixture" for e in state["elements"])
    shot = computer_ops.screenshot(cfg, APP, w, pid=proc.pid).value
    if shot["structuredContent"].get("error") == "screen_recording_permission_required":
        assert all(item["type"] != "image" for item in shot["content"])
    else:
        assert shot["content"][0]["type"] == "image", shot["structuredContent"]
    computer_backend.stop()
    assert computer_ops.screenshot(cfg, APP, w, pid=proc.pid).value["structuredContent"]["error"] == "stale_window"


def test_two_processes_require_pid_and_reject_cross_binding(native):
    cfg, launch = native
    first, first_state = launch()
    second, second_state = launch()
    choice = computer_ops.get_state(cfg, APP)
    assert choice["status"] == "process_selection_required"
    assert {first.pid, second.pid}.issubset({p["pid"] for p in choice["processes"]})
    assert computer_ops.get_state(cfg, APP, pid=second.pid)["pid"] == second.pid
    button = next(e for e in first_state["elements"] if e.get("title") == "Increment 0")
    result = computer_ops.click(cfg, {"app_id": APP, "pid": second.pid, "window_id": first_state["window_id"], "snapshot_id": first_state["snapshot_id"], "element_id": button["element_id"]})
    assert result["error"] == "stale_snapshot"


def test_duplicate_title_minimized_window_never_captures_other(native):
    cfg, launch = native
    proc, selection = launch("--two-windows", "--duplicate-titles", "--minimize-second")
    deadline = time.monotonic() + 5
    while not any(w.get("minimized") for w in selection["windows"]) and time.monotonic() < deadline:
        time.sleep(.1)
        selection = computer_ops.get_state(cfg, APP, pid=proc.pid)
    minimized = next(w for w in selection["windows"] if w["minimized"])
    result = computer_ops.screenshot(cfg, APP, minimized["window_id"], pid=proc.pid).value
    assert result["structuredContent"]["error"] == "window_not_visible"
    assert all(c["type"] != "image" for c in result["content"])


def test_display_snapshot_pointer_click_roundtrip(native):
    cfg, launch = native
    proc, state = launch()
    assert state["status"] == "ok"
    button = next(e for e in state["elements"] if e.get("title") == "Increment 0")
    frame = button.get("frame")
    assert frame and frame["width"] > 0 and frame["height"] > 0

    desktop_cfg = replace(cfg, computer_allowed_apps=("*",))
    displays = computer_ops.list_displays(desktop_cfg)
    cx = frame["x"] + frame["width"] / 2
    cy = frame["y"] + frame["height"] / 2
    display = next(
        d
        for d in displays["displays"]
        if d["logical_x"] <= cx < d["logical_x"] + d["logical_width"]
        and d["logical_y"] <= cy < d["logical_y"] + d["logical_height"]
    )
    shot = computer_ops.screenshot_display(desktop_cfg, display["display_id"]).value
    assert shot["content"][0]["type"] == "image", shot["structuredContent"]
    snapshot_id = shot["structuredContent"]["display_snapshot_id"]

    x = round((cx - display["logical_x"]) / display["logical_width"] * display["source_width"])
    y = round((cy - display["logical_y"]) / display["logical_height"] * display["source_height"])
    result = computer_ops.pointer_click(
        desktop_cfg,
        {
            "display_id": display["display_id"],
            "display_snapshot_id": snapshot_id,
            "x": x,
            "y": y,
            "button": "left",
            "click_count": 1,
        },
    )
    assert result["effect"] == "completed", result

    deadline = time.monotonic() + 3
    latest = None
    while time.monotonic() < deadline:
        latest = computer_ops.get_state(cfg, APP, pid=proc.pid)
        if latest.get("status") == "ok" and any(e.get("title") == "Increment 1" for e in latest["elements"]):
            break
        time.sleep(.05)
    assert latest is not None
    assert any(e.get("title") == "Increment 1" for e in latest.get("elements", [])), latest
