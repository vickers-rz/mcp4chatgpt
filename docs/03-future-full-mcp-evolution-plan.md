# Future Full MCP Evolution Plan

Status: future direction. Do not implement inside the paste-only project by default.

## Goal

Keep the paste-only MCP small and safe while preserving a clear path toward a future full local-ops MCP for ChatGPT Web.

The future full MCP may include command execution, file operations, git operations, patching, and task delegation, but it should use a separate security model from the paste-only project.

## Recommended Evolution

Keep `chatgpt-terminal-paste-mcp` as the low-risk baseline:

- Paste only.
- No Return key.
- No command execution.
- No file system tools.
- No git tools.
- No delegate tools.
- Stable safety promise for daily use.

Build the full MCP as a separate project or a clearly separated profile:

```text
chatgpt-local-ops-mcp
```

Recommended default: separate project.

Reason: full local-ops capabilities have a different threat model, OAuth scope design, audit expectations, and failure impact.

## Reusable Pieces

Future full MCP can reuse these paste-only modules or implementation ideas:

- `macos.py`: frontmost app detection and activation.
- `clipboard.py`: clipboard preservation.
- `safety.py`: dangerous command classification.
- `oauth.py`: ChatGPT-compatible OAuth/Bearer Token support.
- `http_compat.py`: MCP HTTP, SSE, and discovery compatibility.
- README sections for ChatGPT Connector setup and HTTPS exposure.

Do not reuse the paste-only tool contract as an execution contract. Paste-only responses must continue to mean `executed=false` and `pressed_return=false`.

## Possible Future Tools

Future full local-ops MCP may add:

- `run_command_with_confirmation`
- `read_file`
- `write_file`
- `list_files`
- `git_status`
- `git_diff`
- `apply_patch`
- `task_queue_status`
- `delegate_task`

These tools must not be added to the paste-only service's default tool list.

## Security Model For Full MCP

Full MCP should require stronger controls:

- Separate service name and port.
- Separate ChatGPT connector.
- Separate OAuth client and token.
- Tool-level authorization scopes.
- Explicit confirmation for command execution and file writes.
- Workspace allowlist.
- Audit log with sensitive value redaction.
- Rate limits and request size limits.
- Optional local approval prompt before high-risk actions.

Recommended scopes:

- `terminal:paste`
- `terminal:execute`
- `files:read`
- `files:write`
- `git:read`
- `git:write`
- `tasks:delegate`

The paste-only project should only need the equivalent of `terminal:paste`.

## Explicit Separation Rules

Do not let full MCP work weaken paste-only guarantees:

- Do not add `press_return` to paste-only schemas.
- Do not add shell execution aliases to paste-only.
- Do not add filesystem access to paste-only.
- Do not add git or patch tools to paste-only.
- Do not overload `paste_to_terminal` to execute.
- Do not make `never_press_enter` configurable.

If both services run on the same Mac, expose them under different URLs:

```text
https://paste.example.com/mcp
https://local-ops.example.com/mcp
```

or:

```text
https://example.com/paste/mcp
https://example.com/local-ops/mcp
```

## Tests And Acceptance

Before starting full MCP implementation:

- Paste-only tests must remain green.
- Static scans must still reject `do script`, `write text`, and `key code 36` in the paste-only codebase.
- The paste-only ChatGPT connector must still expose only paste tools.

Full MCP acceptance should be separate:

- Commands run only after explicit confirmation.
- File operations are limited to configured workspaces.
- Git write operations require explicit user approval.
- High-risk operations are denied or require an additional local confirmation step.
- Audit logs are available without leaking secrets.

## Migration Path

Recommended sequence:

1. Implement and validate `chatgpt-terminal-paste-mcp`.
2. Extract shared HTTP/OAuth/macOS helper code only after the first version works.
3. Create `chatgpt-local-ops-mcp` as a separate project.
4. Add read-only full MCP tools first.
5. Add write and execute tools only with scopes, allowlists, and confirmation.
6. Keep both ChatGPT connectors installed separately so the user can choose the risk level per conversation.

