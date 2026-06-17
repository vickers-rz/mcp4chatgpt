# chatgpt-terminal-paste-mcp Paste-Only Plan

Status: recommended first implementation.

## Goal

Create a new MCP server project named:

```text
chatgpt-terminal-paste-mcp
```

Its only job is to let ChatGPT Web paste command text into the current macOS Terminal.app or iTerm2 input position.

It must never press Return, never execute commands, never append a newline, and never provide shell, file system, git, patch, or delegate tools.

## Architecture

```text
ChatGPT custom MCP app
        |
        | HTTPS
        v
chatgpt-terminal-paste-mcp
        |
        | FastMCP, streamable-http, OAuth or Bearer Token
        v
paste_to_terminal / paste_multiline_to_terminal
        |
        | Clipboard + Command+V
        v
Terminal.app / iTerm2 current input area
        |
        v
User reviews text and manually presses Return
```

The implementation must use clipboard paste instead of direct command execution.

## Reusable Sources

Reuse from `/Users/vickers/Documents/MCP_Creator/codex_work_with_apps`:

- `_escape_for_applescript`
- `redact`
- `run_osascript`
- Terminal.app and iTerm2 activation ideas.
- `SUPPORTED_APPS` style metadata.
- Dangerous command detection patterns.
- macOS Automation and Accessibility permission documentation.

Do not migrate from `codex_work_with_apps`:

- `run_terminal_command`
- AppleScript `do script`
- iTerm2 `write text`
- Existing `send_terminal_input`
- `press_return=true`
- Return, Enter, Ctrl-C, Ctrl-D, Ctrl-Z, or other special key paths.

Reuse from `catoncat/notion-local-ops-mcp`:

- Python + FastMCP project organization.
- uvicorn startup.
- `/mcp` endpoint.
- streamable-http transport.
- legacy SSE compatibility if needed for ChatGPT clients.
- OAuth / Bearer Token compatibility.
- `.well-known` metadata/discovery style.
- `.env` configuration.
- ChatGPT connector and tunnel documentation style.

Do not migrate from `notion-local-ops-mcp`:

- File system read/write.
- Shell execution.
- `run_command` or streaming command execution.
- Git tools.
- `apply_patch`.
- Delegate, Codex, Claude, or task queue features.
- Workspace arbitrary access.

## Project Structure

```text
chatgpt-terminal-paste-mcp/
├── pyproject.toml
├── README.md
├── SECURITY.md
├── .env.example
├── cloudflared-example.yml
├── scripts/
│   ├── dev.sh
│   └── dev-tunnel.sh
├── src/
│   └── chatgpt_terminal_paste_mcp/
│       ├── __init__.py
│       ├── config.py
│       ├── server.py
│       ├── http_compat.py
│       ├── oauth.py
│       ├── macos.py
│       ├── clipboard.py
│       ├── paste.py
│       └── safety.py
└── tests/
    ├── test_safety.py
    ├── test_clipboard.py
    └── test_tool_schema.py
```

## MCP Tools

Expose only these tools:

- `server_info`: return app name, version, `mode=paste_only`, `never_press_enter=true`, and available tools.
- `get_frontmost_terminal_info`: detect whether the frontmost app is Terminal.app or iTerm2 without reading terminal history.
- `paste_to_terminal`: paste a single-line command into the current input position.
- `paste_multiline_to_terminal`: paste a multi-line snippet only when `allow_multiline=true`.

No other MCP tools should be registered.

## Tool Behavior

`paste_to_terminal` parameters:

```json
{
  "text": "ls -la",
  "target_app": "auto",
  "acknowledge_risk": false
}
```

Rules:

- Reject empty text.
- Reject text containing `\n` or `\r`.
- Require frontmost Terminal.app or iTerm2 unless `target_app` selects a supported app that can be activated.
- Reject dangerous commands unless `acknowledge_risk=true`.
- Paste only, never press Return.

`paste_multiline_to_terminal` parameters:

```json
{
  "text": "cd ~/Project\nnpm test",
  "target_app": "auto",
  "allow_multiline": true,
  "acknowledge_risk": false,
  "preserve_trailing_newline": false
}
```

Rules:

- Require `allow_multiline=true`.
- Strip trailing newlines by default.
- Never append a newline.
- Reject dangerous content unless `acknowledge_risk=true`.
- Paste only, never press Return.

Both paste tools must return:

```json
{
  "executed": false,
  "pressed_return": false
}
```

## Safety Boundary

Hard guarantees:

- `never_press_enter=true` is hardcoded and not configurable.
- No `press_return` parameter exists in any schema.
- No `return`, `enter`, or control-key tool support exists.
- No AppleScript `do script`.
- No AppleScript `key code 36`.
- No iTerm2 `write text`.
- No shell execution tool.
- No file write tool.
- No git write tool.

Dangerous patterns include:

- `sudo`
- `rm -rf`
- `diskutil erase`
- `diskutil partition`
- `dd if=`
- `dd of=/dev/`
- `mkfs`
- `chmod -R 777 /`
- `chown -R`
- `curl ... | sh`
- `wget ... | sh`
- `launchctl`
- `killall`
- fork bomb syntax

Dangerous commands are rejected by default. With `acknowledge_risk=true`, they may be pasted but still must not be executed.

## Clipboard Strategy

Paste flow:

```text
1. Save current clipboard.
2. Write target text to clipboard.
3. Activate Terminal.app or iTerm2.
4. Send Command+V via System Events.
5. Wait for a short delay.
6. Restore original clipboard.
7. Do not send Return.
```

Default configuration:

```env
CHATGPT_TERMINAL_PASTE_RESTORE_CLIPBOARD=true
CHATGPT_TERMINAL_PASTE_CLIPBOARD_RESTORE_DELAY_MS=300
```

If clipboard restoration fails, return a warning without logging sensitive command text.

## Configuration

`.env.example` should include:

```env
CHATGPT_TERMINAL_PASTE_HOST=127.0.0.1
CHATGPT_TERMINAL_PASTE_PORT=8767
CHATGPT_TERMINAL_PASTE_AUTH_MODE=oauth
CHATGPT_TERMINAL_PASTE_AUTH_TOKEN=
CHATGPT_TERMINAL_PASTE_PUBLIC_BASE_URL=
CHATGPT_TERMINAL_PASTE_OAUTH_LOGIN_TOKEN=
CHATGPT_TERMINAL_PASTE_DEBUG_MCP_LOGGING=false
CHATGPT_TERMINAL_PASTE_RESTORE_CLIPBOARD=true
CHATGPT_TERMINAL_PASTE_CLIPBOARD_RESTORE_DELAY_MS=300
```

## ChatGPT Connection

README must document:

- Local startup with `uv`.
- HTTPS exposure via Cloudflare Tunnel, Secure MCP Tunnel, or direct IPv6 reverse proxy.
- `PUBLIC_BASE_URL` setup.
- ChatGPT Developer Mode app creation.
- OAuth selection and authorization.
- Bearer Token alternative.
- Testing `server_info`.
- Testing `paste_to_terminal`.
- How to tell whether ChatGPT Plus has disabled write tools.

## macOS Permissions

README must explain:

- System Settings -> Privacy & Security -> Accessibility.
- Allow the MCP server launcher process to control the computer.
- System Settings -> Privacy & Security -> Automation.
- Allow osascript, Python, or Terminal to control Terminal.app or iTerm2 if prompted.

The project must not require:

- Full Disk Access.
- Administrator privileges.
- `sudo`.

## Tests And Acceptance

Automated tests:

- Single-line safe command passes validation.
- Empty text is rejected.
- Multi-line text is rejected by `paste_to_terminal`.
- Multi-line text is allowed only with `allow_multiline=true`.
- Dangerous command is rejected by default.
- Dangerous command is allowed for paste only when `acknowledge_risk=true`.
- No tool schema includes `press_return`.
- Code does not contain `key code 36`.
- Code does not contain `do script`.
- Code does not contain `write text`.
- No forbidden tools are registered.
- Paste responses contain `executed=false` and `pressed_return=false`.

Manual acceptance:

- Server starts locally.
- `/mcp` is reachable.
- ChatGPT can list tools.
- `server_info` works.
- `paste_to_terminal` places text in Terminal.app or iTerm2 input area.
- The command does not execute automatically.
- No newline is appended.

