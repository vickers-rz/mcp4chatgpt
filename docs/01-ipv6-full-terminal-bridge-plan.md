# ChatGPT Web IPv6 Full Terminal Bridge Plan

Status: early full-terminal direction. This is not the recommended first implementation.

## Goal

Build a ChatGPT Web-connectable MCP server that exposes controlled local terminal interaction on this Mac. ChatGPT would connect through a public HTTPS `/mcp` endpoint backed by the user's dynamic public IPv6 address and domain.

This plan is useful as background for a future full local-ops MCP, but it has a wider risk surface than the paste-only first version.

## Architecture

```text
ChatGPT Web custom MCP app
        |
        | HTTPS, domain AAAA record, public IPv6
        v
Reverse proxy on macOS, Caddy or nginx
        |
        | local HTTP MCP
        v
MCP4ChatGPT terminal bridge service
        |
        | AppleScript, JXA, System Events
        v
Terminal.app / iTerm2 / Termius
```

The service would listen locally and be exposed as:

```text
https://your-domain.example.com/mcp
```

## Reusable Sources

Reuse from `/Users/vickers/Documents/MCP_Creator/codex_work_with_apps`:

- Terminal.app, iTerm2, and Termius app metadata.
- AppleScript/JXA helpers for reading terminal context.
- `run_osascript` helper style.
- Secret redaction patterns.
- Dangerous command detection.
- Existing macOS Automation and Accessibility permission notes.

Reuse from `catoncat/notion-local-ops-mcp`:

- HTTP MCP endpoint shape.
- ChatGPT-compatible connector flow.
- OAuth/Bearer Token authentication ideas.
- `.env`-driven configuration.
- HTTPS tunnel and deployment documentation style.

## MCP Tools

Initial full-terminal bridge tools could include:

- `list_supported_apps`: return Terminal.app, iTerm2, and Termius support status.
- `get_app_context`: read recent front terminal context with secret redaction.
- `run_terminal_command`: send a single-line command to the front terminal tab.
- `send_terminal_input`: send text or limited special keys to an interactive prompt.

These tools are intentionally broader than the paste-only plan and should not be used as the first public-facing implementation unless the security model is strengthened.

## Explicitly Forbidden For First Version

Do not include these capabilities in the first implementation:

- Arbitrary shell execution through a hidden backend.
- File system read/write tools.
- Git write tools such as commit or push.
- `apply_patch`.
- Delegate or task-runner tools for Codex, Claude, or other agents.
- Workspace-wide local-ops agent behavior.

## IPv6 And DNS Deployment

Use the user's domain with an `AAAA` record pointing to the current public IPv6 address.

Recommended deployment pieces:

- DDNS updater for the AAAA record.
- Caddy or nginx on `[::]:443`.
- Valid TLS certificate for the domain.
- Reverse proxy from `/mcp` to the local MCP service.
- macOS firewall rules that only expose the intended HTTPS entrypoint.

If direct IPv6 inbound access is unstable, fall back to Cloudflare Tunnel, ngrok, or Secure MCP Tunnel.

## Security Boundary

Because this plan can interact with a terminal, it must not be exposed without authentication.

Required controls:

- OAuth or Bearer Token authentication.
- No unauthenticated public MCP calls.
- Dangerous command checks before sending anything to the terminal.
- Secret redaction for terminal context.
- Clear logs that avoid recording sensitive command text.
- macOS permissions limited to Automation and Accessibility, not Full Disk Access or sudo.

## Tests And Acceptance

Test locally:

- `list_supported_apps` returns supported apps.
- `get_app_context` reads Terminal.app or iTerm2 context and redacts tokens.
- Safe commands can be sent to the selected terminal.
- Dangerous commands such as `sudo`, `rm -rf`, and disk erase patterns are rejected.

Test through ChatGPT:

- ChatGPT Web can connect to `https://your-domain.example.com/mcp`.
- `server_info` or equivalent health tool works.
- Terminal context can be read after macOS permissions are granted.
- Terminal write tools behave visibly in the front terminal.

Acceptance criteria:

- Public HTTPS endpoint works over IPv6.
- Authentication is required.
- Only intended MCP tools are exposed.
- No file, git, patch, or delegate tools are present.

