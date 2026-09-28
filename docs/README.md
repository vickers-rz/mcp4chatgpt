# Documentation index

## Current implementation and operation

- [Architecture and logic](05-architecture-and-logic.md): runtime topology, request flow, configuration, and deployment.
- [Personal full-access implementation and acceptance](26-personal-full-access-plan.md): implemented personal full-access profile covering co-te, shell/rm, raw response output, and full Computer Use while retaining authentication, audit, workspace transactions, durable-job semantics, and capability/backend identity checks.
- [mcpc protocol acceptance](20-mcpc-protocol-acceptance.md): pinned external MCP client protocol tests and OAuth acceptance limits.
- [Capability orchestration design](21-capability-orchestration-design.md): next-stage isolated read-only composition proposal.
- [macOS Computer Use](macos-computer-use.md): CUA/native routing, permissions, allowlists, and trust boundary.
- [Chrome extension bridge](chrome_extension.md): browser extension setup and bridge behavior.
- [Workspace transactions and durable jobs](13-workspace-transactions-and-durable-jobs.md): file recovery and job persistence.
- [Persistent headless Chrome](12-persistent-headless-chrome.md): browser session design and operation.

## Reviews and acceptance

- [Local Web top-level exposure implementation, 2026-09-28](33-local-web-top-level-exposure-2026-09-28.md): stable `read_webpage`, six-tool compact exposure, web-route audit/annotations, failure-no-fallback regressions, and 375-pass Python-suite acceptance.
- [Browser topology and web routing review, 2026-09-28](32-browser-topology-and-web-routing-review-2026-09-28.md): current Extension/CDP topology, compact exposure, live health snapshot, and proposed local-web Skill routing.

- [File operation reconciliation, 2026-09-28](31-file-operation-reconciliation-2026-09-28.md): strict standalone write/patch executor, durable evidence, explicit reconciliation and crash tests; production tool integration remains pending.

- [Operations store prototype, 2026-09-28](30-operations-store-prototype-2026-09-28.md): isolated SQLite operation ledger, idempotency, revision CAS, and process-crash tests; not connected to production tools.

- [Foundation baseline and manual benchmark, 2026-09-28](29-foundation-baseline-and-manual-benchmark-2026-09-28.md): captured baseline, fixed benchmark results, ChatGPT manual comparison kit, and transaction contract v0.1.

- [Transaction improvement plan and feasibility, 2026-09-28](28-transaction-improvement-plan-and-feasibility-2026-09-28.md): proposed guarantees, recovery architecture, phased implementation, cost estimates, and acceptance criteria for files, commands, coding, and GUI.

- [Cloudflare Code Mode reassessment / P0–P3](25-cloudflare-code-mode-reassessment.md): current discovery, benchmark, P3 evidence gates, and implementation record.
- [Discovery fixes, 2026-09-27](24-discovery-fixes-2026-09-27.md): fixes for R1–R8, real downstream updates, and mcpc failure cleanup.

- [Discovery Plan review, 2026-09-27](23-discovery-plan-review-2026-09-27.md): current uncommitted catalog changes, confirmed defects, protocol tests, and remaining acceptance gaps.

- [A1–A4 PDCA fix record](18-pdca-acceptance-fixes.md): follow-up fixes, regression results, and remaining deployment acceptance boundary.

- [Independent acceptance, 2026-09-26](17-acceptance-2026-09-26.md): latest acceptance decision and remaining blockers.

- [Project review, 2026-09-26](15-project-review-2026-09-26.md): pre-fix findings and architecture/documentation review.
- [Reliability fix and acceptance record, 2026-09-26](16-reliability-fix-record-2026-09-26.md): current status for those findings and verification results.
- [CUA acceptance record](14-cua-final-acceptance.md): historical CUA acceptance evidence; consult the dated fix record for current tests.
- [GUI hardening and fault review](11-handoff-macos-gui-hardening-and-fault-review.md): historical review notes.

- [CUA live PDCA acceptance, 2026-09-26](19-cua-live-pdca-2026-09-26.md): owned-window transport acceptance and native/CUA token fix.
- [Tool discovery implementation record](22-capability-discovery-implementation.md): mcpc and full/compact catalog verification.

## Plans and handoffs

- [Mac GUI automation handoff plan](09-gemini-handoff-macos-gui-plan.md)
- [Coding capability handoff plan](10-gemini-handoff-codexpro-coding-plan.md)
- [Browser bridge reference and roadmap](07-browser-bridge-reference-and-roadmap.md)
- [Implementation notes](04-implementation-notes.md)
- Documents `01`–`03`, `06`, and `08` preserve earlier proposals, analyses, and reference investigations; they are historical context, not current implementation contracts.
