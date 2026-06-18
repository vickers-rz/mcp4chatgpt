# Browser Bridge Reference And Roadmap

This note records the code-level review of real-browser MCP projects that can
inform MCP4ChatGPT's Chrome context bridge.

## Current Baseline

MCP4ChatGPT currently exposes a small read-only Chrome context layer:

- `chrome_list_tabs`: AppleScript reads Chrome window/tab title and URL metadata.
- `chrome_get_active_tab_context`: AppleScript executes page JavaScript in the
  active tab to read title, URL, meta tags, selection, and visible body text.

This is useful as phase 0 because it needs no browser extension and already
works with ChatGPT over this server. Its limits are clear:

- It is macOS/Google Chrome specific.
- It needs Chrome's `Allow JavaScript from Apple Events` setting for page text.
- It cannot use Chrome extension APIs such as `captureVisibleTab`,
  `webRequest`, `debugger`, tab groups, or structured content-script state.
- It has no clean user-facing browser connection state.

The next step should not be another AppleScript-only expansion. The next step
should be an extension-backed browser bridge.

## Official Codex Chrome Extension Findings

Reviewed local install:

- Chrome extension ID: `hehggadaopoacecdllhhajmbjkdcmajg`
- Installed version on this Mac: `1.1.5`
- Native Messaging host: `com.openai.codexextension`
- Native host path:
  `~/.codex/plugins/cache/openai-bundled/chrome/latest/extension-host/macos/arm64/extension-host`
- Bundled browser client:
  `~/.codex/plugins/cache/openai-bundled/chrome/latest/scripts/browser-client.mjs`

The Codex extension is close to the desired architecture, but it is not a
drop-in library for MCP4ChatGPT:

- The extension manifest grants broad Chrome permissions, including
  `nativeMessaging`, `debugger`, `tabs`, `scripting`, and `history`.
- The native host manifest only allows the official extension origin:
  `chrome-extension://hehggadaopoacecdllhhajmbjkdcmajg/`.
- The extension connects to the native host with
  `chrome.runtime.connectNative("com.openai.codexextension")`.
- The bundled `browser-client.mjs` is intentionally guarded. A normal Node
  process fails during `setupBrowserRuntime` with:
  `privileged native pipe bridge is not available; browser-client is not trusted`.

So the supported reuse boundary is design/API reuse, not direct process-level
reuse inside this MCP server. Codex's open-source app-server code still gives us
the right context shape to emulate: `turn/start` and `turn/steer` accept
`additional_context`, and browser context is treated as untrusted model input.
For MCP4ChatGPT, the practical equivalent is to expose read-only MCP tools
named `browser_*` and keep page content clearly untrusted.

Useful Codex source references:

- `refer/codex-main/codex-rs/app-server-protocol/src/protocol/v2/turn.rs`:
  `additional_context` is an optional, client-provided map on turn requests.
- `refer/codex-main/codex-rs/app-server/src/request_processors/turn_processor.rs`:
  `map_additional_context` maps client entries into core untrusted/application
  context entries before submitting user input.
- `refer/codex-main/codex-rs/core/tests/suite/snapshots/all__suite__additional_context__additional_context_simple_input.snap`:
  shows browser info entering the model-visible request as
  `<external_browser_info>...`.

## Reviewed Projects

### BrowserMCP

Path reviewed: `refer/mcp`

Pattern:

```text
MCP client
  -> stdio MCP server
  -> local WebSocket server
  -> Chrome extension connected by user action
  -> current real browser tab
```

Useful files:

- `src/server.ts`: starts a WebSocket server, stores the active extension socket,
  and routes MCP `tools/call` to tool handlers.
- `src/context.ts`: centralizes the "no connected tab" error and request/response
  messages over the socket.
- `src/tools/snapshot.ts`: after click/type/hover/select actions, captures a
  fresh accessibility snapshot so the agent sees the new page state.
- `src/tools/custom.ts`: console logs and screenshot are normal MCP tools.

Takeaway:

This is the cleanest bridge shape for MCP4ChatGPT if we want fast integration
inside the existing Python HTTP server: add a local WebSocket endpoint and let a
Chrome extension connect to it. The repo says it is not independently buildable
because some types/utilities live in the upstream monorepo, so it is a design
reference more than a direct dependency.

### Algonius Browser MCP

Path reviewed: `refer/algonius-browser`

Pattern:

```text
MCP client
  -> Go MCP host over SSE
  -> Chrome Native Messaging
  -> Chrome extension background worker
  -> content scripts / Chrome APIs
```

Useful files:

- `mcp-host-go/cmd/mcp-host/main.go`: registers resources and tools, then starts
  the SSE MCP server and Native Messaging loop.
- `mcp-host-go/pkg/messaging/native_messaging.go`: length-prefixed Chrome Native
  Messaging protocol, RPC request tracking, timeouts.
- `chrome-extension/src/background/mcp/host-manager.ts`: extension side
  `chrome.runtime.connectNative("ai.algonius.mcp.host")`, heartbeat, structured
  host-not-found errors.
- `chrome-extension/src/background/index.ts`: registers browser RPC methods:
  `navigate_to`, `get_browser_state`, `get_dom_state`, `scroll_page`,
  `click_element`, `manage_tabs`, `type_value`.
- `mcp-host-go/pkg/resources/current_state.go` and `dom_state.go`: exposes
  browser state as MCP resources, not only tools.

Takeaway:

This is the closest match to a production native-host architecture. It is the
right reference if MCP4ChatGPT eventually wants a robust installed component
that Chrome can start and monitor. It is more work than a WebSocket bridge, but
it handles installation, lifecycle, and native-host failure modes in a way that
resembles the official Codex Chrome plugin architecture.

### YetiBrowser MCP

Path reviewed: `refer/yetibrowser-mcp`

Pattern:

```text
MCP client
  -> stdio MCP server
  -> local WebSocket bridge
  -> Chrome/Firefox extension
  -> connected real tab
```

Useful files:

- `packages/server/src/bridge.ts`: single active extension socket, request IDs,
  pending request map, timeouts, hello/event/result messages.
- `packages/server/src/context.ts`: snapshot history, formatted snapshot output,
  and snapshot diffing.
- `packages/server/src/tools.ts`: comprehensive tool set and schemas.
- `packages/shared/src/index.ts`: compact shared protocol definitions.

Good ideas to port:

- Keep `browser_connection_info` so the agent can diagnose whether a tab is
  connected before trying browser tools.
- Store recent snapshots and provide `browser_snapshot_diff`.
- Return structured text snapshots before images; images are expensive and
  should be requested explicitly.
- Provide `browser_page_state` only after a clear trust decision because it can
  expose form values, storage keys, and cookies.

Risk:

The README marks the project archived. Treat it as a transparent implementation
reference, not as a maintained dependency.

### SnapStack

Path reviewed: `refer/snapstack-extension`

Pattern:

```text
Browser extension
  -> captures visible/full/area screenshot
  -> POSTs image to local server
  -> MCP client reads screenshot manifest/path on demand
```

Useful files:

- `manifest.json`: minimal permissions: `activeTab`, `tabs`, `scripting`,
  `notifications`, `storage`, `alarms`, `clipboardWrite`, and localhost host
  permissions.
- `background.js`: uses `tabs.captureVisibleTab`, downscales/encodes images,
  posts them to `http://127.0.0.1:4123/push`, and supports visible, area, and
  full-page capture.

Takeaway:

This is not a browser automation bridge. It is the best reference for a visual
MVP: push screenshots to a local store, list them by manifest, and return file
paths instead of embedding large image bytes in every tool call.

### WebClaw

Path reviewed: `refer/webclaw`

Pattern:

```text
MCP client
  -> extraction MCP server
  -> HTTP fetch / extraction engine
```

Useful ideas:

- Clean text formats are first-class: markdown, text, LLM-optimized output, JSON.
- Separate fetch, extraction, cleanup, diff, brand, and summarization concerns.
- Keep local-first extraction independent from browser state.

Takeaway:

WebClaw is not the current-tab bridge. It is useful for post-processing HTML or
page text once MCP4ChatGPT has obtained it from Chrome.

### mcp-chrome

Path reviewed: `refer/mcp-chrome`

Pattern:

```text
MCP client
  -> HTTP/SSE or stdio native server
  -> Native Messaging host
  -> Chrome extension
  -> Chrome APIs, content scripts, CDP/debugger, IndexedDB/vector search
```

Useful files:

- `app/native-server/src/native-messaging-host.ts`: robust length-prefixed
  Native Messaging host with request IDs, size limits, and timeout tracking.
- `app/native-server/src/index.ts`: wires native host and MCP server together.
- `docs/TOOLS.md`: a broad mature tool taxonomy: window/tab management,
  screenshot, network capture, page reading, console, interaction, history, and
  bookmarks.
- `docs/ARCHITECTURE.md`: layered architecture with Native Server, extension,
  content scripts, offscreen docs, vector DB, and SIMD.

Takeaway:

This is the most feature-rich reference, but also the heaviest. It is useful for
tool taxonomy and advanced features, not for the first MCP4ChatGPT extension.

## Recommended MCP4ChatGPT Direction

### Phase 0: Keep The Current AppleScript Read-Only Bridge

Status: implemented.

Keep this as the fallback path:

- `chrome_list_tabs`
- `chrome_get_active_tab_context`

Use it when the user only needs quick active-tab context on this Mac.

### Phase 1: Add A Local WebSocket Extension Bridge

Recommended first extension-backed step.

Directory shape:

```text
browser_bridge/
  extension/
    manifest.json
    background.js
    content.js
    popup.html
  protocol.md
src/mcp4chatgpt/browser_bridge.py
src/mcp4chatgpt/browser_tools.py
```

Runtime shape:

```text
ChatGPT
  -> MCP4ChatGPT /mcp
  -> Python tool handler
  -> localhost WebSocket bridge inside MCP4ChatGPT
  -> extension
  -> active tab / chosen tab
```

Why WebSocket first:

- It avoids native-host install complexity.
- It can live inside the existing Python service lifecycle.
- It is enough for tab context, DOM snapshots, console logs, screenshots, and
  conservative interactions.
- It can still be replaced by Native Messaging later without changing public MCP
  tool names.

Initial tools:

- `browser_connection_info`
- `browser_list_tabs`
- `browser_current_tab`
- `browser_get_selection`
- `browser_get_page_text`
- `browser_get_links`
- `browser_snapshot`
- `browser_screenshot`
- `browser_console_errors`

Do not expose arbitrary JavaScript evaluation in phase 1.

### Phase 2: Add Controlled Interaction

Tools:

- `browser_scroll`
- `browser_click`
- `browser_type`
- `browser_press_key`
- `browser_wait_for`
- `browser_refresh`

Rules:

- Read tools are automatic.
- Mutating tools require ChatGPT/client confirmation and clear domain context.
- Prefer selectors or snapshot refs over raw coordinates.
- After every mutating tool, return a fresh snapshot or status summary.

### Phase 3: Native Messaging Host

Use Algonius and mcp-chrome as references.

Move from WebSocket to Native Messaging when we need:

- Better Chrome lifecycle integration.
- A browser popup that can start/stop/check the host.
- Cleaner install/uninstall scripts.
- A stronger boundary between browser extension and MCP server.
- A future packaged distribution.

## Tool Design Recommendations

Prefer `browser_*` names for the extension bridge. Keep `chrome_*` names for
the AppleScript fallback to make implementation scope obvious.

Good names:

- `browser_connection_info`
- `browser_list_tabs`
- `browser_snapshot`
- `browser_snapshot_diff`
- `browser_get_page_text`
- `browser_get_links`
- `browser_screenshot`
- `browser_console_errors`
- `browser_page_state`
- `browser_click`
- `browser_type`

Avoid early:

- `browser_evaluate`
- `browser_get_cookies`
- `browser_get_storage_values`
- `browser_history`
- `browser_bookmarks`
- raw network response-body capture

Those can leak sensitive data and should be behind explicit opt-in settings.

## Security Defaults

Treat all page content as untrusted.

Default-deny:

- Cookies.
- localStorage and sessionStorage values.
- Browser history.
- Bookmarks.
- Password fields.
- Arbitrary JS evaluation.
- Full network response bodies.

Default-allow:

- Active tab title and URL.
- Selected text.
- Visible body text with truncation.
- Links.
- Accessibility/DOM snapshot with values redacted.
- Console errors with token redaction.
- Screenshot manifest or local file path.

Use host allowlists before mutating operations. Keep read-only tools and
mutating tools separated in MCP annotations.

## Practical Next Step

The highest-value next implementation is Phase 1:

1. Add a WebSocket bridge to the Python service.
2. Add a minimal MV3 extension that connects to `ws://127.0.0.1:<port>`.
3. Implement read-only commands: connection info, list tabs, active tab text,
   links, snapshot, console errors, screenshot.
4. Keep `chrome_*` AppleScript tools as fallback.

This gives ChatGPT real Chrome tab context without committing yet to a native
host installer.
