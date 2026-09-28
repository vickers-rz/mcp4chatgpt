"""Deterministic capability-search evaluation against the pre-P2 lexical ranker.

Run:
    uv run python -m benchmarks.discovery_search --output benchmarks/discovery_search_results.json
"""
from __future__ import annotations

import argparse
import json
import runpy
import tempfile
from pathlib import Path
from typing import Any

_legacy = runpy.run_path(str(Path(__file__).with_name("read_only.py")))
config_at = _legacy["config_at"]
from mcp4chatgpt.audit import AuditLogger
from mcp4chatgpt.downstream.models import DownstreamToolInfo
from mcp4chatgpt.tools import ToolRegistry


CASES = (
    {"query": "PDF 编辑", "expected": "pdf_insert_text", "category": "pdf"},
    {"query": "文件 写入", "expected": "local_write_file", "source": "local"},
    {"query": "后台任务 start", "expected": "local_start_job", "category": "jobs"},
    {"query": "chrome devtools navigate", "expected": "devtools__navigate", "backend": "chrome_devtools"},
    {"query": "chrome headless navigate", "expected": "headless__navigate", "backend": "chrome_headless"},
)


class BrowserBackends:
    def __init__(self) -> None:
        schema = {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        }
        self.tools = [
            DownstreamToolInfo(
                "navigate", "devtools__navigate", "Navigate a controlled browser target.",
                schema, "chrome_devtools",
            ),
            DownstreamToolInfo(
                "navigate", "headless__navigate", "Navigate a controlled browser target.",
                schema, "chrome_headless",
            ),
        ]

    def get_tools(self):
        return list(self.tools)

    def call_tool(self, name, arguments):
        raise AssertionError("search benchmark must not execute tools")


def legacy_rank(registry: ToolRegistry, query: str, limit: int = 5) -> list[str]:
    """Exact pre-P2 search semantics: all split terms in name+description+source."""
    query_text = query.strip().casefold()
    terms = query_text.split()
    ranked: list[tuple[int, str]] = []
    for name in registry._catalog_names:
        tool = registry.tools[name]
        text = f"{name} {tool.description} {registry._catalog_sources[name]}".casefold()
        if all(term in text for term in terms):
            rank = (
                0 if name.casefold() == query_text
                else 1 if all(term in name.casefold() for term in terms)
                else 2
            )
            ranked.append((rank, name))
    ranked.sort()
    return [name for _, name in ranked[:limit]]


def _metrics(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    total = len(rows)
    top1 = sum(row["expected"] in row[key][:1] for row in rows)
    top5 = sum(row["expected"] in row[key][:5] for row in rows)
    return {
        "cases": total,
        "top1_hits": top1,
        "top5_hits": top5,
        "top1_rate": top1 / total if total else None,
        "top5_rate": top5 / total if total else None,
    }


def run() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="mcp-discovery-benchmark-") as temp:
        root = Path(temp)
        registry = ToolRegistry(
            config_at(root),
            AuditLogger(root / "audit.jsonl"),
            downstream_manager=BrowserBackends(),
        )
        rows: list[dict[str, Any]] = []
        for case in CASES:
            arguments = {
                key: value
                for key, value in case.items()
                if key in {"query", "source", "backend", "category"}
            }
            new_names = [
                item["name"]
                for item in registry._capability_search({**arguments, "limit": 5})["matches"]
            ]
            old_names = legacy_rank(registry, case["query"], 5)
            rows.append({
                **case,
                "legacy_top5": old_names,
                "weighted_top5": new_names,
            })

        legacy = _metrics(rows, "legacy_top5")
        weighted = _metrics(rows, "weighted_top5")
        assert weighted["top1_hits"] >= legacy["top1_hits"]
        assert weighted["top5_hits"] >= legacy["top5_hits"]
        assert weighted["top1_hits"] == len(rows)
        return {
            "kind": "deterministic_discovery_query_set",
            "model_invoked": False,
            "vector_service_used": False,
            "ranking_order": [
                "exact canonical name",
                "name keyword/token",
                "maintained explicit keyword",
                "description",
                "category",
                "backend/source/alias tie-break evidence",
            ],
            "legacy": legacy,
            "weighted": weighted,
            "cases": rows,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run()
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Wrote {args.output}: weighted Top-1 "
        f"{report['weighted']['top1_hits']}/{report['weighted']['cases']}"
    )
