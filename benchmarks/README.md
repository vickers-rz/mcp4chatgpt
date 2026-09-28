# Read-only aggregation benchmark

Run from the repository root:

```sh
uv run python -m benchmarks.read_only_v2 --output benchmarks/read_only_v2_results.json
uv run python -m benchmarks.discovery_search --output benchmarks/discovery_search_results.json
uv run python -m benchmarks.model_e2e --input benchmarks/model_e2e_records.jsonl --output benchmarks/model_e2e_summary.json
```

`read_only.py` is retained as the earlier P0 baseline. New acceptance uses `read_only_v2.py`, which adds explicit failure contracts and normalized metrics.

The harness creates 30 temporary PDFs and a ten-page fake downstream dataset.
It starts no services and contacts no external systems. Leaf calls go through
ToolRegistry with a host-created allowlist and per-capability revision bindings.

Four modes compare direct calls, compact discovery, a dedicated batch reducer,
and fixed orchestration. The last two intentionally use the same trusted
reducer. Neither represents a published batch API or an isolated code executor.
No generated code is accepted.

## Recorded run

| Workload | Target calls | Full MCP result bytes | Direct/compact projected visible bytes | Batch/fixed projected visible bytes |
|---|---:|---:|---:|---:|
| 30 PDFs | 30 | 40,650 | 40,650 | 327 |
| 1,000 rows / 10 pages | 10 | 233,913 | 233,913 | 486 |

All four paths produced equal summaries; known PDF anomalies and row totals
were checked against fixture expectations. Bytes are compact UTF-8 JSON at the
registry MCP envelope boundary, including duplicated text/structured fields.
PDF path lengths can change the byte count on other hosts.

Model-visible bytes are projections. No model is invoked, so model round trips and token counts are null. The v2 report separately times setup, discovery, execution, projection, and total wall time; these timings are local fixture measurements, not a cold/warm model comparison. In the current fixture, full/compact tool-definition bytes are 50,335 / 2,405. capability_list remains directly callable for explicit inventory/debug use but is intentionally omitted from default full/compact tools/list, so adding inventory pagination does not change the normal discovery-tool exposure contract.

The contract scenarios additionally verify partial PDF failure reporting, downstream `isError`, missing `structuredContent`, pagination cycles, and target capability revision changes. The discovery query set compares the exact pre-P2 lexical ranker with the new deterministic weighted ranker: legacy Top-1/Top-5 were 2/5 (40%); weighted Top-1/Top-5 were 5/5 (100%) on the five fixed cases covering PDF editing, file writing, background jobs, Chrome DevTools, and Headless.

Batch tool-definition cost remains undefined because no public batch tool exists. The data demonstrates aggregation savings, but provides no evidence that arbitrary code is better than a dedicated batch operation. The fixtures do not test a production sandbox, resource-reference lifecycle, live model repair behavior, or model-generated program quality. Real client/model E2E evaluation remains required before building a general executor or changing default exposure.

## Real-model E2E record boundary

`model_e2e_tasks.json` defines the fixed short-direct, PDF-batch, and paginated-aggregate task identities. `model_e2e.py` does not invoke a model; it only validates JSONL records emitted by an actual client/model run and aggregates matched observations. Records must identify the exact client/model, dataset, permission profile, catalog version, path/exposure, trial/run order, correctness/completeness, discovery failures, repair rounds, model round trips/tokens, target calls, visible bytes, elapsed time, cold/warm state, and errors. Fixed benchmark/protocol data must not be relabeled as `model_e2e` evidence.
