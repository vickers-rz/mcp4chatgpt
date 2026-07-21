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
./MCP4ChatGPT.command cleanup
./MCP4ChatGPT.command clean-restart
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

## Codex/co-te Helper Cleanup

Codex and local app-control bridges can leave lightweight helper processes such
as `co-te.py` or `cua_node/bin/node_repl`. Audit them without killing anything:

```bash
./MCP4ChatGPT.command cleanup
```

The underlying script is intentionally dry-run by default. To terminate only
eligible candidates older than the age threshold:

```bash
scripts/cleanup_codex_co_te.sh --kill --min-age-sec 1800
```

It only targets the known `co-te.py` and Codex `node_repl` helper paths, and it
skips helpers descended from the current MCP service.

For a full one-command refresh, stop this MCP service, clear all known
Codex/co-te helpers, and start the MCP service again:

```bash
./MCP4ChatGPT.command clean-restart
```

`restart-clean` is accepted as an alias. This is intentionally separate from
plain `restart`, which only restarts the MCP service and Cloudflare Tunnel.

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

## Open WebUI

Open WebUI can use the same public MCP endpoint through its native MCP external
tool support:

- Type: `MCP (Streamable HTTP)`
- URL: `https://mcp.runzhe.uk/mcp`
- Auth: `OAuth 2.1`

Do not add this endpoint as an OpenAPI tool server. The `/mcp` route speaks
JSON-RPC MCP over Streamable HTTP; OpenAPI compatibility would require a
separate `mcpo` proxy.

## Tool Groups

- `local_*`: allowed-root file access, safe command execution, read-only Git, exact-text patching
- `terminal_*`: compatibility tools for co-te terminal context/input and visible terminal commands
- `app_*`: co-te macOS app context reads and Accessibility-backed text writeback
- `apple_notes_*`: read-only Apple Notes SQLite inspection, listing, reading, and search through co-te
- `chrome_*` / `browser_*`: lightweight Google Chrome fallback via local AppleScript; no extension required
- `ext_*`: enhanced Chrome tab context and interaction through the optional unpacked Chrome extension
- `web_*`: Firecrawl-backed search, scrape, crawl, map, extract, interact, and add-to-knowledge
- `knowledge_*`: local source library, chunk search, source fetch, summary, study guide, quiz, flashcards

`web_*` tools require `FIRECRAWL_API_KEY`. If it is missing, the tools remain visible but return `web_ops_not_configured`.

## Chrome Context

The browser capability is layered:

- `browser_*` and `chrome_*` are the stable fallback. They read the front Google Chrome tab through local AppleScript and keep working even when no extension is installed.
- `ext_*` uses `src/chrome_extension/` plus a local WebSocket bridge on `127.0.0.1:8765`. It adds real tab listing, DOM reads, selection reads, screenshots, navigation, clicking, form filling, opt-in JavaScript execution, and short page-change listening.

Install the Python dependencies in the service environment so the bridge can import `websockets`:

```bash
.venv/bin/python -m pip install -e .
```

Start the service, then load the unpacked extension from:

```text
src/chrome_extension
```

The extension popup needs a bridge token. It is not written into this README
because it is derived from the local `.env` `MCP_AUTH_SECRET`. Copy the current
token to the macOS clipboard with:

```bash
scripts/extension_token.sh
```

Then paste it into the extension popup's token field.

To print the token manually instead, run:

```bash
source .env
PYTHONPATH=src .venv/bin/python - <<'PY'
from mcp4chatgpt.ext_bridge import _derive_token
import os
print(_derive_token(os.environ["MCP_AUTH_SECRET"]))
PY
```

By default screenshots are saved under `data/screenshots/` and MCP returns the
file path instead of embedding the full image payload. `ext_run_js` remains
disabled until you explicitly enable "Allow JS execution" in the extension
popup.

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

This is a full local-ops server. It can read/write allowed files and run allowed commands. Keep it behind OAuth and HTTPS. Set `MCP_ALLOWED_ROOTS` narrowly. The HTTP server rejects unknown `Host` headers by default; use `MCP_ALLOWED_HOSTS` only for additional trusted reverse-proxy hostnames.

co-te tools reuse `/Users/vickers/Documents/MCP_Creator/codex_work_with_apps/co-te.py`; macOS Automation, Accessibility, and Apple Notes Full Disk Access permissions still apply to the process running this server.
