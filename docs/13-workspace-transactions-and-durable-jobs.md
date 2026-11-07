# Workspace Transactions and Durable Jobs

This document records the failure model and hardening architecture introduced
after two classes of incidents:

1. a large untracked Python backend was replaced by an incomplete/mis-spliced
   source file; and
2. a long-running tool call outlived the outer tool gateway timeout, after which
   a replay started a second copy of the same work.

The design assumes two hostile-to-correctness conditions even on a trusted
single-user machine:

- workspace mutations can be delivered concurrently by more than one agent; and
- remote tool delivery is at-least-once unless the tool itself provides
  idempotency.

## 1. Architecture

The public local tools are intentionally thin adapters:

```text
MCP tool
  |
  +-- local_ops.py
        |
        +-- workspace.transactions
        |     +-- workspace.validators
        |     +-- workspace.versioning
        |     +-- workspace.recovery
        |
        +-- jobs.manager
              +-- jobs.store
              +-- jobs.runner
              +-- jobs.process
```

`local_ops.py` should not grow another independent implementation of file
transactions or background-process lifecycle.

## 2. File mutation transaction

### 2.1 Whole-file replacement

Replacing an existing file requires a complete observation of the exact version
being replaced:

```text
local_read_text
  -> raw bytes
  -> sha256
  -> lossless UTF-8 check
  -> redaction check
  -> truncation check
  -> HMAC full_replace_token(path + sha256 + size)

local_write_file(overwrite=true)
  -> target lock
  -> expected_sha256 CAS
  -> verify full_replace_token
  -> candidate validation
  -> persist before-image
  -> PREPARED journal
  -> same-directory temp write
  -> fsync(temp)
  -> os.replace(temp, target)
  -> fsync(parent directory)
  -> finalize Git recovery ref
  -> COMMITTED journal
```

A truncated, lossy-decoded, or redacted read does not receive a
`full_replace_token`. This prevents a client from reconstructing a partial
view and presenting it as the complete replacement file.

The token is HMAC-bound to:

- canonical target path;
- SHA-256 of the observed bytes; and
- exact byte length.

The HMAC key is local state and is never returned to the caller.

### 2.2 Compare-and-swap

An existing-file whole replacement also requires `expected_sha256`.

Therefore this race:

```text
Agent A reads V1
Agent B reads V1
Agent A writes V2
Agent B attempts V3 based on V1
```

ends with B receiving `file_changed_since_read` rather than overwriting V2.

The exact-patch path takes the per-target lock, reads the current file inside
that lock, checks the optional SHA fence, and replaces only the exact anchor.

### 2.3 Candidate validation

Candidate validation occurs before PREPARED/recovery/Git mutation.

For all text candidates:

- UTF-8 must be lossless.

For Python candidates:

- `ast.parse` must succeed.

For whole-file replacement of sufficiently large files:

- an unexpectedly large shrink is rejected by default;
- an intentional rewrite must explicitly set `allow_large_reduction=true`.

For `computer_cua_backend.py`, an additional structural contract currently
requires these top-level functions:

```text
supports
call
stop
_parse_elements
_image_metadata
```

`supports()` is also checked for calls that belong to execution/routing
paths, including `_call_js` and `_resolve_window`. This specifically detects
the historical corruption mode where screenshot/action code was spliced into
the capability predicate.

Exact-patch mutations bypass only the generic shrink heuristic because the
removed text is explicitly identified by the patch anchor. They still undergo
encoding, Python syntax, and CUA structural checks.

### 2.4 Recovery data and Git

Before replacing an existing file, the old bytes are stored in a
content-addressed local recovery blob.

When the target is inside a Git repository, the transaction additionally uses
Git's object database without touching the active index or branch:

```text
git hash-object -w --stdin
git mktree
git commit-tree
git update-ref refs/mcp4chatgpt/file-transactions/<transaction-id>
```

The prepared recovery commit pins the before-image so normal Git object
garbage collection cannot discard it. After filesystem commit, the recovery
ref advances with compare-and-swap semantics to a commit containing both
`before` and `after` blobs.

This works for tracked and untracked source files.

### 2.5 Commit point and journal failure

The mutation commit point is:

1. atomic filesystem replacement and directory fsync; and
2. Git recovery-ref finalization when Git recovery is applicable.

A failure before that point rolls the target back to the before-image.

A failure to append the final COMMITTED journal record happens *after* the
mutation is already durable. It must not be reported as a failed file
mutation, because doing so could induce an unsafe replay.

The result instead contains:

```text
journal_state = commit_record_failed
journal_warning = ...
```

while preserving the successfully committed file.

## 3. Durable jobs

Long-running work must not use synchronous `local_run_command`.

The synchronous command path is limited to 30 seconds. A larger requested
timeout fails before spawning with:

```text
long_command_requires_job
```

Long work uses:

```text
local_start_job
local_job_operation_status
local_job_status
local_job_logs
local_list_jobs
local_cancel_job
```

### 3.1 Idempotent start

A job start carries a caller-supplied `operation_id`. At the MCP boundary,
new reservations are scoped by the authenticated principal and the longest
matching allowed workspace root. The per-install `local-jobs` store is the
installation namespace; the internal reservation key hashes:

```text
principal + workspace + operation_id
```

The user-facing `operation_id` remains unchanged in job metadata. The server
also fingerprints the canonical request:

```text
command + cwd + timeout + shell
```

The operation record is durably reserved before a supervisor can be spawned.

Replay rules inside one principal/workspace scope:

```text
same operation_id + same fingerprint
  -> return the existing job
  -> never spawn a second copy

same operation_id + different fingerprint
  -> idempotency_conflict
```

Different principals or workspaces may reuse the same public `operation_id`.
During upgrades, a matching legacy unscoped reservation is checked and replayed
before a new scoped reservation is created, so an old in-flight job is not
duplicated. `local_job_operation_status(operation_id, cwd)` resolves the
existing job without starting or repairing it.

### 3.2 Uncertain start crash window

If a crash occurs after operation reservation but before complete job metadata
is committed, a later replay returns the reserved job identity with:

```text
state = reserved
recovery = explicit_repair_required
launched = false
```

It deliberately does not guess whether spawning is safe.

No automatic repair or implicit retry should be added to this state without a
separate reconciliation protocol that can prove the prior launch outcome.

### 3.3 Detached supervisor

The durable supervisor is independent from the MCP request lifetime:

```text
MCP request
  -> create durable job
  -> spawn detached supervisor
  -> return job_id

supervisor
  -> spawn command in its own session/process group
  -> retain stdout/stderr
  -> enforce total job timeout
  -> process explicit cancellation
  -> write terminal state
```

The source checkout is explicitly placed on the supervisor `PYTHONPATH` so
source-tree execution and test environments do not depend on pytest's
in-process path injection.

### 3.4 Observation is side-effect free

`local_job_status`, `local_job_logs`, and `local_list_jobs` are
observation-only.

They must never:

- start a job;
- retry an uncertain start;
- repair a job;
- change a saved log cursor implicitly; or
- cancel a job.

Logs use caller-visible byte offsets so replay is deterministic.

### 3.5 Cancellation and macOS process groups

Jobs launch the child with `start_new_session=True`, so the child is the
process-group leader.

The preferred cancellation path is process-group signalling. Some restricted
macOS execution contexts return `EPERM` from `killpg()` even for a child
created by the current job. The process layer therefore falls back to
signalling the group-leader PID. This prevents cancellation/timeout from being
misreported as a generic failed job.

### 3.6 Live scoped MCP acceptance (2026-09-29)

The production MCP instance was restarted with compact exposure after commit
`9b54806`. `server_info` reported **166 capabilities / 6 listed tools**, and
`local_job_operation_status` was discoverable through the compact capability
catalog.

A live scoped job used:

```text
operation_id = live-scope-smoke-20260929-01
cwd          = /Users/vickers/Documents/MCP_Creator/MCP4ChatGPT
command      = printf 'SCOPED_JOB_OK\n'
```

The first `local_start_job` returned
`job_2711524df01d4f5fbdc7d609` with `launched=true`, a persisted principal
hash and workspace ID. `local_job_operation_status` resolved the same job and
reported `state=succeeded`, `exit_code=0`; `local_job_logs` returned
`SCOPED_JOB_OK\n`.

Replaying the identical scoped start returned the same job with
`replayed=true, launched=false`. Reusing the same operation ID with a different
command returned `idempotency_conflict`; no second job was launched. This live
acceptance verifies the MCP-facing scoped identity and recovery lookup on top of
the existing detached supervisor protocol.

## 4. Test evidence

The focused hardening suites currently cover:

- stale CAS rejection;
- complete-read replacement proof;
- truncated reads cannot authorize replacement;
- Git before/after recovery refs;
- Git index remains untouched;
- rollback on Git-finalize failure;
- post-commit journal failure semantics;
- suspicious whole-file shrink rejection;
- Python syntax validation;
- CUA required-function validation;
- CUA `supports()` purity validation;
- exactly-once job replay;
- fail-closed reserved/uncertain start;
- operation-id conflicts;
- explicit log cursors;
- cancellation;
- independent job timeout;
- 30-second synchronous execution ceiling;
- macOS `killpg` permission fallback; and
- MCP job/write tool annotations and schemas.

Latest focused invocation for the scoped Job integration:

```text
uv run pytest -q tests/test_job_tool_api.py tests/test_local_jobs.py tests/test_local_job_tools.py tests/test_capabilities.py
```

Result:

```text
33 passed
```

The complete repository suite was also rerun after the scoped principal/workspace
boundary was added:

```text
uv run pytest -q
420 passed, 11 skipped, 5 warnings
```

The warnings are the existing SWIG deprecation warnings. CodexPro was running
with native Bash in `bashMode=full` for this verification, and the production
compact MCP instance was restarted and exercised separately as described above.

## 5. Recovery and retention

Computer Use also has a manually created checkpoint ref from the recovery
incident:

```text
refs/checkpoints/computer-use-20260923-004720
commit dbcffa8b9d55c1a5bd445f8d56b8bfd889100a96
```

Per-file transaction refs live under:

```text
refs/mcp4chatgpt/file-transactions/
```

A retention/pruning policy is intentionally not implemented yet. Recovery refs
should not be deleted implicitly until retention rules and audit requirements
are defined.

## 6. Next architecture step

The next isolation boundary is managed Git worktrees for concurrent coding
agents:

```text
Agent A -> worktree A
Agent B -> worktree B
runtime -> validated runtime worktree
```

This complements CAS rather than replacing it:

- worktrees reduce the number of collisions;
- CAS detects stale writes inside one workspace;
- transaction recovery handles filesystem/process failures;
- validation rejects structurally unsafe candidates;
- durable jobs prevent replay from duplicating long-running effects.

A separate runtime worktree should ultimately run only a validated commit,
while development agents operate in disposable or managed development
worktrees.
