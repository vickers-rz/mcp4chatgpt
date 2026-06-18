# MCP4ChatGPT

ChatGPT Web-connectable MCP server for:

- local ops: files, commands, Git, and macOS terminal interaction
- web ops: Firecrawl-style search, scrape, crawl, map, extract, interact
- knowledge ops: NotebookLM-like local source library, search, citations, summaries, quizzes, and flashcards

The intended public connector URL is:

```text
https://mcp.runzhe.uk/mcp
```

`m6.ic2id.fun` is the older IPv6/DDNS endpoint. ChatGPT connector backends need
an IPv4-reachable public route, so the recommended deployment uses Cloudflare
Tunnel on `mcp.runzhe.uk`.

## Quick Start

```bash
cp .env.example .env
PYTHONPATH=src python3 -m unittest discover -s tests -v
scripts/dev.sh
```

The server defaults to `127.0.0.1:8766`. Use Cloudflare Tunnel for public HTTPS.

For the full architecture, request flow, data model, and deployment logic, read
`docs/05-architecture-and-logic.md`.

## Local Controller

The default local workflow is visible and manual, not a login background item.

Use the scripts directly:

```bash
scripts/start.sh
scripts/start_tunnel.sh
scripts/status.sh
scripts/stop_tunnel.sh
scripts/stop.sh
scripts/open_logs.sh
```

Or double-click:

```text
MCP4ChatGPT.command
```

The controller starts the service only when requested, writes a PID file at
`tmp.service.pid`, and stops the service when you choose Stop. It does not
install anything into Login Items.

Useful controller commands:

```bash
./MCP4ChatGPT.command status
./MCP4ChatGPT.command check
./MCP4ChatGPT.command tail
./MCP4ChatGPT.command rotate-logs
```

## Logs And Rotation

Runtime logs are written under `logs/`:

- `audit.jsonl`: HTTP, MCP request, and tool-call audit events.
- `service.out.log` / `service.err.log`: MCP server stdout/stderr when started through scripts.
- `cloudflared.out.log` / `cloudflared.err.log`: Cloudflare Tunnel output.
- `caddy.out.log` / `caddy.err.log`: retained for the older Caddy path.

`audit.jsonl` rotates automatically inside the MCP process when the day changes
or the file exceeds `MCP_LOG_ROTATE_BYTES` (default: 20 MB). Rotated audit logs
are compressed as `.jsonl.gz`.

Old rotated logs are archived by day into:

```text
logs/archive/YYYY-MM-DD.logs.tar.gz
```

The controller runs `scripts/rotate_logs.sh` during `start`, `status`, and
`check`. You can also run it manually:

```bash
./MCP4ChatGPT.command rotate-logs
```

Archive retention is controlled by `MCP_LOG_RETENTION_DAYS` (default: 30).

## ChatGPT Connector

Use OAuth authentication.

OAuth endpoints:

- `/.well-known/oauth-authorization-server`
- `/.well-known/oauth-protected-resource`
- `/oauth/register`
- `/oauth/authorize`
- `/oauth/token`

MCP endpoint:

- `/mcp`

During OAuth authorization, enter `MCP_AUTH_SECRET` on the local approval form.

## Tool Groups

- `local_*`: allowed-root file access, safe command execution, read-only Git, exact-text patching
- `terminal_*`: Terminal.app, iTerm2, and Termius context/input via `co-te.py`
- `web_*`: Firecrawl-backed search, scrape, crawl, map, extract, interact, and add-to-knowledge
- `knowledge_*`: local source library, chunk search, source fetch, summary, study guide, quiz, flashcards

`web_*` tools require `FIRECRAWL_API_KEY`. If it is missing, the tools remain visible but return `web_ops_not_configured`.

## Command Execution Modes

### Background Shell Mode

Tool:

- `local_run_command`

Characteristics:

- Runs in the background under an allowed cwd.
- Does not display in the current Terminal window.
- Returns stdout/stderr to ChatGPT.
- Writes a local execution log to `logs/commands.jsonl`.

Use `local_command_log_tail` to read recent background command logs through MCP.

### Visible Terminal Mode

Tools:

- `terminal_run_command`
- `terminal_send_input`

Characteristics:

- Writes into the front Terminal.app, iTerm2, or Termius tab.
- The user can see the command or input in the real terminal window.
- `terminal_run_command` sends a visible command and presses Return.
- `terminal_send_input` can set `press_return=false` to paste without executing.
- Best for explicit requests to run or paste something in the visible terminal.

## HTTPS Exposure

Recommended:

```bash
scripts/start.sh
scripts/start_tunnel.sh
```

Cloudflare Tunnel routes `https://mcp.runzhe.uk` to the local service at
`http://127.0.0.1:8766`. Its config is in
`deploy/cloudflared-mcp4chatgpt.yml`.

The Caddy configs for `m6.ic2id.fun` are retained only as a legacy IPv6/DDNS
deployment option, not the recommended ChatGPT Web path.

The included launchd plist is a manual template only. It is configured with
`RunAtLoad=false` and `KeepAlive=false` by default so it does not become a
login background item unless you explicitly install and modify it.

## Safety Notes

This is a full local-ops server. It can read/write allowed files and run allowed commands. Keep it behind OAuth and HTTPS. Set `MCP_ALLOWED_ROOTS` narrowly.

Terminal tools reuse `/Users/vickers/Documents/MCP_Creator/codex_work_with_apps/co-te.py`; macOS Automation and Accessibility permissions still apply to the process running this server.
