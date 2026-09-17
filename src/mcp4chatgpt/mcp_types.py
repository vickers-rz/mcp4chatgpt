"""Small internal markers for preserving already-valid MCP protocol results."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RawMCPToolResult:
    """A tool result that is already in MCP ``tools/call`` result shape."""

    value: dict[str, Any]
