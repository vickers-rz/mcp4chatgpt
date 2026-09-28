from __future__ import annotations

"""Validation and aggregation for *real* model E2E benchmark records.

This module never calls a model and never synthesizes model telemetry. It is
only an ingestion boundary for records emitted by a real client/model run.
Fixed/protocol benchmarks must not be written with evidence_layer=model_e2e.
"""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


EVIDENCE_LAYER = "model_e2e"
TASK_TYPES = {"short_direct", "pdf_batch", "paginated_aggregate"}
COLD_WARM = {"cold", "warm"}

REQUIRED_FIELDS = {
    "evidence_layer",
    "task_id",
    "task_type",
    "trial",
    "run_order",
    "client",
    "client_version",
    "exact_model",
    "path",
    "exposure",
    "dataset_version",
    "permission_profile",
    "catalog_version",
    "correct",
    "complete",
    "discovery_failures",
    "repair_rounds",
    "model_round_trips",
    "model_input_tokens",
    "model_output_tokens",
    "target_call_count",
    "model_visible_bytes",
    "elapsed_seconds",
    "cold_or_warm",
    "errors",
}

_NONNEGATIVE_INTS = {
    "trial",
    "run_order",
    "discovery_failures",
    "repair_rounds",
    "model_round_trips",
    "model_input_tokens",
    "model_output_tokens",
    "target_call_count",
    "model_visible_bytes",
}

_NONEMPTY_STRINGS = {
    "task_id",
    "client",
    "client_version",
    "exact_model",
    "path",
    "exposure",
    "dataset_version",
    "permission_profile",
    "catalog_version",
}


class RecordValidationError(ValueError):
    pass


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_record(record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise RecordValidationError("record_not_object")

    missing = sorted(REQUIRED_FIELDS - record.keys())
    if missing:
        raise RecordValidationError(f"missing_fields:{','.join(missing)}")

    if record["evidence_layer"] != EVIDENCE_LAYER:
        raise RecordValidationError("evidence_layer_not_model_e2e")

    if record["task_type"] not in TASK_TYPES:
        raise RecordValidationError("task_type_invalid")

    for field in _NONEMPTY_STRINGS:
        value = record[field]
        if not isinstance(value, str) or not value.strip():
            raise RecordValidationError(f"{field}_invalid")

    for field in _NONNEGATIVE_INTS:
        value = record[field]
        if not _is_int(value) or value < 0:
            raise RecordValidationError(f"{field}_invalid")

    # Trial/order are identifiers, not zero-based indexes.
    if record["trial"] < 1:
        raise RecordValidationError("trial_invalid")
    if record["run_order"] < 1:
        raise RecordValidationError("run_order_invalid")

    if type(record["correct"]) is not bool:
        raise RecordValidationError("correct_invalid")
    if type(record["complete"]) is not bool:
        raise RecordValidationError("complete_invalid")

    elapsed = record["elapsed_seconds"]
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or elapsed < 0:
        raise RecordValidationError("elapsed_seconds_invalid")

    if record["cold_or_warm"] not in COLD_WARM:
        raise RecordValidationError("cold_or_warm_invalid")

    errors = record["errors"]
    if not isinstance(errors, list) or any(not isinstance(item, str) for item in errors):
        raise RecordValidationError("errors_invalid")

    # Real model E2E records must carry actual measured telemetry rather than
    # the null placeholders intentionally used by fixed benchmarks.
    for field in (
        "model_round_trips",
        "model_input_tokens",
        "model_output_tokens",
        "repair_rounds",
    ):
        if record[field] is None:
            raise RecordValidationError(f"{field}_missing_real_measurement")

    return record


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
            records.append(validate_record(parsed))
        except (json.JSONDecodeError, RecordValidationError) as exc:
            raise RecordValidationError(f"line_{line_number}:{exc}") from exc
    if not records:
        raise RecordValidationError("no_records")
    return records


def _mean(records: list[dict[str, Any]], field: str) -> float:
    return statistics.fmean(float(record[field]) for record in records)


def summarize(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    checked = [validate_record(dict(record)) for record in records]
    if not checked:
        raise RecordValidationError("no_records")

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in checked:
        groups[(record["task_id"], record["path"], record["exposure"])].append(record)

    group_summaries = []
    for (task_id, path, exposure), rows in sorted(groups.items()):
        group_summaries.append(
            {
                "task_id": task_id,
                "task_type": rows[0]["task_type"],
                "path": path,
                "exposure": exposure,
                "runs": len(rows),
                "correct_runs": sum(1 for row in rows if row["correct"]),
                "complete_runs": sum(1 for row in rows if row["complete"]),
                "mean_discovery_failures": _mean(rows, "discovery_failures"),
                "mean_repair_rounds": _mean(rows, "repair_rounds"),
                "mean_model_round_trips": _mean(rows, "model_round_trips"),
                "mean_model_input_tokens": _mean(rows, "model_input_tokens"),
                "mean_model_output_tokens": _mean(rows, "model_output_tokens"),
                "mean_target_call_count": _mean(rows, "target_call_count"),
                "mean_model_visible_bytes": _mean(rows, "model_visible_bytes"),
                "mean_elapsed_seconds": _mean(rows, "elapsed_seconds"),
            }
        )

    identities = {
        (
            row["client"],
            row["client_version"],
            row["exact_model"],
            row["dataset_version"],
            row["permission_profile"],
            row["catalog_version"],
        )
        for row in checked
    }

    return {
        "kind": "real_model_e2e_records",
        "evidence_layer": EVIDENCE_LAYER,
        "record_count": len(checked),
        "identity_count": len(identities),
        "comparable_identity": len(identities) == 1,
        "groups": group_summaries,
        "notes": [
            "This summary reports observed client/model records only.",
            "It does not rank generic Code Mode against dedicated batch without matched real-model runs.",
            "Protocol acceptance and fixed benchmark results belong to separate evidence layers.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path, help="JSONL records from real client/model runs")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    summary = summarize(load_jsonl(args.input))
    rendered = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
