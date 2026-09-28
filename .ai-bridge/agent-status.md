# Agent Status

Updated: 2026-09-27

## Scope completed

Reviewed and continued the uncommitted capability-discovery and mcpc reliability work without committing or deploying.

### Files touched in this pass

- `tests/test_mcpc_acceptance.py`
  - generates a per-test random auth secret instead of relying on the shared fixed test secret;
  - creates credential-bearing mcpc config files with mode 0600 before writing content;
  - tracks the normal, invalid-auth, and restart sessions for unconditional cleanup;
  - cleans both isolated mcpc homes and removes temporary config files in `finally`;
  - adds a POSIX process-table check keyed by unique session/home markers so socket/session cleanup is not the only orphan evidence.
- `src/mcp4chatgpt/downstream/client.py`
  - protects `catalog_error` with the same lock as the tool snapshot;
  - cancels and awaits the catalog refresh task during async shutdown;
  - also cancels an outstanding refresh task from synchronous failed-start cleanup.
- `src/mcp4chatgpt/downstream/manager.py`
  - protects `has_tool()` and `is_downstream_tool()` reads with `_catalog_lock`.
- `src/mcp4chatgpt/tools.py`
  - snapshots downstream audit-channel lookup under `_catalog_lock`;
  - snapshots `server_info.tool_catalog` metadata under the catalog lock.
- `tests/test_discovery_regressions.py`
  - retains the dynamic downstream client and verifies its refresh task is terminal after manager shutdown.

The pre-existing uncommitted R1-R7 fixes remain in `downstream/client.py`, `downstream/manager.py`, `server.py`, `tools.py`, `tests/test_capabilities.py`, `tests/fake_dynamic_mcp_server.py`, and `tests/test_discovery_regressions.py`.

## Checks run

1. `git diff --check`
   - PASS, exit 0.

2. `pytest -q tests/test_downstream.py`
   - 39 passed, 2 failed.
   - Both failures are environment-only: the CodexPro safe-bash system pytest runs under Python 3.14 and cannot import `referencing` when the two ToolRegistry integration tests import `mcp4chatgpt.tools`.
   - No low-level downstream client/manager test failed.

3. `pytest -q tests/test_downstream.py -k "not ToolRegistryDownstreamIntegration"`
   - PASS: 39 passed, 2 deselected.

## Verification blockers

CodexPro is configured with `bashMode=safe`. It rejects direct execution of `.venv/bin/python -m pytest` and `.venv/bin/pytest`.

The repository virtualenv is CPython 3.13.13, while the allowed system pytest is running on CPython 3.14. Mixing the 3.13 virtualenv site-packages into the 3.14 interpreter would be unsafe because the project includes binary dependencies, so that workaround was intentionally not used.

A Codex handoff plan was written, but no local handoff executor is running (`.ai-bridge/handoff-run-state.json` is absent), so no external agent test result exists.

## Still required before commit/deploy

Run using the repository virtualenv:

- `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_capabilities.py tests/test_discovery_regressions.py tests/test_server.py tests/test_downstream.py`
- `MCP_MCPC_TESTS=1 PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_mcpc_acceptance.py`
- `PYTHONPATH=src .venv/bin/python -m pytest -q`
- `git diff --check`

Do not claim R1-R8 fully accepted until those commands pass. Real desktop GUI tests, production service restart, and ChatGPT connector acceptance were intentionally not run.

## Review notes / remaining risks

- The R8 mcpc failure-path implementation is now materially stronger, but its external mcpc acceptance test has not yet been executed after these edits.
- Runtime catalog refresh currently performs a manager snapshot/deep-copy and registry comparison on request paths. This is correct for the current scale but may become an O(N) performance concern with very large downstream catalogs; generation-based fast-pathing can be considered separately after correctness acceptance.
- No commit was created and the production MCP service was not restarted.
