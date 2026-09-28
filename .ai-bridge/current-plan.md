# Finish discovery and mcpc cleanup acceptance

Updated: 2026-09-26T17:13:54.125Z
Workspace: /Users/vickers/Documents/MCP_Creator/MCP4ChatGPT
Target agent: Codex (codex)

## Plan

Review and complete the current uncommitted MCP4ChatGPT discovery/mcpc reliability work. Do not commit.

Current intended changes already present:
1. R1-R7 capability/discovery fixes in downstream/client.py, downstream/manager.py, tools.py, server.py and discovery regression tests.
2. R8 hardening in tests/test_mcpc_acceptance.py: random per-test auth secret, credential config files created at 0600 before content, tracking and cleanup of normal/restart/invalid sessions, deletion of temporary config files, isolated-home cleanup checks, and best-effort process checks.
3. Concurrency hygiene: protect manager has_tool/is_downstream_tool reads with _catalog_lock and client catalog_error reads/writes with _tools_lock.

Tasks:
- Review the current diff for correctness, especially tests/test_mcpc_acceptance.py failure paths and whether cleanup can hang or produce false positives.
- Fix any concrete bugs found, preserving scope and production defaults.
- Add/adjust focused tests if needed. Prefer deterministic evidence over timing-only assertions.
- Run with the repository virtualenv:
  a) PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_capabilities.py tests/test_discovery_regressions.py tests/test_server.py tests/test_downstream.py
  b) MCP_MCPC_TESTS=1 PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_mcpc_acceptance.py
  c) PYTHONPATH=src .venv/bin/python -m pytest -q
  d) git diff --check
- Do not run real desktop GUI tests and do not restart/deploy the production MCP service.
- Update .ai-bridge/agent-status.md with exact files changed, commands/results, remaining risks. Save implementation diff if supported.

## Implementation contract

- Work from this plan in small, reviewable steps.
- Keep edits scoped to the requested task and existing project conventions.
- Run focused verification before handing work back.
- Update .ai-bridge/agent-status.md with files touched, checks run, results, blockers, and review notes.
- Save the final review diff to .ai-bridge/implementation-diff.patch when practical.
- Append notable execution events to .ai-bridge/execution-log.jsonl when the implementation agent supports logging.
