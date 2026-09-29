# Roadmap P3a: Managed Git Worktrees MCP Integration (Gap G6)

Date: 2026-09-29. This records the implementation, principal-scoped integration,
and automated verification of managed Git worktrees for isolated concurrent
coding sessions. This completes **Roadmap Phase P3a: Managed Worktree Infrastructure**
(addressing Gap G6 from Document 28). The full end-to-end task validation,
CAS publication (`refs/mcp4chatgpt/tasks/...`), and integration workflow remain
planned under **Phase P3b: Validated Coding Task Pipeline**.

## 1. Motivation and isolation boundary

In multi-agent and concurrent coding scenarios, multiple agents attempting to
mutate the same working directory will collide, dirty untracked files, or overwrite
each other's edits.

Managed Git worktrees provide an isolated filesystem workspace per task/agent:

```text
Source repository (clean commit)
   ├── Agent A -> managed worktree A (wt_...)
   └── Agent B -> managed worktree B (wt_...)
```

Guarantees provided:
- **Clean-source enforcement**: A worktree can only be created when the source repository checkout is clean (`_status_porcelain` is empty).
- **Detached HEAD checkout**: Worktrees are created detached at an exact commit (`git worktree add --detach <dest> <commit>`), preventing branch locking conflicts.
- **Idempotency**: Identical `(principal, workspace, operation_id)` requests replay the existing worktree (`replayed=true, created=false`).
- **Conflict detection**: Same `operation_id` with a different commit or repo raises `idempotency_conflict`.
- **Safe deletion**: `local_worktree_remove` requires the worktree to be clean; uncommitted changes block deletion with `managed_worktree_dirty`.
- **Durable metadata**: All worktree reservations and records persist under `data_dir/managed-worktrees/records/` and `operations/`.

## 2. Tool contracts

The following four tools are registered in `build_tools()` and discoverable under the `workspace` category:

### `local_worktree_create`
- Arguments: `operation_id` (string), `cwd` (string), `base_ref` (string, default `"HEAD"`).
- Creates an isolated managed worktree under `<repo.parent>/.mcp4chatgpt-worktrees/<repo>-<hash>/wt_<uuid>`.
- Scopes the worktree to the canonical Git repository root identity `workspace_id = sha256(canonical_repo_root)`.
- Returns `worktree_id`, `path`, `source_repo`, `base_commit`, `state`, `created`, `replayed`.

### `local_worktree_status`
- Arguments: `worktree_id` (`wt_[a-f0-9]{24}`), optional `cwd` (string).
- Inspects live git status: returns `worktree_id`, `registered`, `exists`, `dirty`, `actual_head`, `base_matches`, `state`.
- If `cwd` is supplied, verifies that the caller has authority over the repository matching the worktree's `workspace_id`.

### `local_worktree_list`
- Arguments: `limit` (integer, default 50), optional `cursor` (string), optional `cwd` (string).
- Cursor pagination: ordered by `(created_at, worktree_id)` descending. Returns `worktrees`, `next_cursor` (opaque token for the next page), and `has_more` (boolean).
- If `cwd` is supplied, filters worktrees belonging to the specific repository/workspace.

### `local_worktree_remove`
- Arguments: `worktree_id` (`wt_[a-f0-9]{24}`), optional `cwd` (string).
- Verifies the worktree has no uncommitted changes, removes the worktree via `git worktree remove`, and marks the record state as `removed`. Replay is a safe no-op.
- If `cwd` is supplied, verifies workspace repository authority before removal.

## 3. Scoping and multi-principal isolation

Worktrees are scoped by:
- Authenticated principal: `principal_hash = sha256(client_id)`
- Workspace: `workspace_id = sha256(canonical_git_repository_root)`
- Scoped reservation ID: `scoped_v1_<sha256(principal_hash + workspace_id + operation_id)>`

Isolation rules:
- **Repo-level workspace identity**: `workspace_id` is rooted at the canonical Git repository root (`git rev-parse --show-toplevel`), preventing scope collision between multiple repositories under the same allowed root.
- **Principal-scoped access**: Principal A cannot view, inspect, or delete scoped worktrees created by Principal B (`worktree_not_found`).
- **Explicit workspace verification**: Passing `cwd` to `status`, `list`, or `remove` strictly validates that the target worktree belongs to the caller's asserted workspace.
- **Zero-trust legacy isolation**: The authenticated MCP API (`worktree_tool_api`) strictly enforces valid scope presence (`strict_scope=True`). Unscoped/legacy records are never leaked or operable over the MCP interface; only low-level internal functions retain access for administrative migration/maintenance.
- **Deterministic cursor pagination**: `local_worktree_list` provides stable pagination via `(created_at, worktree_id)` sort keys and `cursor`/`next_cursor`.

## 4. Test evidence

New focused test suites:
- `tests/test_workspace_worktrees.py`: 6 passed
  - ID validation, create, status, clean/dirty detection, CAS replay, conflict detection, dirty source rejection, clean vs dirty removal, concurrent worktree isolation.
- `tests/test_worktree_tool_api.py`: 7 passed
  - Scoped worktree replay, principal isolation, shared operation_id reuse, ToolRegistry call_tool integration, capability search/get discovery, repo-root workspace scoping across projects, `cwd` workspace authorization & filtering, legacy unscoped record isolation, and deterministic cursor pagination contract.

Complete repository regression:
```text
uv run pytest -q
439 passed, 11 skipped, 5 warnings in 41.28s
```
Zero failures.
