# Compact exposure deployment acceptance

Date: 2026-09-28 (Asia/Shanghai).

The user approved enabling compact exposure before deciding whether to build a
general Code Mode executor. The public profile now defaults to compact; the
local/default profile remains unchanged. `MCP_PUBLIC_TOOL_EXPOSURE=full` provides
an explicit full-list comparison or rollback. The tmux launcher passes the
selected exposure into the new service pane, including when tmux already runs.

## Evidence

- Before deployment, live local `tools/list`: 145 tools, 131,696 bytes.
- After restart, live local `tools/list`: 4 tools, 2,563 bytes.
- Both byte measurements serialize the tools array with Python
  `json.dumps(tools, ensure_ascii=False).encode()`. They are definition bytes,
  not model tokens, total task cost, or OAuth-authenticated schema sizes.
- Visible tools: `server_info`, `capability_search`, `capability_get`,
  `capability_call`.
- Live searches for PDF, WPS, 浏览器 and local_read_text succeeded. Definitions
  for extension status, WPS status and local_read_text were retrievable.
- Hidden ext_connection_status returned connected=true. Revision-bound
  capability_call successfully read a bounded excerpt of this repository's
  README and dispatched wps__wps_status. WPS dispatch success does not establish
  that a WPS document is open or its add-in is connected.
- 29 targeted capability, discovery regression and controller tests passed.
- Shell syntax validation and git diff --check passed.
- ChatGPT's existing 4GPT plugin initially displayed Write73 + Read72. Its
  settings page's Refresh tools action fetched a four-tool list (server audit).
  After reloading the plugin page, the UI displayed Write1 + Read3 and the exact
  four names above. Screenshot: ../logs/compact-chatgpt-20260928.png.
- After enabling the explicit personal full-access / Computer Use profile on
  2026-09-28, the live catalog increased to 159 capabilities while `tools/list`
  remained at exactly 4 bootstrap tools. All 17 `computer_*` capabilities are
  discoverable through `capability_search` and callable through
  `capability_call`; they are no longer a compact-exposure exception.
- The same live instance reports `personal_full_access=true`, Computer Use
  `interact`, backend `auto`, allowlist `*`; Accessibility, Screen Recording and
  event-posting permission probes all returned true.

## Remaining evaluation

This verifies deployment, protocol calls and ChatGPT's refreshed tool inventory.
It is not a real-model full-versus-compact task comparison. No model token counts,
repair rounds or task latency improvements are claimed. The existing
benchmarks/model_e2e_tasks.json remains the task comparison starting point;
general Code Mode remains deferred.

Restart: `./MCP4ChatGPT.command restart-public`.

Rollback/comparison:
`MCP_PUBLIC_TOOL_EXPOSURE=full ./MCP4ChatGPT.command restart-public`.

Cloudflare Tunnel remains managed by Lucky on N100.
