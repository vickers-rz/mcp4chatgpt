"""Compile-only regression test for the native macOS Computer Use helper."""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS Swift helper")
def test_computer_use_swift_helper_typechecks() -> None:
    source = Path(__file__).parents[1] / "src" / "mcp4chatgpt" / "native" / "ComputerUseHelper.swift"
    result = subprocess.run(
        ["xcrun", "swiftc", "-typecheck", str(source)],
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
