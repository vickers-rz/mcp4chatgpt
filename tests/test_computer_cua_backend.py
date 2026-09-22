from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp4chatgpt import computer_cua_backend, computer_ops
from test_core import make_config


def test_parse_ax_state_exposes_cua_indices() -> None:
    state = (
        'Window: "Doc", App: TextEdit.\n'
        '0 standard window Doc\n'
        '\t1 scroll area\n'
        '\t\t2 text area (settable) Value: hello, ID: First Text View\n'
        '\nThe focused UI element is 2 text area\n'
    )
    elements, mapping = computer_cua_backend._parse_elements(state, "cua-snapshot:test")
    assert [item["cua_index"] for item in elements] == [0, 1, 2]
    text = elements[-1]
    assert text["depth"] == 2
    assert text["value"] == "hello"
    assert mapping[text["element_id"]] == 2


def test_cua_supports_only_cua_bound_action_tokens() -> None:
    assert computer_cua_backend.supports(
        "get_state", {"pid": None, "window_id": None}
    )
    assert computer_cua_backend.supports(
        "click", {"pid": None, "snapshot_id": "cua-snapshot:abc"}
    )
    assert not computer_cua_backend.supports(
        "click", {"pid": None, "snapshot_id": "native-snapshot"}
    )
    assert not computer_cua_backend.supports(
        "screenshot", {"pid": None, "window_id": "native-window"}
    )


def _config(tmp_path: Path):
    return replace(
        make_config(tmp_path),
        computer_mode="interact",
        computer_allowed_apps=("*",),
        computer_backend="auto",
    )


def test_auto_prefers_cua_for_observation(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with (
        patch.object(computer_ops.computer_cua_backend, "supports", return_value=True),
        patch.object(
            computer_ops.computer_cua_backend,
            "call",
            return_value={"status": "ok", "backend": "cua"},
        ) as cua,
        patch.object(computer_ops.computer_backend, "call") as native,
    ):
        result = computer_ops.get_state(cfg, "com.apple.TextEdit")
    assert result == {"status": "ok", "backend": "cua"}
    assert cua.call_count == 1
    native.assert_not_called()


def test_auto_falls_back_only_when_cua_proves_not_started(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with (
        patch.object(computer_ops.computer_cua_backend, "supports", return_value=True),
        patch.object(
            computer_ops.computer_cua_backend,
            "call",
            side_effect=computer_cua_backend.ComputerCUABackendError(
                "cua_codex_not_found", "not_started", fallback_allowed=True
            ),
        ),
        patch.object(
            computer_ops.computer_backend,
            "call",
            return_value={"status": "ok", "source": "native"},
        ) as native,
    ):
        result = computer_ops.get_state(cfg, "com.apple.TextEdit")
    assert result == {"status": "ok", "source": "native"}
    assert native.call_count == 1


def test_auto_never_bypasses_cua_approval_denial(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with (
        patch.object(computer_ops.computer_cua_backend, "supports", return_value=True),
        patch.object(
            computer_ops.computer_cua_backend,
            "call",
            side_effect=computer_cua_backend.ComputerCUABackendError(
                "cua_approval_required", "not_started", fallback_allowed=False
            ),
        ),
        patch.object(computer_ops.computer_backend, "call") as native,
    ):
        result = computer_ops.get_state(cfg, "com.apple.TextEdit")
    assert result["success"] is False
    assert result["error"] == "cua_approval_required"
    assert result["backend"] == "cua"
    native.assert_not_called()


def test_forced_cua_does_not_use_native_for_unsupported_operation(tmp_path: Path) -> None:
    cfg = replace(_config(tmp_path), computer_backend="cua")
    with (
        patch.object(computer_ops.computer_cua_backend, "supports", return_value=False),
        patch.object(computer_ops.computer_backend, "call") as native,
    ):
        result = computer_ops.permission_status(cfg)
    assert result == {
        "success": False,
        "error": "cua_operation_unsupported",
        "effect": "not_started",
        "backend": "cua",
        "recovery": "none",
    }
    native.assert_not_called()


def test_cua_detects_jpeg_dimensions_and_mime() -> None:
    # Minimal JPEG with a baseline SOF segment: width=20, height=10.
    jpeg = bytes.fromhex(
        "ffd8"
        "ffc0001108000a001403011100021100031100"
        "ffd9"
    )
    encoded = base64.b64encode(jpeg).decode()
    assert computer_cua_backend._image_metadata(encoded) == ("image/jpeg", (20, 10))


def test_effectful_cua_outcome_unknown_is_never_replayed(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    args = {
        "app_id": "com.apple.TextEdit",
        "window_id": "cua-window:w",
        "snapshot_id": "cua-snapshot:s",
        "element_id": "cua-element:s:2",
    }
    with (
        patch.object(computer_ops.computer_cua_backend, "supports", return_value=True),
        patch.object(
            computer_ops.computer_cua_backend,
            "call",
            side_effect=computer_cua_backend.ComputerCUABackendError(
                "cua_timeout", "outcome_unknown"
            ),
        ),
        patch.object(computer_ops.computer_backend, "call") as native,
    ):
        result = computer_ops.click(cfg, args)
    assert result["success"] is False
    assert result["error"] == "cua_timeout"
    assert result["effect"] == "outcome_unknown"
    assert result["backend"] == "cua"
    assert result["recovery"] == "observe_before_retry"
    native.assert_not_called()


def _native_windows(*, focused: str = "native-window:b"):
    return {
        "status": "ok",
        "app_id": "com.apple.TextEdit",
        "pid": 4242,
        "truncated": False,
        "windows": [
            {
                "window_id": "native-window:a",
                "title": "Same.txt",
                "minimized": False,
                "focused": focused == "native-window:a",
                "main": focused == "native-window:a",
            },
            {
                "window_id": "native-window:b",
                "title": "Same.txt",
                "minimized": False,
                "focused": focused == "native-window:b",
                "main": focused == "native-window:b",
            },
        ],
    }


def test_cua_multi_window_requires_exact_selection_before_sky() -> None:
    args = {
        "app_id": "com.apple.TextEdit",
        "window_id": None,
        "max_elements": 20,
        "allowed_apps": ["*"],
    }
    with (
        patch.object(computer_cua_backend, "_ensure_started"),
        patch.object(
            computer_cua_backend,
            "_native_call",
            return_value=_native_windows(),
        ),
        patch.object(computer_cua_backend, "_call_js") as sky,
    ):
        result = computer_cua_backend.call("get_state", args)

    assert result["status"] == "window_selection_required"
    assert result["backend"] == "cua"
    assert len(result["windows"]) == 2
    assert result["windows"][0]["window_id"] != result["windows"][1]["window_id"]
    sky.assert_not_called()


def test_cua_runtime_is_proven_before_window_inventory() -> None:
    args = {
        "app_id": "com.apple.TextEdit",
        "window_id": None,
        "max_elements": 20,
        "allowed_apps": ["*"],
    }
    with (
        patch.object(
            computer_cua_backend,
            "_ensure_started",
            side_effect=computer_cua_backend.CUABackendError(
                "cua_codex_unavailable",
                "not_started",
                fallback_allowed=True,
            ),
        ),
        patch.object(computer_cua_backend, "_native_call") as native,
    ):
        with pytest.raises(
            computer_cua_backend.CUABackendError,
            match="cua_codex_unavailable",
        ):
            computer_cua_backend.call("get_state", args)

    native.assert_not_called()


def test_cua_readonly_window_inventory_retries_one_not_running_transient() -> None:
    args = {"app_id": "com.apple.TextEdit", "allowed_apps": ["*"]}
    expected = _native_windows()
    with (
        patch.object(
            computer_cua_backend.computer_backend,
            "call",
            side_effect=[
                computer_cua_backend.computer_backend.ComputerBackendError(
                    "app_not_running",
                    "not_started",
                ),
                expected,
            ],
        ) as native,
        patch.object(computer_cua_backend.time, "sleep") as sleep,
    ):
        result = computer_cua_backend._native_call("list_windows", args)

    assert result == expected
    assert native.call_count == 2
    sleep.assert_called_once_with(0.03)


def test_cua_public_window_id_survives_native_helper_token_change() -> None:
    app_id = "com.apple.TextEdit"
    args = {"app_id": app_id, "allowed_apps": ["*"]}

    def inventory(native_token: str) -> dict:
        return {
            "status": "ok",
            "app_id": app_id,
            "pid": 4242,
            "truncated": False,
            "windows": [
                {
                    "window_id": native_token,
                    "window_number": 9001,
                    "title": "Same.txt",
                    "minimized": False,
                    "focused": True,
                    "main": True,
                    "on_screen": True,
                }
            ],
        }

    with patch.object(
        computer_cua_backend,
        "_native_call",
        side_effect=[
            inventory("native-window:first-helper"),
            inventory("native-window:second-helper"),
        ],
    ):
        _, first, _ = computer_cua_backend._window_inventory(args, app_id)
        _, second, _ = computer_cua_backend._window_inventory(args, app_id)

    assert first[0]["native_window_id"] != second[0]["native_window_id"]
    assert first[0]["window_number"] == second[0]["window_number"] == 9001
    assert first[0]["window_id"] == second[0]["window_id"]


def test_cua_window_identity_does_not_collide_for_same_title() -> None:
    args = {"app_id": "com.apple.TextEdit", "allowed_apps": ["*"]}
    with patch.object(
        computer_cua_backend,
        "_native_call",
        return_value=_native_windows(),
    ):
        pid, windows, truncated = computer_cua_backend._window_inventory(
            args, "com.apple.TextEdit"
        )

    assert pid == 4242
    assert truncated is False
    assert windows[0]["title"] == windows[1]["title"] == "Same.txt"
    assert windows[0]["window_id"] != windows[1]["window_id"]


def test_cua_exact_window_selection_drives_sky_observation() -> None:
    app_id = "com.apple.TextEdit"
    target_id = computer_cua_backend._window_id(
        app_id, 4242, "native-window:a"
    )
    args = {
        "app_id": app_id,
        "window_id": target_id,
        "max_elements": 20,
        "allowed_apps": ["*"],
    }
    native_results = [
        _native_windows(focused="native-window:b"),
        {
            "success": True,
            "effect": "completed",
            "app_id": app_id,
            "pid": 4242,
            "window_id": "native-window:a",
        },
        _native_windows(focused="native-window:a"),
    ]
    state = (
        'Window: "Same.txt", App: TextEdit.\n'
        "0 standard window Same.txt\n"
        "\t1 text area (settable) Value: A\n"
    )
    with (
        patch.object(computer_cua_backend, "_ensure_started"),
        patch.object(
            computer_cua_backend,
            "_native_call",
            side_effect=native_results,
        ) as native,
        patch.object(
            computer_cua_backend,
            "_call_js",
            return_value={"state": state},
        ) as sky,
    ):
        try:
            result = computer_cua_backend.call("get_state", args)
        finally:
            computer_cua_backend._snapshots.clear()

    assert result["status"] == "ok"
    assert result["pid"] == 4242
    assert result["window_id"] == target_id
    assert len(result["windows"]) == 2
    assert result["elements"][1]["value"] == "A"
    assert native.call_args_list[1].args[0] == "select_window_background"
    assert native.call_args_list[1].kwargs["effectful"] is True
    assert sky.call_count == 1


def test_cua_snapshot_fails_closed_after_window_focus_changes() -> None:
    app_id = "com.apple.TextEdit"
    window_id = computer_cua_backend._window_id(
        app_id, 4242, "native-window:a"
    )
    snapshot_id = "cua-snapshot:stale-window-test"
    element_id = f"cua-element:{snapshot_id}:2"
    computer_cua_backend._new_snapshot(
        app_id,
        window_id,
        {element_id: 2},
        "__binding",
        native_window_id="native-window:a",
        pid=4242,
        token=snapshot_id,
    )
    args = {
        "app_id": app_id,
        "window_id": window_id,
        "snapshot_id": snapshot_id,
        "element_id": element_id,
        "allowed_apps": ["*"],
    }
    with (
        patch.object(
            computer_cua_backend,
            "_native_call",
            return_value=_native_windows(focused="native-window:b"),
        ),
        patch.object(computer_cua_backend, "_call_js") as sky,
    ):
        try:
            with pytest.raises(
                computer_cua_backend.CUABackendError,
                match="stale_window",
            ):
                computer_cua_backend.call("click", args, effectful=True)
        finally:
            computer_cua_backend._snapshots.clear()

    sky.assert_not_called()


@pytest.mark.parametrize('response', [
    {'result': None},
    {'result': {'content': [{'type': 'text', 'text': 'output without completion marker'}]}},
    {'result': {'isError': True, 'content': [{'type': 'text', 'text': 'permission request failed during cleanup'}]}},
    {'result': {'isError': True, 'content': [{'type': 'text', 'text': '__mcp4_app_not_running__ after write'}]}},
])
def test_dispatched_write_with_bad_reply_remains_unknown(response):
    with (
        patch.object(computer_cua_backend, '_ensure_started'),
        patch.object(computer_cua_backend, '_send') as send,
        patch.object(computer_cua_backend, '_wait_response', return_value=response),
    ):
        with pytest.raises(computer_cua_backend.CUABackendError) as error:
            computer_cua_backend._call_js('type_keyboard', {}, 'return {};', effectful=True)
    assert error.value.effect == 'outcome_unknown'
    assert error.value.fallback_allowed is False
    assert send.call_count == 1
