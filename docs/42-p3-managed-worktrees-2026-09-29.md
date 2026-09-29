# P3 Managed Git Worktrees MCP Integration

Date: 2026-09-29. This records the implementation, principal-scoped integration,
and automated verification of managed Git worktrees for isolated concurrent
coding sessions, fulfilling Transaction Roadmap P3 (Gap G6 in document 28).

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
- Returns `worktree_id`, `path`, `source_repo`, `base_commit`, `state`, `created`, `replayed`.

### `local_worktree_status`
- Arguments: `worktree_id` (`wt_[a-f0-9]{24}`).
- Inspects live git status: returns `worktree_id`, `registered`, `exists`, `dirty`, `actual_head`, `base_matches`, `state`.

### `local_worktree_list`
- Arguments: `limit` (integer, default 50).
- Lists all active and historical worktrees scoped to the authenticated principal.

### `local_worktree_remove`
- Arguments: `worktree_id` (`wt_[a-f0-9]{24}`).
- Verifies the worktree has no uncommitted changes, removes the worktree via `git worktree remove`, and marks the record state as `removed`. Replay is a safe no-op.

## 3. Scoping and multi-principal isolation

Worktrees are scoped by:
- Authenticated principal: `principal_hash = sha256(client_id)`
- Workspace: `workspace_id = sha256(canonical_workspace_path)`
- Scoped reservation ID: `scoped_v1_<sha256(principal_hash + workspace_id + operation_id)>`

Isolation rules:
- Principal A cannot view, inspect, or delete worktrees created by Principal B (`worktree_not_found`).
- Different principals can safely reuse the same public `operation_id` without collision.
- `local_worktree_list` only returns worktrees belonging to the calling principal.

## 4. Test evidence

New focused test suites:
- `tests/test_workspace_worktrees.py`: 6 passed
  - ID validation, create, status, clean/dirty detection, CAS replay, conflict detection, dirty source rejection, clean vs dirty removal, concurrent worktree isolation.
- `tests/test_worktree_tool_api.py`: 3 passed
  - Scoped worktree replay, principal isolation, shared operation_id reuse, ToolRegistry call_tool integration, and capability search/get discovery.

Complete repository regression:
```text
uv run pytest -q
435 passed, 11 skipped, 5 warnings in 40.59s
```
Zero failures.
