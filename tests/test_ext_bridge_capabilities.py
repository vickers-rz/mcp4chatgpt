from __future__ import annotations

import json
import re
from pathlib import Path
from unittest import mock

from mcp4chatgpt import ext_bridge


ROOT = Path(__file__).resolve().parents[1]
BACKGROUND = ROOT / "src" / "chrome_extension" / "background.js"
MANIFEST = ROOT / "src" / "chrome_extension" / "manifest.json"


def test_extension_protocol_advertisement_matches_dispatch() -> None:
    source = BACKGROUND.read_text(encoding="utf-8")
    match = re.search(
        r"const SUPPORTED_COMMANDS = Object\.freeze\(\[(.*?)\]\);",
        source,
        flags=re.S,
    )
    assert match is not None
    advertised = set(re.findall(r'"([a-z_]+)"', match.group(1)))
    dispatched = set(re.findall(r'case "([a-z_]+)"\s*:', source))
    assert advertised == dispatched
    assert "get_element_state" in advertised
    assert "protocolRevision: BRIDGE_PROTOCOL_REVISION" in source
    assert "supportedCommands: SUPPORTED_COMMANDS" in source


def test_extension_manifest_bumped_for_protocol_advertisement() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["version"] == "1.0.5"


def test_normalize_extension_capability_advertisement() -> None:
    assert ext_bridge._normalize_protocol_revision(None) == 1
    assert ext_bridge._normalize_protocol_revision("2") == 2
    assert ext_bridge._normalize_protocol_revision(0) == 1
    assert ext_bridge._normalize_supported_commands(None) is None
    assert ext_bridge._normalize_supported_commands(
        ["list_tabs", "get_element_state", "list_tabs", "", 3]
    ) == ["list_tabs", "get_element_state"]


def test_command_support_distinguishes_legacy_missing_and_present() -> None:
    with mock.patch.object(ext_bridge, "_websocket", object()):
        with mock.patch.object(
            ext_bridge,
            "_client_info",
            {"supported_commands": None},
        ):
            assert ext_bridge.command_support("get_element_state") is None

        with mock.patch.object(
            ext_bridge,
            "_client_info",
            {"supported_commands": ["list_tabs"]},
        ):
            assert ext_bridge.command_support("get_element_state") is False

        with mock.patch.object(
            ext_bridge,
            "_client_info",
            {"supported_commands": ["list_tabs", "get_element_state"]},
        ):
            assert ext_bridge.command_support("get_element_state") is True
