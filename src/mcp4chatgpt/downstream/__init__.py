"""Downstream MCP aggregator.

The first implementation supports stdio MCP servers. The manager/config model
keeps transport explicit so HTTP can be added later without Chrome-specific
architecture leaking into the gateway core.
"""
