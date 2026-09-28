"""P0 fixed-workload benchmark with explicit failure/measurement contracts.

This supersedes the original read_only.py runner for new measurements while
reusing its isolated Config fixture. It does not call a model, network service,
generated-code executor, or production MCP service.

Run:
    uv run python -m benchmarks.read_only_v2 --output benchmarks/read_only_results.json
"""
from __future__ import annotations

import argparse
import json
import runpy
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pymupdf

_legacy = runpy.run_path(str(Path(__file__).with_name("read_only.py")))
config_at = _legacy["config_at"]
from mcp4chatgpt.audit import AuditLogger
from mcp4chatgpt.downstream.models import DownstreamToolInfo
from mcp4chatgpt.tools import CallContext, ToolRegistry


def byte_size(value: Any) -> int:
    return len(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))


class Pages:
    """Deterministic ten-page downstream fixture (100 rows/page)."""

    def __init__(self) -> None:
        self.tools = [DownstreamToolInfo(
            "page",
            "fixture__page",
            "Read fixture rows",
            {
                "type": "object",
                "properties": {"cursor": {"type": "integer", "minimum": 0, "maximum": 9}},
                "required": ["cursor"],
                "additionalProperties": False,
            },
            "fixture",
        )]

    def get_tools(self):
        return list(self.tools)

    def call_tool(self, name, arguments):
        cursor = arguments["cursor"]
        return {
            "content": [],
            "structuredContent": {
                "rows": [
                    {"id": i, "score": i % 17, "detail": "x" * 200}
                    for i in range(cursor * 100, (cursor + 1) * 100)
                ],
                "next_cursor": cursor + 1 if cursor < 9 else None,
            },
        }


class ContractDownstream:
    """Small mutable fixture used only for explicit result/version failures."""

    def __init__(self, behavior: str = "ok") -> None:
        self.behavior = behavior
        self.tools = [self._tool("integer")]

    @staticmethod
    def _tool(cursor_type: str) -> DownstreamToolInfo:
        return DownstreamToolInfo(
            "page",
            "fixture__page",
            "Read contract fixture rows",
            {
                "type": "object",
                "properties": {"cursor": {"type": cursor_type}},
                "required": ["cursor"],
                "additionalProperties": False,
            },
            "fixture",
        )

    def get_tools(self):
        return list(self.tools)

    def call_tool(self, name, arguments):
        if self.behavior == "is_error":
            return {
                "isError": True,
                "content": [{"type": "text", "text": "controlled downstream failure"}],
                "structuredContent": {"error": "controlled"},
            }
        if self.behavior == "missing_structured":
            return {"content": [{"type": "text", "text": "no structured payload"}]}
        if self.behavior == "cycle":
            return {"content": [], "structuredContent": {"rows": [], "next_cursor": 0}}
        return {"content": [], "structuredContent": {"rows": [], "next_cursor": None}}


def _unwrap_result(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("isError") is True:
        raise ValueError("downstream_is_error")
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        raise ValueError("missing_structured_result")
    return structured


def pdf_summary(call, paths: list[Path]) -> dict[str, Any]:
    abnormal: list[str] = []
    errors: list[dict[str, str]] = []
    completed = 0
    for path in paths:
        try:
            data = call("pdf_inspect", {"path": str(path), "max_chars": 100})
            completed += 1
            if data["page_count"] != 2 or any(
                abs(page["width"] - 595) > 1 or abs(page["height"] - 842) > 1
                for page in data["pages"]
            ):
                abnormal.append(path.name)
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append({"file": path.name, "error": type(exc).__name__})
    return {
        "requested": len(paths),
        "completed": completed,
        "abnormal": abnormal,
        "errors": errors,
        "complete": not errors,
    }


def page_summary(call) -> dict[str, Any]:
    cursor: int | None = 0
    count = 0
    total = 0
    best: list[dict[str, int]] = []
    seen: set[int] = set()
    while cursor is not None:
        if cursor in seen:
            raise ValueError("pagination_cycle")
        if len(seen) >= 10:
            raise ValueError("pagination_limit")
        seen.add(cursor)
        data = call("fixture__page", {"cursor": cursor})
        rows = data["rows"]
        count += len(rows)
        total += sum(row["score"] for row in rows)
        best = sorted(
            best + [{"id": row["id"], "score": row["score"]} for row in rows],
            key=lambda row: (-row["score"], row["id"]),
        )[:5]
        cursor = data["next_cursor"]
    return {
        "count": count,
        "score_total": total,
        "top5": best,
        "pages_completed": len(seen),
        "complete": True,
    }


def measure(config, workload: str, paths: list[Path], mode: str) -> dict[str, Any]:
    total_start = time.perf_counter()

    setup_start = time.perf_counter()
    manager = Pages()
    registry = ToolRegistry(
        replace(config, tool_exposure="compact" if mode == "compact" else "full"),
        AuditLogger(config.audit_log),
        downstream_manager=manager,
    )
    name = "pdf_inspect" if workload == "pdf" else "fixture__page"
    context = CallContext(
        run_id=f"{workload}:{mode}",
        entrypoint="benchmark",
        allowed_tools=frozenset({name}),
        capability_bindings=registry.bind_capabilities((name,)),
        deadline=time.monotonic() + 60,
    )
    setup_seconds = time.perf_counter() - setup_start

    discovery_start = time.perf_counter()
    definition_bytes: int | None = None
    discovery_result_bytes = 0
    discovery_calls = 0
    if mode in {"direct", "compact"}:
        definition_bytes = byte_size(registry.list_tools(auth_required=False))
    if mode == "compact":
        for tool, arguments in (
            ("capability_search", {"query": name}),
            ("capability_get", {"name": name}),
        ):
            discovery_result_bytes += byte_size(registry.call_tool(tool, arguments))
            discovery_calls += 1
    discovery_seconds = time.perf_counter() - discovery_start

    mcp_result_bytes = 0
    projected_visible_bytes = 0
    call_count = 0
    successful_calls = 0
    failed_calls = 0

    def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal mcp_result_bytes, projected_visible_bytes
        nonlocal call_count, successful_calls, failed_calls
        if call_count >= 100:
            failed_calls += 1
            raise ValueError("call_budget_exceeded")
        call_count += 1
        try:
            if mode == "compact":
                result = registry.call_tool(
                    "capability_call",
                    {"name": tool, "arguments": arguments},
                    context=context,
                )
            else:
                result = registry.call_tool(tool, arguments, context=context)
        except Exception:
            failed_calls += 1
            raise

        size = byte_size(result)
        mcp_result_bytes += size
        if mode in {"direct", "compact"}:
            projected_visible_bytes += size
        try:
            structured = _unwrap_result(result)
        except ValueError:
            failed_calls += 1
            raise
        successful_calls += 1
        return structured

    execution_start = time.perf_counter()
    summary = pdf_summary(call, paths) if workload == "pdf" else page_summary(call)
    execution_seconds = time.perf_counter() - execution_start

    projection_start = time.perf_counter()
    if mode in {"dedicated_batch", "fixed_orchestration"}:
        projected_visible_bytes = byte_size({
            "structuredContent": summary,
            "content": [{"type": "text", "text": json.dumps(summary, ensure_ascii=False)}],
        })
    projection_seconds = time.perf_counter() - projection_start

    return {
        "workload": workload,
        "mode": mode,
        "call_count": call_count,
        "successful_calls": successful_calls,
        "failed_calls": failed_calls,
        "mcp_result_bytes": mcp_result_bytes,
        "projected_model_visible_bytes": projected_visible_bytes,
        "discovery_cost": {
            "definition_bytes": definition_bytes,
            "result_bytes": discovery_result_bytes,
            "calls": discovery_calls,
        },
        "stage_seconds": {
            "setup": setup_seconds,
            "discovery": discovery_seconds,
            "execution": execution_seconds,
            "projection": projection_seconds,
            "total": time.perf_counter() - total_start,
        },
        "model_round_trips": None,
        "model_input_tokens": None,
        "model_output_tokens": None,
        "summary": summary,
    }


def run_contract_scenarios(root: Path, sample_path: Path) -> list[dict[str, Any]]:
    config = config_at(root)
    scenarios: list[dict[str, Any]] = []

    # Partial local workload: one valid PDF plus one missing target. The summary
    # must state that the workload is incomplete instead of presenting a total.
    local_registry = ToolRegistry(
        config,
        AuditLogger(config.audit_log),
        downstream_manager=ContractDownstream(),
    )

    def local_call(tool, arguments):
        return _unwrap_result(local_registry.call_tool(tool, arguments))

    partial = pdf_summary(local_call, [sample_path, root / "missing.pdf"])
    scenarios.append({
        "name": "partial_pdf_failure",
        "detected": partial["complete"] is False and partial["completed"] == 1,
        "summary": partial,
    })

    for behavior, expected in (
        ("is_error", "downstream_is_error"),
        ("missing_structured", "missing_structured_result"),
    ):
        manager = ContractDownstream(behavior)
        registry = ToolRegistry(config, AuditLogger(config.audit_log), downstream_manager=manager)
        error = None
        try:
            _unwrap_result(registry.call_tool("fixture__page", {"cursor": 0}))
        except ValueError as exc:
            error = str(exc)
        scenarios.append({
            "name": behavior,
            "detected": error == expected,
            "error": error,
        })

    manager = ContractDownstream("cycle")
    registry = ToolRegistry(config, AuditLogger(config.audit_log), downstream_manager=manager)
    cycle_error = None
    try:
        page_summary(lambda tool, args: _unwrap_result(registry.call_tool(tool, args)))
    except ValueError as exc:
        cycle_error = str(exc)
    scenarios.append({
        "name": "pagination_cycle",
        "detected": cycle_error == "pagination_cycle",
        "error": cycle_error,
    })

    manager = ContractDownstream("ok")
    registry = ToolRegistry(config, AuditLogger(config.audit_log), downstream_manager=manager)
    binding = registry.bind_capabilities(("fixture__page",))
    context = CallContext(
        run_id="contract:version-change",
        entrypoint="benchmark",
        allowed_tools=frozenset({"fixture__page"}),
        capability_bindings=binding,
        deadline=time.monotonic() + 60,
    )
    manager.tools = [manager._tool("string")]
    registry.refresh_catalog()
    version_error = None
    try:
        registry.call_tool(
            "capability_call",
            {"name": "fixture__page", "arguments": {"cursor": "0"}},
            context=context,
        )
    except ValueError as exc:
        version_error = str(exc)
    scenarios.append({
        "name": "target_version_change",
        "detected": version_error == "capability_revision_changed",
        "error": version_error,
    })

    return scenarios


def run() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="mcp-readonly-benchmark-") as temp:
        root = Path(temp)
        paths: list[Path] = []
        for index in range(30):
            path = root / f"sample-{index:02d}.pdf"
            with pymupdf.open() as document:
                for _ in range(2):
                    document.new_page(width=612 if index % 10 == 0 else 595, height=842)
                document.set_metadata({"title": path.stem})
                document.save(path)
            paths.append(path)

        results = [
            measure(config_at(root), workload, paths, mode)
            for workload in ("pdf", "pages")
            for mode in ("direct", "compact", "dedicated_batch", "fixed_orchestration")
        ]
        for workload in ("pdf", "pages"):
            group = [item for item in results if item["workload"] == workload]
            assert all(item["summary"] == group[0]["summary"] for item in group)

        expected_abnormal = ["sample-00.pdf", "sample-10.pdf", "sample-20.pdf"]
        assert results[0]["summary"]["abnormal"] == expected_abnormal
        assert results[0]["summary"]["complete"] is True
        assert results[4]["summary"]["count"] == 1000
        assert results[4]["summary"]["score_total"] == sum(index % 17 for index in range(1000))

        contract_scenarios = run_contract_scenarios(root, paths[1])
        assert all(item["detected"] for item in contract_scenarios)

        return {
            "kind": "fixed_workload_data_path_v2",
            "model_invoked": False,
            "metric_contract": {
                "call_count": "target MCP calls only; discovery calls are reported separately",
                "successful_calls": "calls with isError != true and dict structuredContent",
                "failed_calls": "target calls that raise or violate the MCP result contract",
                "mcp_result_bytes": "serialized complete MCP tool results returned by the registry",
                "projected_model_visible_bytes": "projection only; not client telemetry",
                "discovery_cost": "tool definition bytes plus progressive discovery result bytes/calls",
                "stage_seconds": "setup/discovery/execution/projection/total wall time",
                "model_round_trips": None,
                "model_tokens": None,
            },
            "notes": [
                "No executor, model, network, or public batch tool is used.",
                "Model-visible bytes are projections, not client telemetry.",
                "Dedicated batch and fixed orchestration share the same trusted reducer; equality does not establish Code Mode benefit.",
                "Protocol acceptance and real-model E2E evaluation are separate evidence layers.",
            ],
            "contract_scenarios": contract_scenarios,
            "results": results,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = run()
    arguments.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Wrote {arguments.output}: {len(report['results'])} measurements; "
        f"{len(report['contract_scenarios'])} contract scenarios verified"
    )
