from __future__ import annotations

from mcp4chatgpt.tools import build_tools


def _definitions() -> dict[str, dict]:
    return {
        tool.name: tool.definition(auth_required=False)
        for tool in build_tools(computer_mode="off")
    }


def test_local_job_tool_contracts() -> None:
    tools = _definitions()

    start = tools["local_start_job"]
    assert start["inputSchema"]["required"] == ["operation_id", "command"]
    assert start["inputSchema"]["properties"]["timeout_sec"]["maximum"] == 86400
    assert start["annotations"]["readOnlyHint"] is False
    assert start["annotations"]["destructiveHint"] is True
    assert start["annotations"]["openWorldHint"] is True
    assert start["annotations"]["idempotentHint"] is True

    for name in ("local_job_status", "local_job_logs", "local_list_jobs"):
        definition = tools[name]
        assert definition["annotations"]["readOnlyHint"] is True
        assert definition["annotations"]["destructiveHint"] is False
        assert definition["annotations"]["idempotentHint"] is True

    cancel = tools["local_cancel_job"]
    assert cancel["annotations"]["readOnlyHint"] is False
    assert cancel["annotations"]["destructiveHint"] is True
    assert cancel["annotations"]["idempotentHint"] is True

    sync = tools["local_run_command"]
    timeout = sync["inputSchema"]["properties"]["timeout_sec"]
    assert timeout["minimum"] == 1
    assert timeout["maximum"] == 30

    write = tools["local_write_file"]
    write_props = write["inputSchema"]["properties"]
    assert "expected_sha256" in write_props
    assert "full_replace_token" in write_props
    assert write_props["allow_large_reduction"]["default"] is False
