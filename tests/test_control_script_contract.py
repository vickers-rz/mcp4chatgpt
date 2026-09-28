from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
CONTROL = ROOT / "MCP4ChatGPT.command"


def test_control_script_shell_syntax():
    result = subprocess.run(
        ["sh", "-n", str(CONTROL)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_safe_public_profile_is_explicit_and_non_full_access():
    text = CONTROL.read_text(encoding="utf-8")
    assert ('MCP_BIND_HOST="' + "$" + '{MCP_PUBLIC_BIND_HOST:-0.0.0.0}"') in text
    assert ('MCP_PUBLIC_BASE_URL="' + "$" + '{MCP_PUBLIC_PROFILE_BASE_URL:-https://mcp.runzhe.uk}"') in text
    assert ('MCP_EXTERNAL_TUNNEL="' + "$" + '{MCP_PUBLIC_EXTERNAL_TUNNEL:-1}"') in text
    assert 'MCP_COMPUTER_MODE="off"' in text
    assert 'MCP_PERSONAL_FULL_ACCESS="0"' in text
    assert 'start-public|public-start) restart_public_profile ;;' in text
    assert 'restart-public|public-restart) restart_public_profile ;;' in text

    start = text.index("restart_public_profile() {")
    end = text.index("\n}\n", start)
    body = text[start:end]
    assert "enable_public_profile" in body
    assert "stop_all" in body
    assert "start_all" in body


def test_full_access_profile_is_explicit_compact_and_personal():
    text = CONTROL.read_text(encoding="utf-8")
    assert 'MCP_PERSONAL_FULL_ACCESS="${MCP_FULL_PERSONAL_FULL_ACCESS:-1}"' in text
    assert 'MCP_TOOL_EXPOSURE="${MCP_FULL_TOOL_EXPOSURE:-compact}"' in text
    assert 'MCP_COMPUTER_MODE="${MCP_FULL_COMPUTER_MODE:-interact}"' in text
    assert 'MCP_COMPUTER_ALLOWED_APPS="${MCP_FULL_COMPUTER_ALLOWED_APPS:-*}"' in text
    assert 'restart-full|full-restart) restart_full_access ;;' in text
