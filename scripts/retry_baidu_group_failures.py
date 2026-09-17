#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import time
from pathlib import Path
from typing import Any


def load_exporter(project_root: Path):
    path = project_root / "scripts" / "export_baidu_group_tree.py"
    spec = importlib.util.spec_from_file_location("baidu_exporter", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load exporter: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def error_key(error: dict[str, Any]) -> str:
    return f"{error.get('path','')}|{int(error.get('page', 1) or 1)}"


def work_key(work: dict[str, Any]) -> str:
    return f"{work.get('path','')}|{work.get('fs_id','')}|{work.get('msg_id','')}|{int(work.get('page',1) or 1)}"


def load_main_nodes(state_path: Path, state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Read the append-only node log, with fallback for legacy checkpoints."""
    nodes = state.get("nodes")
    if isinstance(nodes, dict):
        return nodes
    node_path = state_path.with_suffix(".nodes.jsonl")
    loaded: dict[str, dict[str, Any]] = {}
    if node_path.exists():
        with node_path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    if isinstance(record.get("path"), str) and isinstance(record.get("node"), dict):
                        loaded[record["path"]] = record["node"]
                except (json.JSONDecodeError, TypeError):
                    continue
    return loaded


def main() -> int:
    parser = argparse.ArgumentParser(description="Retry failed Baidu group branches on a second Chrome page")
    parser.add_argument("--main-state", required=True)
    parser.add_argument("--page-id", required=True, type=int)
    parser.add_argument("--retry-state", required=True)
    parser.add_argument("--poll-sec", type=float, default=3.0)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--main-pid", type=int)
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    exporter = load_exporter(project_root)
    main_state_path = Path(args.main_state).expanduser().resolve()
    retry_state_path = Path(args.retry_state).expanduser().resolve()

    if retry_state_path.exists():
        retry = json.loads(retry_state_path.read_text(encoding="utf-8"))
    else:
        retry = {
            "version": 1,
            "page_id": args.page_id,
            "seen_errors": [],
            "recovered_errors": [],
            "unresolved_errors": [],
            "queue": [],
            "completed_pages": [],
            "nodes": {},
            "started_at": time.time(),
            "updated_at": time.time(),
        }

    seen_errors = set(retry.get("seen_errors", []))
    recovered_errors = set(retry.get("recovered_errors", []))
    completed_pages = set(retry.get("completed_pages", []))

    def main_alive() -> bool:
        if args.main_pid is None:
            return True
        try:
            os.kill(args.main_pid, 0)
            return True
        except OSError:
            return False

    idle_after_main_exit = 0
    while True:
        main_state = json.loads(main_state_path.read_text(encoding="utf-8"))
        main_nodes = load_main_nodes(main_state_path, main_state)

        # Discover newly failed branches from the primary crawler.
        for err in main_state.get("errors", []):
            ek = error_key(err)
            if ek in seen_errors:
                continue
            seen_errors.add(ek)
            node = main_nodes.get(err.get("path"))
            if not isinstance(node, dict):
                retry.setdefault("unresolved_errors", []).append({
                    "key": ek,
                    "path": err.get("path"),
                    "page": err.get("page", 1),
                    "error": "failed branch node is missing from main checkpoint",
                    "source_error": err.get("error"),
                    "time": time.time(),
                })
                continue
            work = exporter.item_to_work(node)
            work["page"] = int(err.get("page", 1) or 1)
            work["source_error_key"] = ek
            wk = work_key(work)
            queued = {work_key(q) for q in retry.get("queue", [])}
            if wk not in completed_pages and wk not in queued:
                retry.setdefault("queue", []).append(work)
                print(f"queued retry: {ek}", flush=True)

        # Process a small retry batch on the dedicated Chrome page.
        batch: list[dict[str, Any]] = []
        while retry.get("queue") and len(batch) < max(1, min(args.batch_size, 16)):
            work = retry["queue"].pop(0)
            wk = work_key(work)
            if wk in completed_pages:
                continue
            batch.append(work)

        if batch:
            results: list[dict[str, Any]] | None = None
            last_error: Exception | None = None
            for attempt in range(1, max(1, args.max_attempts) + 1):
                try:
                    results = exporter.fetch_directory_pages(
                        args.page_id,
                        batch,
                        concurrency=max(1, args.concurrency),
                    )
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    print(f"retry batch attempt {attempt}/{args.max_attempts} failed: {exc}", flush=True)
                    time.sleep(min(attempt, 3))

            if results is None:
                results = [{"error": str(last_error or "unknown retry failure")} for _ in batch]

            queued_keys = {work_key(q) for q in retry.get("queue", [])}
            for work, result in zip(batch, results):
                wk = work_key(work)
                ek = str(work.get("source_error_key") or "")
                if not isinstance(result, dict) or result.get("error"):
                    retry.setdefault("unresolved_errors", []).append({
                        "key": ek,
                        "path": work.get("path"),
                        "page": work.get("page", 1),
                        "error": str((result or {}).get("error") or last_error or "unknown retry failure"),
                        "time": time.time(),
                    })
                    completed_pages.add(wk)
                    print(f"retry still failed: {work.get('path')} page={work.get('page',1)}", flush=True)
                    continue

                completed_pages.add(wk)
                if ek:
                    recovered_errors.add(ek)
                items = result.get("items") or []
                for item in items:
                    path = str(item.get("path") or "")
                    if not path:
                        parent = str(work.get("path") or "").rstrip("/")
                        item["path"] = parent + "/" + str(item.get("name") or "")
                        path = item["path"]
                    retry.setdefault("nodes", {})[path] = item
                    if item.get("isdir"):
                        child = exporter.item_to_work(item)
                        child["source_error_key"] = ek
                        ck = work_key(child)
                        if ck not in completed_pages and ck not in queued_keys:
                            retry["queue"].append(child)
                            queued_keys.add(ck)

                if result.get("has_more"):
                    nxt = dict(work)
                    nxt["page"] = int(work.get("page", 1) or 1) + 1
                    nk = work_key(nxt)
                    if nk not in completed_pages and nk not in queued_keys:
                        retry["queue"].insert(0, nxt)
                        queued_keys.add(nk)

                print(
                    f"retry recovered: {work.get('path')} page={work.get('page',1)} "
                    f"items={len(items)} has_more={bool(result.get('has_more'))}",
                    flush=True,
                )

            retry["seen_errors"] = sorted(seen_errors)
            retry["recovered_errors"] = sorted(recovered_errors)
            retry["completed_pages"] = sorted(completed_pages)
            retry["updated_at"] = time.time()
            atomic_write(retry_state_path, retry)
            continue

        retry["seen_errors"] = sorted(seen_errors)
        retry["recovered_errors"] = sorted(recovered_errors)
        retry["completed_pages"] = sorted(completed_pages)
        retry["updated_at"] = time.time()
        atomic_write(retry_state_path, retry)

        if not main_alive():
            idle_after_main_exit += 1
            if idle_after_main_exit >= 3:
                break
        else:
            idle_after_main_exit = 0
        time.sleep(max(0.5, args.poll_sec))

    print(json.dumps({
        "seen_errors": len(seen_errors),
        "recovered_errors": len(recovered_errors),
        "unresolved_errors": len(retry.get("unresolved_errors", [])),
        "retry_nodes": len(retry.get("nodes", {})),
        "remaining_retry_queue": len(retry.get("queue", [])),
        "retry_state": str(retry_state_path),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
