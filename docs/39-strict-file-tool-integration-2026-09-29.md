# Strict file operation MCP integration

Date: 2026-09-29. This records the first registered integration of the isolated
operation ledger and file executor. It does not replace the legacy file tools.

## Tool contract

- `local_strict_write_file(operation_id, path, content, expected_sha256, full_replace_token, allow_large_reduction=false)` replaces an **existing** ordinary UTF-8 file.
- `local_strict_apply_patch(operation_id, path, old, new, expected_sha256)` replaces the first exact anchor in an **existing** ordinary UTF-8 file.
- `local_operation_status(operation_id, path)` reads the persisted result without execution or reconciliation.
- `local_operation_reconcile(operation_id, path)` explicitly compares evidence and current file identity; it never writes or replays the target.

The caller obtains `expected_sha256` and, for whole-file replacement, `full_replace_token`
from a complete `local_read_text` result. Keep the same `operation_id`, path and arguments
when retrying after a lost response. Changing an argument with the same identity returns
`idempotency_conflict`. A new ID is a new operation, not a retry.

All four capabilities are available through discovery in compact exposure. They bind
the operation key to the authenticated MCP client identity (or `local` for a local
no-auth instance), installation key and allowed workspace root. The SQLite ledger
and evidence live under `MCP_DATA_DIR/operations`; the full-read proof key remains
in the existing audit state directory. The registered path was verified through
`ToolRegistry.call_tool`, including replay, principal isolation, status, conflicts and
extended-attribute rejection. A loopback HTTP `/mcp` `tools/call` round trip also
verified strict patch and status through the actual protocol handler.

## Current limits and rollout gate

This first integration deliberately supports only existing, singly linked ordinary
text files up to 16 MiB. It rejects non-current ownership, special mode bits and
user extended attributes rather than discarding unsupported metadata. On macOS,
the automatically attached `com.apple.provenance` attribute is exempted. Creation,
deletion, renaming, binary files, ACL preservation, arbitrary xattrs and hostile
same-user races remain outside the strict contract. A failure after replacement
may return `needs_reconciliation`; clients must query/reconcile the same operation
instead of issuing a new write.

Evidence images remain on disk until an operator runs
`uv run python scripts/prune_operation_images.py --state-dir /path/to/MCP_DATA_DIR/operations`
(dry-run) and repeats it with `--apply`. Only terminal operations older than 90
days are eligible; ledger rows, manifests, receipts and a durable pruning marker
remain. Uncertain operations and older records without a terminal timestamp are
never pruned. Before broad production use, verify backup/restore on the intended
runtime and perform a controlled service rollout. The
maintenance script reports retained image bytes, free disk space and a warning
at 1 GiB or under 32 MiB free; it does not remove anything without `--apply`.
The independent fault
tests in [document 31](31-file-operation-reconciliation-2026-09-28.md) validate
process-crash windows, not power-loss safety.

Jobs are still managed by their existing durable JSON protocol; unification with
the operation ledger remains separate work. The safe public `compact` process
was restarted without changing its permission profile. A signed loopback MCP
smoke verified six listed tools, one strict patch, same-ID replay and status;
the synthetic target file was removed. Its ledger row and 11 bytes of retained
before/after images remain as normal operation evidence. No existing operation
data was migrated. This is a controlled smoke, not a full production recovery
drill or power-loss test.
