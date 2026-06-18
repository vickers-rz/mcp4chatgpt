# MCP4ChatGPT Architecture And Logic

This document explains how the project is wired and how a ChatGPT Web request
travels through the local service.

## Goals

MCP4ChatGPT is a ChatGPT Web connector backend for three related workflows:

- `local_ops`: operate on this Mac through bounded file, command, Git, and terminal tools.
- `web_ops`: provide Firecrawl-style web search, scrape, crawl, map, extract, and interaction tools.
- `knowledge_ops`: maintain a local NotebookLM-like source library with citations, chunk search, fetch, and learning-material helpers.

The public target URL is:

```text
https://mcp.runzhe.uk/mcp
```

Cloudflare Tunnel routes that public HTTPS endpoint to the local service on
`127.0.0.1:8766`. The older `m6.ic2id.fun` IPv6/DDNS + Caddy path is retained
only as a legacy fallback, not the recommended deployment.

## Runtime Topology

```text
ChatGPT Web
  |
  | HTTPS + OAuth bearer token
  v
mcp.runzhe.uk
  |
  | Cloudflare Tunnel
  v
MCP4ChatGPT HTTP server on 127.0.0.1:8766
  |
  +-- local_ops: files, commands, Git
  +-- terminal_ops: co-te.py -> Terminal.app / iTerm2 / Termius
  +-- web_ops: Firecrawl HTTP API
  +-- knowledge_ops: local JSON source store
```

The service is intentionally a backend. ChatGPT remains the conversational UI,
reasoning layer, and answer composer.

## Request Flow

1. ChatGPT discovers OAuth metadata from `/.well-known/oauth-authorization-server`.
2. ChatGPT dynamically registers a client at `/oauth/register`.
3. The user opens `/oauth/authorize`, enters `MCP_AUTH_SECRET`, and approves.
4. ChatGPT exchanges the authorization code at `/oauth/token`.
5. ChatGPT calls `/mcp` with `Authorization: Bearer <token>`.
6. `server.py` validates the token, routes JSON-RPC methods, and delegates tool calls to `ToolRegistry`.
7. `tools.py` dispatches the tool to the correct subsystem.
8. The subsystem returns structured data, which is wrapped as MCP text content plus `structuredContent`.

## Public HTTP Endpoints

- `GET /health`: local health check.
- `GET /.well-known/oauth-authorization-server`: OAuth authorization server metadata.
- `GET /.well-known/oauth-protected-resource`: protected resource metadata used by bearer challenges.
- `POST /oauth/register`: dynamic OAuth client registration.
- `GET /oauth/authorize`: local approval page.
- `POST /oauth/authorize`: validates `MCP_AUTH_SECRET` and redirects back with an authorization code.
- `POST /oauth/token`: exchanges authorization code for a signed bearer token.
- `POST /mcp`: JSON-RPC endpoint for MCP methods.

Supported MCP methods:

- `initialize`
- `notifications/initialized`
- `tools/list`
- `tools/call`

## Tool Registry

All tool schemas and handlers are declared in `src/mcp4chatgpt/tools.py`.
The registry is the only place the HTTP layer knows about individual tools.

Tool names are prefixed by capability:

- `local_*`: local file, command, Git, and patch tools.
- `terminal_*`: macOS terminal/app interaction tools.
- `web_*`: Firecrawl-backed web tools.
- `knowledge_*`: local source-library tools.
- `server_info`: backend status and configuration summary.

This prefix split is deliberate: it helps ChatGPT choose the right capability
and makes future authorization scopes easier to add.

## Local Ops

`local_ops.py` handles local files, commands, patching, and read-only Git.

Safety rules:

- Every path is resolved to a real absolute path.
- Paths must live under `MCP_ALLOWED_ROOTS`.
- Command execution validates obvious dangerous command patterns before running.
- Command output is redacted and truncated.
- `local_run_command` writes redacted execution records to `logs/commands.jsonl`.
- Git tools are read-only in v1.

`local_apply_patch` is intentionally an exact-text replacement instead of a
general patch parser. It is easy to audit and avoids surprising multi-file edits.

## Terminal Ops

`terminal_ops.py` dynamically loads:

```text
/Users/vickers/Documents/MCP_Creator/codex_work_with_apps/co-te.py
```

That existing project owns the macOS-specific logic:

- Terminal.app history reading.
- iTerm2 session reading.
- Termius guarded context/input.
- AppleScript/System Events calls.
- Secret redaction.
- Dangerous visible-command checks.

MCP4ChatGPT wraps those functions rather than copying them. macOS Automation and
Accessibility permissions still apply to the Python process running this server.

## Web Ops

`web_ops.py` is a thin Firecrawl adapter. It does not implement a crawler or
browser cluster locally.

Tools map to Firecrawl-style endpoints:

- `web_search` -> `/v2/search`
- `web_scrape` -> `/v2/scrape`
- `web_crawl` -> `/v2/crawl`
- `web_map` -> `/v2/map`
- `web_extract` -> `/v2/extract`
- `web_interact` -> scrape plus interact on the returned scrape/session id when available
- `web_add_to_knowledge` -> scrape a page and ingest markdown into the knowledge store

If `FIRECRAWL_API_KEY` is not configured, web tools remain listed but fail with
`web_ops_not_configured`. This keeps the schema stable while making missing
configuration explicit.

## Knowledge Ops

`knowledge_ops.py` implements a local source library.

Each source record stores:

- `source_id`
- title
- path or URL
- metadata
- content hash
- full text
- chunks with `chunk_id`, offsets, and text

The source id is deterministic from source reference and content hash. Re-adding
the same content replaces the same record.

Current retrieval is simple token-overlap chunk search. It is enough for the v1
NotebookLM-like workflow:

1. Add sources from files, pasted text, or scraped web pages.
2. Search chunks for a question.
3. Fetch the original source or chunk context.
4. Let ChatGPT compose the final answer with citations.

This is not a vector database yet. The tool contract is designed so the backend
can later be replaced by embeddings without changing ChatGPT-facing tool names.

## Data And Logs

Default paths:

- OAuth clients: `data/oauth_clients.json`
- Knowledge store: `data/knowledge/sources.json`
- Audit log: `logs/audit.jsonl`
- Background command log: `logs/commands.jsonl`

Audit events include HTTP activity and tool-call success/failure. They do not
record OAuth tokens or `terminal_send_input` sensitive text. Background command
logs redact obvious secrets before writing command text, stdout, and stderr.

## OAuth Security Notes

- Authorization codes expire after 10 minutes.
- Expired authorization codes are cleaned from memory before creating or
  exchanging codes.
- Pending authorization codes are capped to avoid unbounded memory growth.
- If a code challenge is present, `/oauth/token` requires a matching
  `code_verifier`; omitting it is rejected.

## Configuration

Primary configuration lives in `.env`.

Important variables:

- `MCP_PUBLIC_BASE_URL`: public origin, expected to be `https://mcp.runzhe.uk`.
- `MCP_BIND_HOST` / `MCP_BIND_PORT`: local listener, default `127.0.0.1:8766`.
- `MCP_ALLOWED_HOSTS`: optional comma-separated additions to the HTTP `Host`
  header allowlist. Defaults already include `localhost`, `127.0.0.1`, `::1`,
  `MCP_BIND_HOST`, and the hostname from `MCP_PUBLIC_BASE_URL`.
- `MCP_AUTH_SECRET`: local approval secret and token signing key.
- `MCP_ALLOWED_ROOTS`: roots allowed for file, command, Git, and patch tools.
- `MCP_CO_TE_PATH`: path to the reusable macOS terminal backend. If unset, the
  default is the sibling path `../codex_work_with_apps/co-te.py` relative to
  this project directory.
- `FIRECRAWL_API_KEY`: enables web tools.
- `KNOWLEDGE_ROOTS`: roots allowed for adding local knowledge sources.
- `KNOWLEDGE_STORE_DIR`: local source-library data directory.

## Deployment

Recommended deployment:

1. Run `MCP4ChatGPT.command` or `scripts/start.sh` manually while testing.
2. Run `scripts/start_tunnel.sh` to start the named Cloudflare Tunnel.
3. Cloudflare routes `https://mcp.runzhe.uk` to `127.0.0.1:8766`.
4. Create or update the ChatGPT Web connector with OAuth and MCP URL `https://mcp.runzhe.uk/mcp`.

`deploy/cloudflared-mcp4chatgpt.yml` is the active public-exposure template.
`deploy/Caddyfile.example` is retained only for the older `m6.ic2id.fun`
IPv6/DDNS Caddy path.
`deploy/com.vickers.mcp4chatgpt.plist` is a starter template.
The launchd template is intentionally not auto-starting; turn on `RunAtLoad`
and `KeepAlive` only after you accept the background-service risk.

Recommended daily workflow:

- Start: `scripts/start.sh` or menu option 1 in `MCP4ChatGPT.command`.
- Start tunnel: `scripts/start_tunnel.sh`.
- Check: `scripts/status.sh` or menu status.
- Stop tunnel: `scripts/stop_tunnel.sh`.
- Stop: `scripts/stop.sh` or menu option 2.
- Logs: `scripts/open_logs.sh` or menu option 4.

## Background Service Risk

If installed as a login item, this service keeps a Python process running in
the background. That process listens on the configured local port and exposes
the MCP tool surface to whatever HTTPS reverse proxy you put in front of it.

Operational impact:

- It consumes a small amount of memory while idle.
- A crash loop can create repeated process launches.
- macOS may show it under Login Items as `python3` because Python is the launcher.

Security impact:

- If reverse-proxied publicly, OAuth and HTTPS become mandatory.
- Unknown `Host` headers are rejected before route handling; keep
  `MCP_ALLOWED_HOSTS` limited to trusted public/reverse-proxy names.
- `MCP_ALLOWED_ROOTS` should remain narrow.
- Firecrawl keys and OAuth client data live under the configured project data directory.
- Terminal tools may trigger macOS Automation/Accessibility prompts and should not be enabled casually as an always-on service.

## Current Limits

- No audio/video overview generation.
- No custom crawler/browser infrastructure.
- No Git write operations.
- Knowledge retrieval is lexical, not embedding-based.
- The HTTP MCP transport is a minimal JSON-RPC implementation, not a full SDK-generated transport.
