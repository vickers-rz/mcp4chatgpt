# CUA final acceptance — 2026-09-23

Verified against the current working tree, using owned temporary TextEdit documents.

## Passed

- Two documents with the same title received distinct CUA window tokens.
- CUA setValue independently updated both documents; readback confirmed no cross-window writes.
- Closing and reopening one document invalidated its old CUA window token (`stale_window`).
- Reproduction: `PYTHONPATH=src .venv/bin/python scripts/acceptance_cua_windows.py`. This opens temporary TextEdit documents and closes them without saving afterward.
- Started the service using `./MCP4ChatGPT.command start-full`; local and public health checks passed.
- Actual 4GPT connector `server_info` reported `interact` / `auto`; `computer_list_apps` reported `backend=cua`.
- Actual connector observation of the owned temporary document succeeded, followed by an 859 × 768 JPEG screenshot.

## Unresolved

Follow-up: two fresh local backend paste probes and one actual 4GPT connector paste probe all returned `success=true`, `effect=completed`, `strategy=CUABackgroundPaste`; fresh readback confirmed exactly one inserted marker. No original upstream error body exists in the service log, and the original failure was not reproduced. This is evidence of successful subsequent operation, not proof of a root-cause fix.

The adapter was hardened so malformed replies, missing completion markers, and upstream errors mentioning permissions or application exit after a dispatched write remain `outcome_unknown` with fallback disabled. Four regression cases verify this behavior. Error text alone must not be treated as proof that a write never happened.

The connector `computer_type_keyboard` returned `cua_operation_failed`, `effect=outcome_unknown`. A subsequent fresh observation confirmed that `MCP_CONNECTOR_VERIFIED_` was inserted exactly once before `MCP_CONNECTOR_BASE`. The request was not replayed. The upstream error detail was not retained, so its cause is not yet established. Do not report this write-response contract as passed or convert unknown outcomes to success without evidence.

The session's cached connector inventory still contains the earlier get_state description, rather than the expanded multi-window description in source. Listing/calling tools works, but description refresh remains unverified.

Temporary acceptance documents were closed and removed. The requested full-access service remains running. No Git baseline commit was created during this acceptance run: the unresolved write-response behavior must remain explicit before declaring release readiness.
