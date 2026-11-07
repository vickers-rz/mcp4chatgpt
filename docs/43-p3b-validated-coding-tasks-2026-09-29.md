# Roadmap P3b Core: Validated Coding Task Pipeline

Date: 2026-09-29.

This document records the first complete artifact-oriented slice of Roadmap P3b built on top of the P3a managed-worktree infrastructure. It implements a reproducible path from a frozen baseline to an isolated candidate commit, durable validation evidence, and compare-and-swap publication under `refs/mcp4chatgpt/scoped-tasks/<task-key>`.

The implementation also includes explicit source-checkout integration and a durable validation-attempt state. Fault-injection and multi-process crash acceptance remain follow-up work, so this document does **not** declare the entire Roadmap P3 closed.

## 1. Lifecycle

The implemented lifecycle is:

```text
coding_task_create
    -> frozen base commit
    -> scoped isolated managed worktree
    -> edit only inside task worktree
coding_task_validate
    -> declared-path enforcement
    -> candidate commit
    -> validation command
    -> evidence bound to candidate commit/tree
coding_task_publish
    -> re-check validation freshness
    -> compare-and-swap task ref
coding_task_integrate
    -> verify clean source checkout and expected source commit
    -> fast-forward source checkout to published candidate
coding_task_reconcile_validation
    -> inspect an interrupted validation attempt
    -> explicitly abandon after reviewing possible command effects
coding_task_inspect
    -> live worktree / validation / publication / integration state
```

Publication does not merge, reset, checkout, cherry-pick, or otherwise mutate the user's source checkout.

## 2. MCP contracts

### `coding_task_create`

Required:
- `task_id`: 1-64 ASCII letters, digits, `_`, or `-`.
- `cwd`: path inside the source Git repository.
- `paths`: explicit repository-relative paths owned by the task.

Optional:
- `base_ref`: default `HEAD`.

The call resolves `base_ref` to an exact commit, derives the workspace identity from the canonical Git repository root, and creates/replays a principal+workspace-scoped detached managed worktree.

The first request persists the normalized base selector, declared paths and resolved commit before creating the worktree. Repeating the same scoped request reuses that commit even if HEAD moves. Changing the selector or paths is an idempotency conflict. Existing task records retain their recorded baseline.

A legacy worktree reservation without a creation intent still uses the recorded worktree fingerprint. If its original selector has moved, retry with the original commit SHA. A conflicting retry leaves the reservation available for that recovery.

### `coding_task_inspect`

Required:
- `task_id`
- `cwd`

Returns the durable task record plus live worktree state, whether the current validation still matches the worktree HEAD/tree, the task ref target, and whether recorded publication still matches Git.

Task lookup is scoped by authenticated principal and canonical repository workspace.

### `coding_task_validate`

Required:
- `task_id`
- `cwd`
- `operation_id`
- `command`

Optional:
- `timeout_sec`: 1-1800 seconds, default 300.

Before validation:
1. The task worktree must still be active and registered.
2. The original base commit must remain an ancestor of HEAD.
3. The cumulative changed path set from the frozen baseline must be a subset of the task's declared paths.
4. Only actually changed declared paths are staged.
5. If needed, the isolated worktree creates a candidate commit using the MCP4ChatGPT coding-task identity.
6. The resulting candidate tree must differ from the baseline and the worktree must be clean.

Validation then runs the supplied command with the task worktree as cwd. This provides Git/worktree filesystem isolation only; it is **not an OS-level sandbox**.

Evidence is stored under:

```text
<data_dir>/managed-worktrees/evidence/<task-key>/<validation-id>/
    evidence.json
    stdout.log
    stderr.log
```

Evidence binds:
- task and worktree identity;
- base commit;
- candidate commit and candidate tree;
- declared and actually committed paths;
- validation command and timeout;
- exit code / timeout state;
- whether the worktree remained unchanged during validation;
- timestamps;
- platform, machine, Python, shell, and Git summaries;
- log file paths.

Validation is accepted only when the command exits zero, does not time out, and the candidate HEAD/tree remains unchanged and clean after the command.

A validation command that modifies source files invalidates its own evidence even if it exits zero.

#### Validation operation replay

`operation_id` prevents response-loss retries from executing the validation command twice.

For a completed validation operation:
- the same `operation_id` with the same command/timeout returns the original evidence without rerunning the command;
- reusing that operation ID with different command/timeout parameters raises `idempotency_conflict`;
- replay still reports whether the original evidence is current relative to the worktree now.

Before command launch, the task stores a `prepared` attempt. A gated launcher starts without executing the command. The server persists its process identity and the `running` marker before releasing the gate. An interrupted `running` or `uncertain` attempt is never automatically rerun. `coding_task_reconcile_validation` requires the same scoped task and operation ID, `resolution: "abandon"`, and `confirm_effects_reviewed: true`. It refuses to abandon an attempt while its recorded process group is still running. For a legacy running or uncertain attempt with no recorded PID, operators can select `abandon_unknown_process` with both `confirm_effects_reviewed` and `confirm_no_process_running` set to true. The abandoned operation ID cannot be reused. The operator must inspect any possible effects before confirming; this is not automatic proof that an interrupted command had no effects.

## 3. Publication semantics

`coding_task_publish` publishes only a validation that is still current.

Immediately before publication it verifies:
- the persisted validation succeeded;
- worktree HEAD still equals the validated candidate commit;
- worktree tree still equals the validated candidate tree;
- the worktree is still clean.

The publication target is:

```text
refs/mcp4chatgpt/scoped-tasks/<task-key>
```

The ref update uses Git compare-and-swap:

```text
git update-ref <task-ref> <candidate> <expected-old>
```

Unexpected ref movement is reported as `coding_task_publish_conflict`.

If the ref already points at the exact candidate but the durable task record was not updated—for example, after a response loss at the ref-update commit point—the next publish call reconciles the durable record instead of publishing a second result.

### `coding_task_integrate`

Required: `task_id`, `cwd`, and the exact `expected_source_commit` (40-hex Git commit). The operation requires a clean source checkout, a still-current published task ref, and an expected source HEAD that is an ancestor of the candidate. It uses a fast-forward Git merge; dirty or diverged source state returns a conflict. If the source already points at the published candidate after a lost response, the task record is reconciled. It never uses `reset --hard`.

## 4. Isolation and conflict rules

- Task records are keyed by authenticated principal, canonical Git-repository workspace, and task ID.
- Different principals may create isolated tasks with the same public task ID.
- Their task metadata and worktrees are not mutually visible.
- The publication ref is intentionally repository-global. Independent principals or agents targeting the same public task ref therefore converge through Git CAS and receive an explicit publication conflict instead of silent overwrite.
- Changes outside the declared path set are rejected before candidate publication.
- Validation after code drift requires a new validation operation; stale evidence cannot be published.
- Two tasks from the same baseline can publish separate artifact refs. Integrating the first advances source HEAD; integrating the second with the old expected source commit reports a conflict.

## 5. Automated acceptance

Focused P3b tests cover:
- create -> isolated edit -> candidate commit -> validate -> publish;
- source checkout remains unchanged by artifact publication;
- post-validation code change invalidates evidence;
- undeclared path changes are rejected;
- external task-ref drift is detected;
- create replay and request conflict;
- validation operation replay does not execute the command twice;
- validation operation parameter conflicts;
- a validation command that mutates the worktree invalidates itself;
- response-loss reconciliation when the task ref already reached the candidate;
- principal-scoped task isolation;
- interrupted validation reconciliation refuses a live process group and permanently abandons the reviewed operation ID;
- two published tasks from the same baseline detect a source integration conflict;
- capability discovery, schema retrieval, and registry invocation.

Current focused result:

```text
uv run pytest -q tests/test_coding_tasks.py
14 passed
```

Combined workspace/capability/server regression:

```text
uv run pytest -q \
  tests/test_workspace_worktrees.py \
  tests/test_worktree_tool_api.py \
  tests/test_coding_tasks.py \
  tests/test_capabilities.py \
  tests/test_server.py

64 passed
```

## 6. P3 acceptance coverage and remaining work

The following distinguishes tested boundaries from remaining hardening:

1. **Fault-injection and multi-process acceptance**
   - Independent service-worker SIGKILL tests cover launch before the running record, before gate release, after command start, before/after evidence persistence, and before/after completed validation and publication records.
   - Three simultaneous service processes retry the same validation or publication. Tests require exactly one original response, replayed responses for the others, and exactly one command side effect.
   - These cover the named completion boundaries and same-operation races; they do not establish exhaustive crash safety for all filesystem failures or competing operations.

2. **Interrupted-command reconciliation acceptance**
   - The durable running/uncertain marker prevents blind command replay, and explicit abandonment is available.
   - Worker-crash recovery is exercised at the boundaries above. Broader orphaned-child and competing-operation scenarios remain additional hardening work.

Accordingly, the current status is:

- **P3a Managed Worktree Infrastructure: implemented and regression-tested.**
- **P3b Validated Artifact Pipeline and source integration: implemented and regression-tested.**
- **P3 crash/fault-injection acceptance: still in progress.**

### Editing and checkout ownership

Use `coding_task_read_file`, `coding_task_write_file`, or
`coding_task_apply_patch` with the source `cwd`, `task_id`, and a path relative
to the task checkout. Reads return the file SHA-256 and a replacement token
when eligible. Overwriting uses both values. Exact patches require the SHA-256.
Writes are limited to declared paths. Paths through symlinks or Git metadata
are rejected. These tools work when the checkout is outside the server's
general file roots because authorization is scoped to the task and principal.

`local_worktree_remove` refuses checkouts owned by coding tasks, including
during validation and when recovering a task creation whose checkout was
already recorded. Task checkout removal requires a separate lifecycle operation.

### Validation completion and recovery

The validation timeout covers the shell and remaining runnable members of its
process group. The service checks the candidate and writes validation evidence
after that group exits. On timeout it attempts TERM and KILL, then confirms
exit. If exit cannot be confirmed, the attempt remains unresolved and the
operation ID cannot rerun the command. Processes that deliberately leave the
group are outside this guarantee.

An unresolved attempt makes earlier evidence non-current and blocks editing,
publishing, and integration until reconciliation. If the service crashed before
it recorded a process group, the reconciliation endpoint cannot establish exit
and leaves the attempt unresolved unless an operator uses the legacy-only
`abandon_unknown_process` resolution with both `confirm_effects_reviewed` and
`confirm_no_process_running` set to true. This is an explicit manual assertion,
not automatic process-exit detection.

On publish or integrate, a single Git reference transaction verifies the legacy ref against the recorded published commit and creates or verifies the scoped ref. Drift causes a conflict and the legacy ref is retained.

### Completion-boundary acceptance (2026-11-07)

`tests/test_review_fixes.py` now kills an independent service process at six
completion boundaries. Before the completed validation record is durable,
retries refuse to execute the command and publication is blocked; explicit
abandonment permanently retires the operation ID. A separately written
evidence file alone is not treated as committed validation. After the record
is durable, the original result is replayed. If publication updates the Git
reference before its task record, retry reconciles the record against that
reference; after the record is durable, retry replays publication. Every case
checks that the command's external append happened exactly once.

Two additional tests synchronize three independent service processes before
concurrent validation/publication retries. Task locking serializes each race
and returns one original result plus two replays. Targeted regression:
`sh scripts/test.sh -q tests/test_review_fixes.py --tb=short` — **27 passed**.
No production service or real ChatGPT session was changed by these tests.

Full regression on 2026-11-07: `sh scripts/test.sh -q --tb=short` —
**492 passed, 11 skipped** (five SWIG deprecation warnings);
`node --test tests/browser_search.test.mjs` — **6 passed**.
The opt-in isolated MCP/OAuth acceptance was not rerun in this test-only change;
its previous result remains 10 passed. Production deployment and real-client
acceptance remain outstanding.
