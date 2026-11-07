# Roadmap P3a: Managed Git Worktrees MCP Integration (Gap G6)

Date: 2026-09-29. This records the implementation, principal-scoped integration,
and automated verification of managed Git worktrees for isolated concurrent
coding sessions. This completes **Roadmap Phase P3a: Managed Worktree Infrastructure**
(addressing Gap G6 from Document 28). The validated candidate/evidence/CAS
publication core, explicit source-checkout integration, and validation-attempt
reconciliation are now implemented in
[Document 43](43-p3b-validated-coding-tasks-2026-09-29.md). Crash fault-injection
acceptance remains P3 follow-up work.

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
- **Conflict detection**: Within the same `(principal, workspace, operation_id)` scope, changing the resolved base commit raises `idempotency_conflict`; the same public operation id may be reused independently in another repository workspace.
- **Safe deletion**: `local_worktree_remove` requires the worktree to be clean; uncommitted changes block deletion with `managed_worktree_dirty`.
- **Durable metadata**: All worktree reservations and records persist under `data_dir/managed-worktrees/records/` and `operations/`.

## 2. Tool contracts

The following four tools are registered in `build_tools()` and discoverable under the `workspace` category:

### `local_worktree_create`
- Arguments: `operation_id` (string), `cwd` (string), `base_ref` (string, default `"HEAD"`).
- Creates an isolated managed worktree under the MCP-owned runtime area: `<data_dir>/managed-worktrees/checkouts/<repo>-<hash>/wt_<uuid>`.
- Scopes the worktree to the canonical Git repository root identity `workspace_id = sha256(canonical_repo_root)`.
- Scoped authenticated creation never probes or replays the old public/unscoped reservation key; legacy records remain maintenance-only.
- Returns `worktree_id`, `path`, `source_repo`, `base_commit`, `state`, `created`, `replayed`.

### `local_worktree_status`
- Arguments: `worktree_id` (`wt_[a-f0-9]{24}`), `cwd` (required string).
- Inspects live git status: returns `worktree_id`, `registered`, `exists`, `dirty`, `actual_head`, `base_matches`, `state`.
- Canonicalizes `cwd` to the Git repository root and requires the record's `workspace_id` to match it.

### `local_worktree_list`
- Arguments: `cwd` (required string), `limit` (integer, default 50), optional `cursor` (string).
- Cursor pagination: ordered by `(created_at, worktree_id)` descending. Returns `worktrees`, `next_cursor` (opaque token for the next page), and `has_more` (boolean).
- Returns only worktrees belonging to the authenticated principal in the asserted repository/workspace.

### `local_worktree_remove`
- Arguments: `worktree_id` (`wt_[a-f0-9]{24}`), `cwd` (required string).
- Verifies repository/workspace authority, then requires the worktree to be clean before `git worktree remove`; the durable record is marked `removed`. Replay is a safe no-op.

## 3. Scoping and multi-principal isolation

Worktrees are scoped by:
- Authenticated principal: `principal_hash = sha256(client_id)`
- Workspace: `workspace_id = sha256(canonical_git_repository_root)`
- Scoped reservation ID: `scoped_v1_<sha256(principal_hash + workspace_id + operation_id)>`

Isolation rules:
- **Repo-level workspace identity**: `workspace_id` is rooted at the canonical Git repository root (`git rev-parse --show-toplevel`), preventing scope collision between multiple repositories under the same allowed root.
- **Principal-scoped access**: Principal A cannot view, inspect, or delete scoped worktrees created by Principal B (`worktree_not_found`).
- **Mandatory workspace proof**: `status`, `list`, and `remove` require `cwd`; each call canonicalizes it to a Git repository root and rejects records from any other workspace.
- **Zero-trust legacy isolation**: The authenticated MCP API never exposes unscoped/legacy records. Scoped creation also bypasses the legacy public reservation key, so a legacy record cannot be replayed into an authenticated principal/workspace session.
- **Internal checkout authority**: New checkouts live under `data_dir/managed-worktrees/checkouts`, so creating a managed worktree does not require writing to `repo.parent` outside the configured source authority.
- **Deterministic cursor pagination**: `local_worktree_list` provides stable pagination via `(created_at, worktree_id)` sort keys and `cursor`/`next_cursor`.

## 4. Test evidence

New focused test suites:
- `tests/test_workspace_worktrees.py`: 9 passed
  - ID validation, create/status, idempotent replay/conflict, dirty-source rejection, safe removal, isolated worktrees, internal checkout authority, embedded ignored `data_dir` cleanliness, and an 8-thread same-operation replay race.
- `tests/test_worktree_tool_api.py`: 7 passed
  - Scoped replay, principal isolation, shared operation-id reuse, ToolRegistry discovery/call integration, repo-root workspace identity, mandatory `cwd` authorization/filtering, authenticated create isolation from real legacy unscoped reservations, and deterministic cursor pagination.
- Combined with capability tests: `33 passed`.

Complete repository regression:
```text
uv run pytest -q
442 passed, 11 skipped, 5 warnings in 43.85s
```
Zero failures.
