# MCP4ChatGPT release acceptance — 2026-09-29

## Scope

This note closes the 2026-09-28/29 reliability and Personal Skill acceptance
work without adding another feature surface. It records the verified runtime
state after the compact capability rollout, strict file/tool recovery work,
durable Job scoping, Chrome bridge capability negotiation, and same-tab live
element-state readback.

## Accepted runtime baseline

The production/self-use MCP instance is running with:

- `personal_full_access=true`;
- `tool_catalog.exposure=compact`;
- **166 capabilities** behind **6 listed top-level tools**;
- Chrome Computer Use in `interact / auto` mode;
- local health at `127.0.0.1:8766`;
- public health at `https://mcp.runzhe.uk/health`.

Both local and public health endpoints returned `ok=true` from the same current
runtime. The Chrome bridge in that health response reported:

- `connected=true`;
- Extension **1.0.5**;
- protocol revision **2**;
- `capabilities_advertised=true`;
- `get_element_state` present in `supported_commands`.

The launcher's full-access profile already defaults to compact exposure:

```sh
MCP_TOOL_EXPOSURE="${MCP_FULL_TOOL_EXPOSURE:-compact}"
```

Therefore no repository default change is required. `MCP_FULL_TOOL_EXPOSURE=full`
remains the explicit override for full-schema diagnostics or controlled
comparison work.

### Status-command caveat

A plain `./MCP4ChatGPT.command status` evaluates the launcher process's current
environment defaults. It does not reconstruct the profile environment of an
already-running service. Consequently it can print localhost/default profile
labels even when the running instance was started by `restart-full`. Release
acceptance therefore uses the real health endpoints and `server_info` as the
authoritative runtime evidence.

## P4A / Chrome Extension live acceptance

The stale Extension runtime was first detected correctly: an older background
worker did not know `get_element_state`. After the unpacked Extension reload,
the new connection advertised all required commands and
`extension_reload_required=false`.

Final same-handle acceptance:

```text
fresh tab_id = 59535031
URL          = http://127.0.0.1:8767/form
selector     = #note
value        = P4A_LIVE_PASS_20260929_FINAL
```

`ext_fill_input(..., submit=false)` returned `filled=true`. A subsequent
`ext_get_element_state` on the **same tab_id** returned:

```text
found=true
element_tag=INPUT
element_type=text
value=P4A_LIVE_PASS_20260929_FINAL
value_suppressed=false
value_truncated=false
disabled=false
read_only=false
```

Result: **implemented / test-passed / live-pass**.

## Durable Jobs live acceptance

New MCP Job reservations are scoped by authenticated principal, workspace, and
public `operation_id`; legacy unscoped reservations are checked first during
upgrade replay so old in-flight work is not duplicated.

Final release smoke used:

```text
operation_id = release-smoke-20260929-final
command      = printf 'RELEASE_JOB_OK\n'
cwd          = /Users/vickers/Documents/MCP_Creator/MCP4ChatGPT
```

The first start returned `launched=true` and a persisted scoped Job. Read-only
`local_job_operation_status` recovered the same job and reported
`state=succeeded`, `exit_code=0`. `local_job_logs` returned exactly
`RELEASE_JOB_OK\n`.

Earlier live acceptance also verified:

- identical scoped replay returns the same job with
  `replayed=true, launched=false`;
- same scoped `operation_id` plus a different request returns
  `idempotency_conflict`;
- unit tests cover principal isolation for status, logs, list, cancel and
  operation lookup;
- different principals/workspaces may reuse the same public `operation_id`.

## Automated regression baseline

Latest complete repository run:

```text
uv run pytest -q
426 passed, 11 skipped, 5 warnings
```

The five warnings are the existing SWIG deprecation warnings. `git diff --check`
also passed before the P4A live-acceptance commit.

## Manual ChatGPT full vs compact benchmark

The manual benchmark remains intentionally **incomplete rather than fabricated**:

```text
planned runs   18
recorded runs  15
remaining      compact trial 3: A, B, C
```

Observed correctness so far:

- full exposure: **7/9** correct;
- compact exposure: **6/6** correct.

The two full failures were task C response-shape failures: the model returned
filename arrays for `requested` / `completed` where the benchmark expected
counts. Compact has not shown a correctness failure in the six recorded samples,
but there are still three planned compact samples outstanding, so this note does
**not** claim a statistically or experimentally complete superiority result.

An attempt to automate the remaining fresh-chat samples through the logged-in
Chrome session was rejected as benchmark evidence: the newly opened ChatGPT tab
was subsequently taken over by the current conversation state, so it no longer
satisfied the independent fresh-conversation requirement. No result from that
attempt was added to the benchmark JSON.

The remaining A/B/C runs must therefore be collected as three genuinely
independent fresh ChatGPT conversations. Until then,
`benchmarks/runs/2026-09-29-chatgpt-manual.json` correctly remains at 15/18.

## Exposure decision

Keep **compact** as the normal full-access runtime/default exposure. This is
already the repository default and has passed the current capability, P4A, Job,
strict-file and service smoke tests. Full exposure remains available explicitly
for diagnosis and benchmark comparison.

This is an operational/default decision, not a claim that the unfinished manual
benchmark proves compact is universally better.

## Relevant commits

Recent acceptance chain:

```text
9404d36 Record live element-state acceptance
598ca36 Record stale extension runtime detection
4518acd Advertise Chrome bridge runtime capabilities
f1ddaee Record live scoped job acceptance
9b54806 Scope durable jobs by principal and workspace
5877d90 Record partial ChatGPT full vs compact benchmark
8038729 Add same-tab live element state readback
4bd9f24 Verify live strict files and HTTP downstream reuse
a0e19c0 Integrate recoverable file tools and HTTP downstream bridge
```

## Final state

Accepted:

- compact runtime and capability discovery;
- public/local health;
- Chrome Extension protocol/capability advertisement;
- same-tab live element-state readback;
- scoped durable Job identity and recovery;
- idempotent replay/conflict behavior;
- complete repository regression suite.

Outstanding by design:

- compact trial 3 A/B/C manual fresh-chat samples only.

No additional functional change is required for the accepted runtime baseline.
