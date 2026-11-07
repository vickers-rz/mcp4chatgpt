from __future__ import annotations

import json
import runpy
from pathlib import Path

_ROOT = Path(__file__).parents[1] / "benchmarks"
run_discovery_search = runpy.run_path(str(_ROOT / "discovery_search.py"))["run"]
run_read_only_v2 = runpy.run_path(str(_ROOT / "read_only_v2.py"))["run"]


def test_read_only_v2_contract_and_metrics():
    report = run_read_only_v2()
    assert report["model_invoked"] is False
    assert all(item["detected"] for item in report["contract_scenarios"])
    assert all(result["model_round_trips"] is None for result in report["results"])
    assert all(result["model_input_tokens"] is None for result in report["results"])
    assert all(result["model_output_tokens"] is None for result in report["results"])
    assert all(result["call_count"] == result["successful_calls"] + result["failed_calls"]
               for result in report["results"])
    print("READ_ONLY_V2=" + json.dumps(report, ensure_ascii=False, sort_keys=True))


def test_discovery_search_query_set():
    report = run_discovery_search()
    assert report["model_invoked"] is False
    assert report["vector_service_used"] is False
    assert report["weighted"]["top1_hits"] == report["weighted"]["cases"]
    assert report["weighted"]["top1_hits"] >= report["legacy"]["top1_hits"]
    assert report["weighted"]["top5_hits"] >= report["legacy"]["top5_hits"]
    print("DISCOVERY_SEARCH=" + json.dumps(report, ensure_ascii=False, sort_keys=True))


def test_manual_chatgpt_benchmark_collection_consistency():
    report = json.loads((_ROOT / "runs" / "2026-09-29-chatgpt-manual.json").read_text(encoding="utf-8"))
    runs = report["runs"]
    status = report["collection_status"]

    assert status["recorded_runs"] == len(runs)
    assert status["planned_runs"] == len(runs) + len(status["remaining"])

    run_keys = {(item["exposure"], item["trial"], item["task_id"]) for item in runs}
    remaining_keys = {
        (item["exposure"], item["trial"], item["task_id"])
        for item in status["remaining"]
    }
    assert len(run_keys) == len(runs)
    assert run_keys.isdisjoint(remaining_keys)
    assert all(item["fresh_chat"] is True for item in runs)
    assert all(item["complete"] is True for item in runs)
