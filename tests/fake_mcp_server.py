#!/usr/bin/env python3
"""Fake stdio MCP server for automated testing.

Implements the minimal MCP JSON-RPC protocol over stdin/stdout:
  - initialize → returns server info + capabilities
  - notifications/initialized → accepted
  - tools/list → returns configurable fake tools
  - tools/call → returns configurable fake results or errors

Usage:
    python -m tests.fake_mcp_server [--fail-init] [--slow=N] [--exit-after=N]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

_JSONRPC = "2.0"

# Fake tools exposed by this server
FAKE_TOOLS = [
    {
        "name": "list_pages",
        "description": "List open browser pages/tabs.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
        "outputSchema": {
            "type": "object",
            "properties": {"pages": {"type": "array"}},
        },
    },
    {
        "name": "evaluate_script",
        "description": "Evaluate JavaScript in a page context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expression": {"type": "string"},
                "page_id": {"type": "string"},
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
    },
    {
        "name": "select_page",
        "description": "Select a page/tab by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string"},
            },
            "required": ["page_id"],
            "additionalProperties": False,
        },
    },
]


def _respond(request_id: int | str, result: Any) -> None:
    msg = {"jsonrpc": _JSONRPC, "id": request_id, "result": result}
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()


def _error(request_id: int | str | None, code: int, message: str) -> None:
    msg: dict[str, Any] = {
        "jsonrpc": _JSONRPC,
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()


def _handle_tools_call(request_id: int | str, params: dict[str, Any]) -> None:
    name = params.get("name", "")
    arguments = params.get("arguments", {})

    if name == "list_pages":
        pages = [
            {"id": "page-1", "title": "Example", "url": "https://example.com"},
            {"id": "page-2", "title": "Test", "url": "https://test.com"},
        ]
        _respond(request_id, {
            "content": [{"type": "text", "text": json.dumps(pages)}],
            "structuredContent": {"pages": pages},
        })
    elif name == "evaluate_script":
        expr = arguments.get("expression", "")
        if expr == "__large__":
            _respond(request_id, {
                "content": [{"type": "text", "text": "x" * 200_000}],
            })
        else:
            _respond(request_id, {
                "content": [{"type": "text", "text": json.dumps({
                    "result": f"evaluated: {expr}",
                    "type": "string",
                })}],
            })
    elif name == "select_page":
        _respond(request_id, {
            "content": [{"type": "text", "text": json.dumps({
                "selected": arguments.get("page_id", "unknown"),
            })}],
        })
    elif name == "error_tool":
        _error(request_id, -32000, "Simulated tool error")
    else:
        _error(request_id, -32601, f"Unknown tool: {name}")


def main() -> None:
    fail_init = "--fail-init" in sys.argv
    slow = 0.0
    tool_slow = 0.0
    exit_after = None
    eof_marker = None
    eof_delay = 0.0

    for arg in sys.argv[1:]:
        if arg.startswith("--slow="):
            slow = float(arg.split("=", 1)[1])
        elif arg.startswith("--tool-slow="):
            tool_slow = float(arg.split("=", 1)[1])
        elif arg.startswith("--exit-after="):
            exit_after = int(arg.split("=", 1)[1])
        elif arg.startswith("--eof-marker="):
            eof_marker = arg.split("=", 1)[1]
        elif arg.startswith("--eof-delay="):
            eof_delay = float(arg.split("=", 1)[1])

    calls = 0

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method", "")
        request_id = msg.get("id")
        params = msg.get("params", {})

        if slow > 0:
            time.sleep(slow)

        if method == "initialize":
            if fail_init:
                _error(request_id, -32000, "Simulated initialization failure")
                sys.exit(1)
            _respond(request_id, {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "fake-mcp-server",
                    "version": "1.0.0-test",
                },
            })

        elif method == "notifications/initialized":
            # Notification — no response
            pass

        elif method == "tools/list":
            _respond(request_id, {"tools": FAKE_TOOLS})

        elif method == "tools/call":
            if tool_slow > 0:
                time.sleep(tool_slow)
            _handle_tools_call(request_id, params)
            calls += 1
            if exit_after is not None and calls >= exit_after:
                sys.exit(0)

        elif request_id is not None:
            _error(request_id, -32601, f"Method not found: {method}")

    # stdin closed — simulate stateful child cleanup before clean exit.
    if eof_delay > 0:
        time.sleep(eof_delay)
    if eof_marker:
        Path(eof_marker).write_text("clean", encoding="utf-8")
    sys.exit(0)


if __name__ == "__main__":
    main()
