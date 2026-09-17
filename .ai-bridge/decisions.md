# Decisions

## 2026-09-12 — Phase 1 downstream MCP transport

- Keep `DownstreamMCPManager` generic and transport-explicit; Chrome DevTools remains configuration, not architecture.
- The repository currently has no official Python `mcp` SDK dependency and intentionally has a very small runtime dependency surface. Phase 1 therefore uses a deliberately narrow stdio JSON-RPC client that implements only `initialize`, `notifications/initialized`, `tools/list`, and `tools/call`, plus explicit rejection of unsupported server-initiated requests. It is not intended to become a full MCP client reimplementation.
- Revisit the official SDK if the gateway later needs broader MCP client capabilities (sampling, elicitation, roots, HTTP transport, richer cancellation/session semantics) or if protocol maintenance cost grows.
- Optional downstream startup/runtime failure degrades that integration and must not prevent native or Extension tools from operating.
- Chrome remains dual-channel: existing `ext_*` tools are preserved independently of `chrome_devtools__*`; no implicit fallback is introduced in Phase 1.

