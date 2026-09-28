"""Trusted fixed workloads only. No generated code, executor, or public tools.

Run: PYTHONPATH=src .venv/bin/python benchmarks/read_only.py --output report.json
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import pymupdf

from mcp4chatgpt.audit import AuditLogger
from mcp4chatgpt.config import Config
from mcp4chatgpt.downstream.models import DownstreamToolInfo
from mcp4chatgpt.tools import CallContext, ToolRegistry


def byte_size(value):
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def config_at(root):
    return Config(
        public_base_url="http://127.0.0.1", bind_host="127.0.0.1", bind_port=0,
        auth_secret="benchmark-unused", allowed_roots=[root], co_te_path=root / "unused",
        data_dir=root, audit_log=root / "audit.jsonl", firecrawl_api_key="",
        firecrawl_base_url="", brave_api_key="", brave_base_url="",
        open_webui_search_default_engine="", knowledge_roots=[root], knowledge_store_dir=root,
        tls_cert_path="", tls_key_path="", max_output_chars=10000,
        log_rotate_bytes=20000000, log_retention_days=1, allowed_hosts=["127.0.0.1"],
        local_auth_disabled=False, modern_protocol_enabled=True, ext_bridge_port=0,
        ext_screenshot_dir=root,
    )


class Pages:
    def get_tools(self):
        return [DownstreamToolInfo("page", "fixture__page", "Read fixture rows", {
            "type": "object", "properties": {"cursor": {"type": "integer", "minimum": 0, "maximum": 9}},
            "required": ["cursor"], "additionalProperties": False,
        }, "fixture")]

    def call_tool(self, name, arguments):
        cursor = arguments["cursor"]
        return {"content": [], "structuredContent": {
            "rows": [{"id": i, "score": i % 17, "detail": "x" * 200} for i in range(cursor * 100, (cursor + 1) * 100)],
            "next_cursor": cursor + 1 if cursor < 9 else None,
        }}


def pdf_summary(call, paths):
    abnormal, errors = [], []
    for path in paths:
        try:
            data = call("pdf_inspect", {"path": str(path), "max_chars": 100})
            if data["page_count"] != 2 or any(abs(p["width"] - 595) > 1 or abs(p["height"] - 842) > 1 for p in data["pages"]):
                abnormal.append(path.name)
        except (ValueError, RuntimeError) as exc:
            errors.append({"file": path.name, "error": type(exc).__name__})
    return {"checked": len(paths), "abnormal": abnormal, "errors": errors}


def page_summary(call):
    cursor, count, total, best, seen = 0, 0, 0, [], set()
    while cursor is not None:
        if cursor in seen or len(seen) >= 10:
            raise ValueError("pagination_limit")
        seen.add(cursor)
        data = call("fixture__page", {"cursor": cursor})
        rows = data["rows"]
        count += len(rows)
        total += sum(row["score"] for row in rows)
        best = sorted(best + [{"id": r["id"], "score": r["score"]} for r in rows], key=lambda r: (-r["score"], r["id"]))[:5]
        cursor = data["next_cursor"]
    return {"count": count, "score_total": total, "top5": best}


def measure(config, workload, paths, mode):
    registry = ToolRegistry(replace(config, tool_exposure="compact" if mode == "compact" else "full"),
                            AuditLogger(config.audit_log), downstream_manager=Pages())
    name = "pdf_inspect" if workload == "pdf" else "fixture__page"
    context = CallContext(run_id=f"{workload}:{mode}", entrypoint="benchmark",
                          allowed_tools=frozenset({name}), capability_bindings=registry.bind_capabilities((name,)),
                          deadline=time.monotonic() + 60)
    definition_bytes = byte_size(registry.list_tools(auth_required=False))
    discovery_bytes = 0
    if mode == "compact":
        discovery_bytes = sum(byte_size(registry.call_tool(tool, args)) for tool, args in (
            ("capability_search", {"query": name}), ("capability_get", {"name": name})))
    backend_bytes = visible_bytes = calls = 0
    start = time.perf_counter()

    def call(tool, arguments):
        nonlocal backend_bytes, visible_bytes, calls
        if calls >= 100:
            raise ValueError("call_budget_exceeded")
        calls += 1
        if mode == "compact":
            result = registry.call_tool("capability_call", {"name": tool, "arguments": arguments}, context=context)
        else:
            result = registry.call_tool(tool, arguments, context=context)
        size = byte_size(result)
        backend_bytes += size
        if mode in {"direct", "compact"}:
            visible_bytes += size
        if result.get("isError") or not isinstance(result.get("structuredContent"), dict):
            raise ValueError("unexpected_result_contract")
        return result["structuredContent"]

    # The dedicated batch and fixed orchestration intentionally share the same
    # reducer: this control isolates aggregation benefits from generated code.
    summary = pdf_summary(call, paths) if workload == "pdf" else page_summary(call)
    if mode in {"dedicated_batch", "fixed_orchestration"}:
        visible_bytes = byte_size({"structuredContent": summary, "content": [{"type": "text", "text": json.dumps(summary, ensure_ascii=False)}]})
    return {
        "workload": workload, "mode": mode, "backend_calls": calls,
        "backend_result_bytes": backend_bytes, "projected_model_visible_result_bytes": visible_bytes,
        "definition_bytes": definition_bytes if mode in {"direct", "compact"} else None,
        "discovery_result_bytes": discovery_bytes,
        "elapsed_seconds": time.perf_counter() - start, "model_round_trips": None,
        "summary": summary,
    }


def run():
    with tempfile.TemporaryDirectory(prefix="mcp-readonly-benchmark-") as temp:
        root = Path(temp)
        paths = []
        for i in range(30):
            path = root / f"sample-{i:02d}.pdf"
            with pymupdf.open() as doc:
                for _ in range(2):
                    doc.new_page(width=612 if i % 10 == 0 else 595, height=842)
                doc.set_metadata({"title": path.stem})
                doc.save(path)
            paths.append(path)
        results = [measure(config_at(root), workload, paths, mode)
                   for workload in ("pdf", "pages")
                   for mode in ("direct", "compact", "dedicated_batch", "fixed_orchestration")]
        for workload in ("pdf", "pages"):
            group = [r for r in results if r["workload"] == workload]
            assert all(r["summary"] == group[0]["summary"] for r in group)
        assert results[0]["summary"] == {"checked": 30, "abnormal": ["sample-00.pdf", "sample-10.pdf", "sample-20.pdf"], "errors": []}
        assert results[4]["summary"]["count"] == 1000
        assert results[4]["summary"]["score_total"] == sum(i % 17 for i in range(1000))
        return {"kind": "fixed_workload_data_path", "model_invoked": False,
                "byte_boundary": "serialized registry MCP response, including text/structured duplication",
                "notes": ["No executor, model, network, or public batch tool is used.",
                          "Model-visible bytes are projections, not client telemetry.",
                          "Batch and orchestration use the same trusted reducer; equal results do not establish Code Mode benefit.",
                          "Elapsed time excludes discovery, model inference, and sandbox startup; no cold/warm comparison."],
                "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Wrote {args.output}: {len(report['results'])} measurements; result equivalence verified")
