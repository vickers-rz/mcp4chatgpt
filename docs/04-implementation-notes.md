# MCP4ChatGPT Implementation Notes

This implementation is a first full-ops backend for ChatGPT Web, not a paste-only server.

## Implemented

- OAuth-protected HTTP MCP endpoint at `/mcp`.
- OAuth discovery, protected resource metadata, dynamic registration, authorize, and token endpoints.
- Local file, command, Git, and exact-text patch tools.
- Terminal tools that load `/Users/vickers/Documents/MCP_Creator/codex_work_with_apps/co-te.py`.
- Firecrawl-style web tools with explicit `web_ops_not_configured` errors when no API key is set.
- Local NotebookLM-like knowledge store with source add/list/search/fetch and learning material generation.
- Unit and HTTP integration tests.

## Not Implemented In v1

- Audio Overview or video generation.
- Custom browser cluster or crawler infrastructure.
- Git commit/push tools.
- Full vector embeddings. Current knowledge search is local token overlap over chunks.
- Production UI. ChatGPT is the intended UI.

## Deployment Shape

```text
ChatGPT Web
  -> https://m6.ic2id.fun/mcp
  -> Caddy/nginx on macOS [::]:443
  -> mcp4chatgpt on 127.0.0.1:8766
```

`ddns-go` already maintains the AAAA record for `m6.ic2id.fun`.

