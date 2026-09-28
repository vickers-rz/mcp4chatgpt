from __future__ import annotations

import runpy
from pathlib import Path

import pytest

_MODEL_E2E = runpy.run_path(str(Path(__file__).parents[1] / "benchmarks" / "model_e2e.py"))
RecordValidationError = _MODEL_E2E["RecordValidationError"]
summarize = _MODEL_E2E["summarize"]
validate_record = _MODEL_E2E["validate_record"]


def _record(**overrides):
    record = {
        "evidence_layer": "model_e2e",
        "task_id": "short-direct-001",
        "task_type": "short_direct",
        "trial": 1,
        "run_order": 1,
        "client": "example-client",
        "client_version": "1.0",
        "exact_model": "example-model",
        "path": "direct_full",
        "exposure": "full",
        "dataset_version": "fixture-v1",
        "permission_profile": "read-only",
        "catalog_version": "abc123",
        "correct": True,
        "complete": True,
        "discovery_failures": 0,
        "repair_rounds": 0,
        "model_round_trips": 2,
        "model_input_tokens": 100,
        "model_output_tokens": 25,
        "target_call_count": 1,
        "model_visible_bytes": 2048,
        "elapsed_seconds": 1.25,
        "cold_or_warm": "cold",
        "errors": [],
    }
    record.update(overrides)
    return record


def test_model_e2e_record_requires_real_measurements():
    assert validate_record(_record())["exact_model"] == "example-model"

    with pytest.raises(RecordValidationError, match="model_round_trips_invalid"):
        validate_record(_record(model_round_trips=None))

    with pytest.raises(RecordValidationError, match="evidence_layer_not_model_e2e"):
        validate_record(_record(evidence_layer="fixed_benchmark"))

    with pytest.raises(RecordValidationError, match="trial_invalid"):
        validate_record(_record(trial=0))


def test_model_e2e_summary_keeps_identity_and_paths_explicit():
    records = [
        _record(path="direct_full", exposure="full", run_order=1),
        _record(
            path="progressive_discovery",
            exposure="compact",
            run_order=2,
            discovery_failures=1,
            model_round_trips=3,
            model_input_tokens=120,
        ),
    ]
    summary = summarize(records)
    assert summary["kind"] == "real_model_e2e_records"
    assert summary["comparable_identity"] is True
    assert summary["identity_count"] == 1
    assert [group["path"] for group in summary["groups"]] == [
        "direct_full",
        "progressive_discovery",
    ]
    assert summary["groups"][1]["mean_discovery_failures"] == 1.0


def test_model_e2e_summary_flags_unmatched_identity():
    summary = summarize([
        _record(path="direct_full"),
        _record(path="progressive_discovery", catalog_version="different"),
    ])
    assert summary["comparable_identity"] is False
    assert summary["identity_count"] == 2


def test_model_e2e_task_manifest_keeps_generic_executor_out():
    import json

    manifest = json.loads(
        (Path(__file__).parents[1] / "benchmarks" / "model_e2e_tasks.json").read_text(encoding="utf-8")
    )
    tasks = manifest["tasks"]
    assert [task["task_type"] for task in tasks] == [
        "short_direct",
        "pdf_batch",
        "paginated_aggregate",
    ]
    assert len({task["task_id"] for task in tasks}) == len(tasks)
    assert all("generic_code" not in task["required_paths"] for task in tasks)
