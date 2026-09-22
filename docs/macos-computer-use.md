# macOS Computer Use

MCP4ChatGPT now uses a two-tier macOS Computer Use architecture:

```text
computer_* MCP tools
  -> Computer backend router
     -> primary: OpenAI CUA / Sky through Codex cua_repl
     -> fallback: MCP4ChatGPT native Swift helper
```

The primary goal is background computer use: CUA/Sky can operate an allowlisted application without moving the user's physical pointer or forcing that application to the foreground. The previous native implementation remains available as a fallback for operations CUA does not currently cover, for environments where CUA is unavailable, and for explicit low-level desktop-pointer work.

## Configuration

Computer Use remains opt-in at the capability level. The generic development and service launchers now default to localhost with Computer Use disabled:

```text
MCP_BIND_HOST=127.0.0.1
MCP_COMPUTER_MODE=off
MCP_COMPUTER_BACKEND=auto
```

For the reviewed single-user deployment that intentionally needs the historical full-desktop behavior, use an explicit full-access profile:

```text
./MCP4ChatGPT.command start-full
# or:
./scripts/dev-full-access.sh
```

That profile explicitly sets `MCP_COMPUTER_MODE=interact`, `MCP_COMPUTER_ALLOWED_APPS=*`, and `MCP_COMPUTER_BACKEND=auto`. It also restores the project's public listener/tunnel settings. `MCP_FULL_*` environment variables can override each full-access profile value.

`MCP_COMPUTER_MODE` accepts `off`, `observe`, or `interact`. An enabled mode still requires an explicit app allowlist. A literal `*` allows every running application with a bundle identifier and is also required for desktop-wide display/pointer tools.

`MCP_COMPUTER_BACKEND` accepts:

- `auto`: prefer OpenAI CUA/Sky for supported app-bound operations and fall back to the native helper only when CUA proves that no side effect started.
- `cua`: require CUA/Sky and do not fall back.
- `native`: use only the existing Swift/Accessibility/ScreenCaptureKit/Quartz implementation.

`load_config()` defaults the backend selector to `auto`, but `scripts/dev.sh`, `scripts/start.sh`, and the ordinary `MCP4ChatGPT.command start` profile now default `MCP_COMPUTER_MODE=off`. The explicit `start-full` / `restart-full` and `dev-full-access.sh` profiles opt back into `interact + * + auto`. The dataclass default remains `native` so isolated tests and callers that manually construct `Config` preserve the historical behavior unless they opt into routing.

## CUA primary path

The CUA adapter lives in `src/mcp4chatgpt/computer_cua_backend.py`. It discovers the effective Codex `cua_repl` transport through `codex mcp get cua_repl --json`, starts that MCP server, negotiates MCP elicitation support, and sends Codex turn metadata required by the OpenAI Computer Use runtime.

Only low-risk internal Computer Use elicitations are auto-accepted by the adapter. Any higher-risk or non-Computer-Use elicitation is declined rather than silently approved.

The first CUA-backed operations are:

- `computer_list_apps`
- `computer_get_state`
- `computer_screenshot`
- `computer_launch_app`
- `computer_click`
- `computer_type_text`
- `computer_type_keyboard`
- `computer_press_key`

CUA observations are translated into the existing MCP4ChatGPT action model. They receive opaque `cua-window:...`, `cua-snapshot:...`, and `cua-element:...` identifiers. A CUA snapshot can only be consumed by the CUA adapter; it is never passed to the native helper. Likewise, a native snapshot never becomes a CUA action target.

The snapshot fence is still 45 seconds. A state-changing action consumes its CUA snapshot even when the result becomes uncertain, matching the native helper's observe -> one effect -> observe-again discipline.

### Multi-window hybrid targeting

OpenAI's current macOS `cua.getApp()` / `computer.get_app_state` contract is app-scoped and does not expose a public window selector. MCP4ChatGPT therefore separates **window identity/selection** from **Sky observation/action** instead of pretending that a title hash is a real window identity.

The native helper inventories substantial layer-0 WindowServer roots and pairs them with AXWindow objects. Public CUA window identity prefers `PID + CGWindowNumber`; the helper's process-local `native-window:UUID` is kept private as the current AX binding. This prevents same-title collisions and keeps public CUA window tokens stable when the native helper process itself restarts.

When an app has multiple eligible windows, an unqualified `computer_get_state` returns `window_selection_required` before asking Sky to inspect a window. Once a CUA window token is selected, the native helper changes only the target application's internal main/focused window through Accessibility, verifies that the system frontmost application did not change, and only then lets Sky observe or act. The combined `select -> Sky call -> verify` sequence is serialized under the CUA adapter lock.

Each CUA snapshot stores the exact PID and private native window binding. Before a state-changing action, the adapter revalidates that process/window and requires it still to be the selected internal target. If it changed, the action fails closed before Sky receives the effectful request. A short retry exists only for a read-only `list_windows` `app_not_running/not_started` transient; no effectful operation is automatically replayed.

The CUA runtime itself is also proven available before any CUA-bound window token is minted. Therefore `backend=auto` can still fall back cleanly to native when Codex/CUA is unavailable, while forced `backend=cua` reports the CUA startup failure.

### Text input

For a CUA-bound snapshot, `computer_type_keyboard` intentionally maps to Sky's background `app.paste(..., {format:"text"})` path rather than synthetic Quartz Unicode events. On macOS, Sky temporarily uses the system pasteboard and restores the user's previous pasteboard contents afterward. This is the preferred general text-entry path because it does not require the target app to become foreground and does not depend on the user's active input method.

`computer_type_text` continues to mean value replacement. CUA uses `setValue`; the native fallback uses AXValue.

## Native fallback

The native helper remains at:

```text
~/Library/Application Support/MCP4ChatGPT/ComputerUse/
  MCP4ChatGPT Computer Use.app
```

Bundle identifier:

```text
uk.runzhe.mcp4chatgpt.computeruse
```

It continues to provide Accessibility inspection, ScreenCaptureKit window/display capture, application launch, foreground window activation, AXPress, AXValue replacement, Quartz keyboard events, and raw pointer move/click/drag/scroll.

The following operations currently remain native-first because their existing semantics are desktop-wide or explicitly foreground-oriented:

- `computer_permissions`
- `computer_request_permissions`
- `computer_list_displays`
- `computer_screenshot_display`
- `computer_activate_window`
- `computer_pointer_move`
- `computer_pointer_click`
- `computer_pointer_drag`
- `computer_pointer_scroll`

The native helper is still signed as a stable background app so macOS TCC permissions remain attached to a stable code identity.

## Fallback and replay safety

The router never blindly retries state-changing work.

For `auto` mode:

1. CUA is tried first only for operations it supports.
2. If CUA fails with `effect=not_started` before a CUA snapshot/window has bound the request, the router may fall back to native.
3. If CUA reports `effect=outcome_unknown`, the native backend is not invoked.
4. If a request contains a `cua-snapshot:` or `cua-window:` token, it stays on CUA and is never replayed through native.
5. After any successful state-changing operation, callers must observe again before the next action.

This preserves the existing `completed` / `not_started` / `outcome_unknown` contract and avoids double-clicks or duplicate text insertion during backend failures.

## Validation on this Mac

The OpenAI CUA path was tested directly through the local Codex/ChatGPT runtime, not inferred from configuration.

A background TextEdit click was executed while Google Chrome remained the frontmost application. During the 492 ms click operation, the physical system pointer was sampled 121 times; every sample remained at the same coordinates and the maximum observed pointer step was 0 px. Chrome remained frontmost throughout.

A second background TextEdit test performed `selectText -> paste -> getAXState` while Chrome stayed frontmost. The physical pointer remained unchanged, the original macOS pasteboard hash before and after the operation was identical, and the inserted marker was visible in the refreshed TextEdit Accessibility state.

The production adapter itself then passed a separate end-to-end test:

```text
computer_cua_backend.list_apps
  -> computer_cua_backend.get_state
  -> computer_cua_backend.type_keyboard
     strategy = CUABackgroundPaste
  -> computer_cua_backend.get_state
     marker verified
```

The formal ChatGPT connector path was also validated after restarting MCP4ChatGPT with `MCP_COMPUTER_BACKEND=auto`:

```text
ChatGPT computer_list_apps
  -> backend = cua

ChatGPT computer_get_state(TextEdit)
  -> cua-window / cua-snapshot / cua-element identifiers
  -> backend = cua
  -> background_capable = true

ChatGPT computer_type_keyboard(TextEdit)
  -> strategy = CUABackgroundPaste
  -> backend = cua
  -> background = true

ChatGPT computer_get_state(TextEdit)
  -> inserted E2E marker verified
  -> Google Chrome remained frontmost

ChatGPT computer_screenshot(TextEdit)
  -> backend = cua
  -> image/jpeg, 586 x 476
```

An explicit `pid`-bound TextEdit observation was then issued through the same public `computer_get_state` tool. The router deliberately selected the native backend and returned native UUID window/snapshot/element identifiers, confirming that the caller-visible PID identity fence remains intact.

The hybrid multi-window path was subsequently validated through the formal ChatGPT connector with three live TextEdit windows. An unqualified `computer_get_state` returned `window_selection_required` with three distinct CUA window tokens plus native-backed `window_number`, focus/main state, bounds, and on-screen metadata. Selecting the initially non-focused `MCP4_CUA_MULTI_B.txt` caused the native helper to switch TextEdit's internal target window without changing the system frontmost application; Sky then observed the exact B window and returned its AX value `[MCP4_MULTI_B_BASE]`.

A CUA-bound `computer_type_keyboard` then used `CUABackgroundPaste` on B. B became `[MCP4_HYBRID_B_OK][MCP4_MULTI_B_BASE]`, while `MCP4_CUA_MULTI_A.txt` remained `[MCP4_MULTI_A_BASE]`. The user's frontmost application remained unchanged across the action.

The stale-window fence was also exercised end to end. After taking an A snapshot, the native helper changed TextEdit's internal focus to B. Reusing the A snapshot for a write failed before dispatch with `error=stale_window`, `effect=not_started`, and `backend=cua`; the forbidden test marker appeared in neither A nor B. This confirms that a CUA-bound action is not replayed through the native fallback when its window identity has changed.

The current hybrid implementation was then revalidated locally with A/B TextEdit windows after adding WindowServer identities. A CUA `setValue` changed only A from `[MCP4_MULTI_A_BASE]` to `[MCP4_MULTI_A_ACTION_OK]`; B remained `[MCP4_MULTI_B_BASE]`; A was restored afterward. Google Chrome stayed frontmost for observation, write, verification, and restore. Restarting only the native helper changed the helper PID but preserved all three public CUA window IDs because they were derived from PID + CGWindowNumber rather than the helper UUID.

A controlled CUA-unavailable test also passed after the multi-window changes. With Codex hidden from HOME/PATH, forced `backend=cua` returned `cua_codex_unavailable` with `effect=not_started`; the identical request under `backend=auto` fell back to native and returned native UUID window candidates. No CUA window token was minted before runtime availability was proven.

The CUA adapter returns `backend=cua`, `background_capable=true`, and CUA-bound window/snapshot identifiers on the primary route.

Focused Computer Use tests currently pass 58/58. The complete project test suite passes 207 tests with 4 native acceptance tests skipped by default.

## Design references

The multi-window hardening was cross-checked against local source clones under `../reference/` rather than relying only on documentation summaries:

- `pi-computer-use`: informed the split between WindowServer identity, AX pairing, background delivery, immutable observations, and serialized per-resource effects. Its macOS bridge also demonstrates filtering broad/utility roots and pairing AX windows with numeric CGWindow identities.
- `trycua/cua` `libs/cua-driver`: reinforced the `pid + window_id` targeting contract and the rule that window title is metadata rather than identity.
- `codex-computer-use-cli` and related Sky reverse-engineering work: reinforced the observation that the public macOS Sky wrapper is app-scoped and that AX/PID-directed delivery should be preferred over foreground activation.

These projects are references, not vendored runtime dependencies. MCP4ChatGPT keeps its own small Swift identity/selection layer and continues to use OpenAI CUA/Sky for the primary app-bound observation/action path.

## Known boundaries

CUA is treated as an installed OpenAI runtime dependency rather than vendored project code. The adapter currently discovers the hidden Codex plugin binary when no `codex` executable is on PATH. `MCP_CUA_CODEX_BINARY` can override that discovery path.

Caller-supplied exact PID requests deliberately remain on the native backend. For ordinary CUA requests, the hybrid adapter obtains the exact PID and native window identity from the native helper itself and binds those identities into the CUA snapshot/window fence.

Multi-window targeting depends on the native helper's AX/WindowServer inventory and background window-selection primitive. If the helper cannot verify the requested window, if the process identity changes, or if background selection changes the system frontmost application, the operation fails closed rather than allowing Sky to act on an ambiguous target.

Desktop-wide coordinate actions remain on the native helper for now. This is intentional: the main CUA integration is focused on preserving background, independent-cursor app interaction rather than replacing every low-level pointer primitive immediately.

For rollback, set either:

```text
MCP_COMPUTER_BACKEND=native
```

or disable Computer Use entirely:

```text
MCP_COMPUTER_MODE=off
```

and restart the service.
