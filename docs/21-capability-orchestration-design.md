# Capability orchestration: next-stage design

## Purpose and entry criteria

This note designs a later, isolated code-orchestration capability. It does not
add an execute tool or select/install an execution runtime. Begin implementation
only after the capability catalog is accepted by the intended MCP clients and
measurements show that a chosen workflow spends material time or context on
repeated calls or intermediate data.

The first release must be read-only batch processing. GUI/native operations,
writes, commands, and arbitrary shell access are excluded.

## Proposed topology

```text
Agent
  │ compact program + bounded arguments
  ▼
Isolated execution worker
  │ narrow RPC; no credentials
  ▼
MCP4ChatGPT capability bridge
  │ allowlisted operation + validated input
  ▼
ToolRegistry → existing business handler → existing audit
```

The worker receives a run identifier, a short-lived opaque bridge handle, a
small read-only capability manifest, and the submitted program. It cannot read
OAuth bearer tokens, server environment variables, project files, user home,
browser profile, or arbitrary network destinations. Bridge authorization is
checked per call; it does not rely on the worker honoring the manifest.

The allowlist is maintainer-controlled configuration keyed by canonical tool
name. Never infer read-only permission solely from MCP annotations or a
downstream server's `readOnlyHint`. The bridge calls the same registry path,
input-schema validation, and audit channel as direct capability calls.

## Run contract and limits

The future API should accept a source program, an allowlist profile, and
explicit resource limits. A run returns a run id, terminal state, bounded
structured result, and per-operation summary. The exact public schema is to be
finalized alongside a selected runtime; required controls are:

- one absolute wall-clock deadline shared by compilation, execution, and bridge calls;
- fixed maximum bridge-call count, bounded concurrency, and per-call timeout;
- memory/CPU limit enforced by the execution platform;
- maximum serialized result bytes and text characters;
- cancellation that terminates the worker and prevents new bridge calls;
- explicit partial results with completed/failed operation summaries;
- no automatic retry after an unknown or side-effecting outcome.

Large files and images are represented by scoped resource references. Never
send base64 image/file payloads through the program result or duplicate them in
the model context. Each reference is bound to the run, expires with it, and is
read-only in the first release.

Programs and their result summaries are retained only as long as needed for
audit and debugging. Logs record the program hash, allowlist profile, limits,
tool names, arguments after secret redaction, and outcome; they must not include
OAuth credentials or full binary payloads.

## Initial workflows

1. **PDF metadata batch:** inspect a bounded list of PDFs under allowed roots,
   return title/page count/page dimensions and per-file errors. No text
   extraction, redaction, insertion, or writes.
2. **Downstream read query:** call an explicit configured list of read-only
   downstream operations, filter and aggregate rows inside the worker, and
   return a small summary plus stable source references.

For each pilot, compare baseline and composed runs using the same fixtures:
model round trips, bytes sent to the model, elapsed time, call count, output
size, failures, and cancellation behavior. Continue only if reduced round trips
or intermediate output outweigh the extra runtime and debugging cost.

## Runtime evaluation

Evaluate two categories independently:

- **Cloudflare isolated Workers:** assess network reachability from the worker
  to a local Mac service, private ingress/authentication, data residency,
  worker lifetime, and per-run cost. An existing Cloudflare Tunnel alone is
  not proof that the worker can securely reach this local bridge.
- **Local isolated worker:** assess a maintained OS/container/VM sandbox,
  syscall/network restrictions, filesystem isolation, update burden, resource
  accounting, and macOS compatibility. Python `exec`, Node `vm`, ordinary
  shell, and mcpc credential proxy do not provide this isolation.

No runtime should be chosen until both categories have a threat model, a
working bridge prototype with no credential exposure, limit-enforcement tests,
and operational cost estimates. Keep GUI execution in the existing local
computer subsystem; never tunnel it through arbitrary generated code.
