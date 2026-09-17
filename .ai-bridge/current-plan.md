# Mainland Retrieval Gateway — acceptance and ChatGPT tool snapshot refresh

Updated: 2026-09-17T15:06:30.411Z
Workspace: /Users/vickers/Documents/MCP_Creator/MCP4ChatGPT
Target agent: Codex (codex)

## Plan

Continue implementation from the current working tree; do not redo the 2026-09-11 downstream/async-job work. Treat current source/tests/live runtime as authoritative over older handoff notes.

Current verified state (2026-09-17):
1. Runtime service is healthy at http://127.0.0.1:8766 and public https://mcp.runzhe.uk/mcp. Chrome Extension 1.0.4 is connected. chrome_devtools downstream is running and reports 29 tools.
2. The server's live tools/list returns 97 tools total: 64 build_tools + 4 ext async-job tools (enabled by env) + 29 chrome_devtools downstream tools.
3. ChatGPT currently has an older frozen app snapshot with only 54 tools. It therefore cannot yet see ext_search_web, ext_read_webpage, ext_web_rag, ext_archive_webpage, the five web_archive_* tools, local_expose_file, the four ext_*_job tools, or chrome_devtools__* tools. This is a ChatGPT custom-app action snapshot issue, not a backend registry failure. OpenAI docs state MCP app tool/action changes are not auto-enabled and require Refresh/Scan Tools or app recreation depending on plan/UI. Do not try to 'fix' this only by setting tools.listChanged=true; ChatGPT approval snapshots are a separate layer.
4. Local OpenWebUI has already re-listed the updated server and successfully called local_expose_file followed by resources/read. Runtime audit confirms this.
5. Live verification performed through MCP4ChatGPT itself: ext_navigate opened http://127.0.0.1:8766/health and ext_get_dom read it successfully, proving localhost/private-network access is allowed by default. ext_navigate/ext_get_dom also opened https://www.shaanxi.gov.cn/ successfully. Direct local MCP tools/call to ext_search_web returned China-local Chrome/Bing results for '陕西省人民政府 政策'; direct tools/call to ext_read_webpage extracted the rendered Shaanxi policy-library body.
6. Current full Python test suite had previously passed 147 tests; re-run full suite after any code change.

Architecture direction:
- MCP4ChatGPT is a Mainland Retrieval Gateway, not a mandatory local-RAG system.
- Preferred transient web path: ChatGPT -> ext_search_web/ext_read_webpage (or explicit Chrome tab tooling) -> page text directly to ChatGPT.
- Preferred binary-file path: download/discover file on Mac -> local_expose_file -> resource_link -> resources/read -> original bytes to ChatGPT/model.
- web_archive/knowledge are optional persistence/archive layers, not required transit hops.
- Keep RFC1918/localhost/CGNAT/Tailscale/.local/intranet hostnames allowed by default for this self-hosted on-demand gateway; strict network mode remains opt-in.

Next acceptance sequence:
A. User must Refresh/Scan Tools for the MCP4ChatGPT ChatGPT app (or recreate the app if the UI has no refresh action). Chrome extension reload is not sufficient. After refresh, verify ChatGPT can discover all expected current tools, especially local_expose_file, ext_search_web, ext_read_webpage, ext_start_js_job and chrome_devtools__list_pages.
B. From ChatGPT itself, run true end-to-end calls to ext_search_web and ext_read_webpage without the local_run_command workaround.
C. Run true ChatGPT binary-resource acceptance: choose a harmless local PDF/DOCX/image under allowed roots, call local_expose_file, ensure the returned resource_link is consumable by ChatGPT via resources/read and that the model can inspect the original file. Do not substitute local text extraction for this acceptance.
D. Verify one chrome_devtools__list_pages/take_snapshot call through the refreshed ChatGPT action snapshot and ensure ext_* remains independent.
E. Re-run focused tests and full pytest; inspect git diff; update README with a short operational note that ChatGPT custom-app tool definitions are snapshotted and must be refreshed after adding/renaming tool actions.
F. Only after E2E acceptance, create a clean checkpoint/commit strategy; current working tree contains many intended modified/untracked files from the broader feature set, so do not discard them.

Known observation to preserve: server.py currently advertises tools.listChanged=false and modern tools/list replies use ttlMs=0/cacheScope=private. That is not the root cause of ChatGPT's 54-tool view: audit shows the bearer ChatGPT client reauthorized but did not issue a fresh tools/list, while local clients did and saw the expanded registry.

Do not commit credentials, OAuth material, browser session data, or sensitive page contents. Keep tool schemas backward-compatible where possible.

## Implementation contract

- Work from this plan in small, reviewable steps.
- Keep edits scoped to the requested task and existing project conventions.
- Run focused verification before handing work back.
- Update .ai-bridge/agent-status.md with files touched, checks run, results, blockers, and review notes.
- Save the final review diff to .ai-bridge/implementation-diff.patch when practical.
- Append notable execution events to .ai-bridge/execution-log.jsonl when the implementation agent supports logging.
