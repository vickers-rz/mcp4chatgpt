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
