#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

MCP_URL = "http://127.0.0.1:8766/mcp"
TERMINAL_WAIT_SEC = 20.0


def rpc(tool: str, arguments: dict[str, Any], *, timeout: float = 60.0) -> dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    request = urllib.request.Request(
        MCP_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.load(response)
    if "error" in body:
        raise RuntimeError(f"MCP {tool} failed: {body['error']}")
    result = body.get("result") or {}
    return result.get("structuredContent") or {}


def parse_downstream_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list) or not content:
        raise RuntimeError(f"Unexpected downstream payload: {payload!r}")
    text = content[0].get("text")
    if not isinstance(text, str):
        raise RuntimeError(f"Downstream payload has no text: {payload!r}")
    return text


def extract_script_json(payload: dict[str, Any]) -> Any:
    text = parse_downstream_text(payload)
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    if text.startswith("Script ran on page and returned:"):
        tail = text.split("\n", 1)[1] if "\n" in text else ""
        return json.loads(tail)
    raise RuntimeError(f"Could not parse evaluate_script response: {text[:500]}")


def list_pages() -> list[tuple[int, str, str]]:
    payload = rpc("chrome_devtools__list_pages", {})
    text = parse_downstream_text(payload)
    pages: list[tuple[int, str, str]] = []
    for line in text.splitlines():
        match = re.match(r"(\d+):\s+(.*?)\s+\((https?://.*?)\)(?:\s+\[selected\])?$", line)
        if match:
            pages.append((int(match.group(1)), match.group(2), match.group(3)))
            continue
        match = re.match(r"(\d+):\s+(https?://\S+)(?:\s+\[selected\])?$", line)
        if match:
            pages.append((int(match.group(1)), match.group(2), match.group(2)))
    return pages


def evaluate(page_id: int, function: str, *, timeout: float = 60.0) -> Any:
    payload = rpc(
        "chrome_devtools__evaluate_script",
        {
            "pageId": page_id,
            "function": function,
            "waitForStableDom": False,
        },
        timeout=timeout,
    )
    return extract_script_json(payload)


def find_group_page(gid: str) -> int:
    candidates = [p for p in list_pages() if "pan.baidu.com" in p[2]]
    if not candidates:
        raise RuntimeError("No open pan.baidu.com page found in Chrome")
    probe = """() => {
      const el = document.querySelector('.im-doclib');
      const v = el && el.__vue__;
      return {hasDocLib: !!v, gid: v && String(v.gid || ''), url: location.href};
    }"""
    fallback: int | None = None
    for page_id, _title, _url in candidates:
        try:
            data = evaluate(page_id, probe, timeout=20)
        except Exception:
            continue
        if data.get("hasDocLib"):
            fallback = fallback or page_id
            if str(data.get("gid") or "") == gid:
                return page_id
    if fallback is not None:
        return fallback
    raise RuntimeError(
        "No Baidu Netdisk tab currently exposes the file-library Vue component. "
        "Open the target group's 文件库 in Chrome first."
    )


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class NodeStore:
    """Append-only node persistence so batch checkpoints stay small."""

    def __init__(self, path: Path, nodes: dict[str, dict[str, Any]]) -> None:
        self.path = path
        self.nodes = nodes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")

    def put(self, path: str, node: dict[str, Any]) -> None:
        self.nodes[path] = node
        self._file.write(json.dumps({"path": path, "node": node}, ensure_ascii=False, separators=(",", ":")) + "\n")

    def flush(self) -> None:
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self) -> None:
        self._file.close()


def open_node_store(state_path: Path, state: dict[str, Any]) -> NodeStore:
    node_path = state_path.with_suffix(".nodes.jsonl")
    nodes: dict[str, dict[str, Any]] = {}
    if node_path.exists():
        with node_path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    if isinstance(record.get("path"), str) and isinstance(record.get("node"), dict):
                        nodes[record["path"]] = record["node"]
                except (json.JSONDecodeError, TypeError):
                    continue
    legacy_nodes = state.pop("nodes", None)
    if isinstance(legacy_nodes, dict):
        with node_path.open("a", encoding="utf-8") as stream:
            for path, node in legacy_nodes.items():
                if isinstance(path, str) and isinstance(node, dict):
                    nodes[path] = node
                    stream.write(json.dumps({"path": path, "node": node}, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        atomic_write_json(state_path, state)
    state["node_store"] = str(node_path)
    return NodeStore(node_path, nodes)


def work_key(work: dict[str, Any]) -> str:
    return "|".join(
        [
            str(work.get("path") or ""),
            str(work.get("fs_id") or ""),
            str(work.get("msg_id") or ""),
            str(work.get("page") or 1),
        ]
    )


def item_to_work(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": str(item.get("path") or ""),
        "name": str(item.get("name") or ""),
        "fs_id": item.get("fs_id"),
        "from_uk": item.get("from_uk"),
        "from_ciduk": item.get("from_ciduk"),
        "msg_id": item.get("msg_id"),
        "to_uk": item.get("to_uk"),
        "to_ciduk": item.get("to_ciduk"),
        "fromB": bool(item.get("fromB", False)),
        "toB": bool(item.get("toB", False)),
        "isPolymer": bool(item.get("isPolymer", False)),
        "page": 1,
    }


def fetch_directory_page(page_id: int, work: dict[str, Any]) -> dict[str, Any]:
    work_json = json.dumps(work, ensure_ascii=False, separators=(",", ":"))
    function = f"""async () => {{
      const job = {work_json};
      const el = document.querySelector('.im-doclib');
      const v = el && el.__vue__;
      if (!v) return {{error: 'im-doclib Vue component not found', url: location.href}};

      const params = {{
        from_uk: job.from_ciduk || job.from_uk,
        msg_id: job.msg_id,
        type: 2,
        num: 100,
        page: Number(job.page || 1),
        gid: String(v.gid || ''),
        limit: 100,
        desc: 1
      }};
      if (!job.isPolymer) params.fs_id = job.fs_id;
      Object.keys(params).forEach(k => {{
        if (params[k] === undefined || params[k] === null || params[k] === '') delete params[k];
      }});
      const query = new URLSearchParams();
      Object.entries(params).forEach(([k, value]) => query.set(k, String(value)));
      const response = await v.http.post('/mbox/msg/shareinfo?' + query.toString());
      if (!response || Number(response.errno || 0) !== 0) {{
        return {{
          error: 'shareinfo errno=' + String(response && response.errno),
          show_msg: response && response.show_msg,
          page: params.page,
          path: job.path
        }};
      }}
      const rawItems = Array.isArray(response.records) ? response.records : [];
      const listedItems = typeof v.dealList === 'function' ? v.dealList(rawItems) : rawItems;

      const items = listedItems.map(x => ({{
        name: String(x.server_filename || x.formatName || x.realFileName || ''),
        path: String(x.path || (x.fileMeta && x.fileMeta.path) || ''),
        isdir: !!x.isdir,
        isPolymer: !!x.isPolymer,
        fs_id: x.fs_id != null ? x.fs_id : (x.fileMeta && x.fileMeta.fs_id),
        from_uk: x.from_uk != null ? x.from_uk : job.from_uk,
        from_ciduk: x.from_ciduk,
        msg_id: x.msg_id || job.msg_id,
        to_uk: x.to_uk,
        to_ciduk: x.to_ciduk,
        fromB: !!x.fromB,
        toB: !!x.toB,
        size: Number(x.size || (x.fileMeta && x.fileMeta.size) || 0),
        category: x.category != null ? x.category : (x.fileMeta && x.fileMeta.category),
        formatTime: x.formatTime || '',
        formatSize: x.formatSize || ''
      }}));

      return {{
        gid: String(v.gid || ''),
        page: Number(job.page || 1),
        has_more: !!response.has_more,
        count: items.length,
        items
      }};
    }}"""
    data = evaluate(page_id, function, timeout=45)
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data["error"]))
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise RuntimeError(f"Unexpected directory payload: {data!r}")
    return data


def fetch_directory_pages(
    page_id: int,
    works: list[dict[str, Any]],
    *,
    concurrency: int = 6,
) -> list[dict[str, Any]]:
    """Fetch several directory pages in one DevTools evaluation.

    The Baidu page's own HTTP wrapper is used, so this preserves the same
    authenticated/session semantics as the visible file-library UI while
    avoiding one MCP round trip per directory.
    """
    if not works:
        return []
    jobs_json = json.dumps(works, ensure_ascii=False, separators=(",", ":"))
    worker_count = max(1, min(int(concurrency), len(works), 8))
    function = f"""async () => {{
      const jobs = {jobs_json};
      const el = document.querySelector('.im-doclib');
      const v = el && el.__vue__;
      if (!v) return jobs.map(() => ({{error: 'im-doclib Vue component not found'}}));

      async function fetchOne(job) {{
        try {{
          const params = {{
            from_uk: job.from_ciduk || job.from_uk,
            msg_id: job.msg_id,
            type: 2,
            num: 100,
            page: Number(job.page || 1),
            gid: String(v.gid || ''),
            limit: 100,
            desc: 1
          }};
          if (!job.isPolymer) params.fs_id = job.fs_id;
          Object.keys(params).forEach(k => {{
            if (params[k] === undefined || params[k] === null || params[k] === '') delete params[k];
          }});
          const query = new URLSearchParams();
          Object.entries(params).forEach(([k, value]) => query.set(k, String(value)));
          const response = await v.http.post('/mbox/msg/shareinfo?' + query.toString());
          if (!response || Number(response.errno || 0) !== 0) {{
            return {{
              error: 'shareinfo errno=' + String(response && response.errno),
              show_msg: response && response.show_msg,
              page: params.page,
              path: job.path
            }};
          }}
          const rawItems = Array.isArray(response.records) ? response.records : [];
          const listedItems = typeof v.dealList === 'function' ? v.dealList(rawItems) : rawItems;
          const items = listedItems.map(x => ({{
            name: String(x.server_filename || x.formatName || x.realFileName || ''),
            path: String(x.path || (x.fileMeta && x.fileMeta.path) || ''),
            isdir: !!x.isdir,
            isPolymer: !!x.isPolymer,
            fs_id: x.fs_id != null ? x.fs_id : (x.fileMeta && x.fileMeta.fs_id),
            from_uk: x.from_uk != null ? x.from_uk : job.from_uk,
            from_ciduk: x.from_ciduk,
            msg_id: x.msg_id || job.msg_id,
            to_uk: x.to_uk,
            to_ciduk: x.to_ciduk,
            fromB: !!x.fromB,
            toB: !!x.toB,
            size: Number(x.size || (x.fileMeta && x.fileMeta.size) || 0),
            category: x.category != null ? x.category : (x.fileMeta && x.fileMeta.category),
            formatTime: x.formatTime || '',
            formatSize: x.formatSize || ''
          }}));
          return {{
            gid: String(v.gid || ''),
            page: params.page,
            has_more: !!response.has_more,
            count: items.length,
            items
          }};
        }} catch (error) {{
          return {{error: String(error && error.message || error), path: job.path, page: job.page || 1}};
        }}
      }}

      const results = new Array(jobs.length);
      let cursor = 0;
      async function worker() {{
        while (true) {{
          const index = cursor++;
          if (index >= jobs.length) return;
          results[index] = await fetchOne(jobs[index]);
        }}
      }}
      await Promise.all(Array.from({{length: {worker_count}}}, () => worker()));
      return results;
    }}"""
    data = evaluate(page_id, function, timeout=60)
    if not isinstance(data, list) or len(data) != len(works):
        raise RuntimeError(f"Unexpected batch payload: {data!r}")
    return data


def render_tree(root_name: str, nodes: dict[str, dict[str, Any]], errors: list[dict[str, Any]]) -> str:
    root_path = "/" + root_name.strip("/")
    children: dict[str, list[dict[str, Any]]] = {}
    for node in nodes.values():
        path = str(node.get("path") or "")
        if not path or path == root_path:
            continue
        parent = path.rsplit("/", 1)[0] or "/"
        children.setdefault(parent, []).append(node)

    def sort_key(node: dict[str, Any]) -> tuple[int, str]:
        return (0 if node.get("isdir") else 1, str(node.get("name") or "").casefold())

    lines = [root_name + "/"]

    def walk(parent_path: str, prefix: str) -> None:
        entries = sorted(children.get(parent_path, []), key=sort_key)
        for idx, node in enumerate(entries):
            last = idx == len(entries) - 1
            branch = "└── " if last else "├── "
            suffix = "/" if node.get("isdir") else ""
            name = str(node.get("name") or node.get("path") or "")
            lines.append(prefix + branch + name + suffix)
            if node.get("isdir"):
                walk(str(node.get("path") or ""), prefix + ("    " if last else "│   "))

    walk(root_path, "")
    if errors:
        lines.extend(["", "# Crawl errors / incomplete branches"])
        for err in errors:
            lines.append(
                f"# {err.get('path', '?')} page={err.get('page', '?')}: {err.get('error', 'unknown error')}"
            )
    return "\n".join(lines) + "\n"


def load_or_initialize_state(args: argparse.Namespace, state_path: Path) -> dict[str, Any]:
    if args.reset and state_path.exists():
        state_path.unlink()
    if state_path.exists():
        return json.loads(state_path.read_text(encoding="utf-8"))

    root_path = "/" + args.root_name.strip("/")
    root_work = {
        "path": root_path,
        "name": args.root_name,
        "fs_id": args.root_fs_id,
        "from_uk": args.from_uk,
        "from_ciduk": None,
        "msg_id": args.msg_id,
        "to_uk": 0,
        "to_ciduk": None,
        "fromB": False,
        "toB": False,
        "isPolymer": False,
        "page": 1,
    }
    root_node = {
        "name": args.root_name,
        "path": root_path,
        "isdir": True,
        "isPolymer": False,
        "fs_id": args.root_fs_id,
        "from_uk": args.from_uk,
        "msg_id": args.msg_id,
        "size": 0,
    }
    return {
        "version": 1,
        "gid": args.gid,
        "group_name": args.group_name,
        "root_name": args.root_name,
        "root_fs_id": args.root_fs_id,
        "from_uk": args.from_uk,
        "msg_id": args.msg_id,
        "queue": [root_work],
        "completed_pages": [],
        "nodes": {root_path: root_node},
        "errors": [],
        "pages_processed": 0,
        "started_at": time.time(),
        "updated_at": time.time(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Checkpointed Baidu group file-library tree exporter")
    parser.add_argument("--gid", required=True)
    parser.add_argument("--group-name", required=True)
    parser.add_argument("--root-name", required=True)
    parser.add_argument("--root-fs-id", required=True, type=int)
    parser.add_argument("--from-uk", required=True, type=int)
    parser.add_argument("--msg-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-pages", type=int, default=150)
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    state_path = project_root / "data" / f"baidu-group-tree-{args.gid}.json"
    state = load_or_initialize_state(args, state_path)
    node_store = open_node_store(state_path, state)
    page_id = find_group_page(args.gid)
    print(f"Using Chrome DevTools pageId={page_id}")

    completed = set(str(x) for x in state.get("completed_pages", []))
    processed_this_run = 0
    consecutive_failures = 0

    while state["queue"] and processed_this_run < max(1, args.max_pages):
        budget = max(1, args.max_pages) - processed_this_run
        batch_limit = max(1, min(int(args.batch_size), budget, 24))
        works: list[dict[str, Any]] = []
        keys: list[str] = []
        while state["queue"] and len(works) < batch_limit:
            work = state["queue"].pop(0)
            key = work_key(work)
            if key in completed:
                continue
            works.append(work)
            keys.append(key)

        if not works:
            continue

        print(
            f"Batch {state.get('pages_processed', 0) + 1}-"
            f"{state.get('pages_processed', 0) + len(works)} "
            f"({len(works)} pages, queue={len(state['queue'])})"
        )

        results: list[dict[str, Any]] | None = None
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                results = fetch_directory_pages(
                    page_id,
                    works,
                    concurrency=max(1, int(args.concurrency)),
                )
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                print(f"  batch attempt {attempt}/3 failed: {exc}", file=sys.stderr)
                time.sleep(min(attempt, 2))

        if results is None:
            # Isolate a failed batch so one pathological directory does not
            # prevent the rest of the checkpoint from advancing.
            results = []
            for work in works:
                try:
                    results.append(fetch_directory_page(page_id, work))
                except Exception as exc:
                    results.append({"error": str(exc)})

        queued_keys = {work_key(q) for q in state["queue"]}
        for work, key, data in zip(works, keys, results):
            if not isinstance(data, dict) or data.get("error"):
                consecutive_failures += 1
                state["errors"].append(
                    {
                        "path": work.get("path"),
                        "page": work.get("page", 1),
                        "error": str((data or {}).get("error") or last_exc or "unknown error"),
                        "time": time.time(),
                    }
                )
                completed.add(key)
                if consecutive_failures >= 5:
                    node_store.flush()
                    state["completed_pages"] = sorted(completed)
                    state["updated_at"] = time.time()
                    atomic_write_json(state_path, state)
                    raise RuntimeError(
                        "Five consecutive directory-page failures; stopping to avoid bad crawl state"
                    )
                continue

            consecutive_failures = 0
            items = data.get("items") or []
            for item in items:
                path = str(item.get("path") or "")
                if not path:
                    parent = str(work.get("path") or "").rstrip("/")
                    item["path"] = parent + "/" + str(item.get("name") or "")
                    path = item["path"]
                node_store.put(path, item)
                if item.get("isdir"):
                    child_work = item_to_work(item)
                    child_key = work_key(child_work)
                    if child_key not in completed and child_key not in queued_keys:
                        state["queue"].append(child_work)
                        queued_keys.add(child_key)

            completed.add(key)
            if data.get("has_more"):
                next_work = dict(work)
                next_work["page"] = int(work.get("page", 1)) + 1
                next_key = work_key(next_work)
                if next_key not in completed and next_key not in queued_keys:
                    state["queue"].insert(0, next_work)
                    queued_keys.add(next_key)

            processed_this_run += 1
            state["pages_processed"] = int(state.get("pages_processed", 0)) + 1

        state["completed_pages"] = sorted(completed)
        state["updated_at"] = time.time()
        node_store.flush()
        atomic_write_json(state_path, state)

    output_path = Path(os.path.expanduser(args.output)).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    node_store.flush()
    tree = render_tree(state["root_name"], node_store.nodes, state.get("errors", []))
    header = [
        f"# Baidu Netdisk group: {state['group_name']}",
        f"# gid: {state['gid']}",
        f"# generated_at: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"# nodes: {len(node_store.nodes)}",
        f"# directory_pages_processed: {state.get('pages_processed', 0)}",
        f"# remaining_queue: {len(state['queue'])}",
        f"# crawl_errors: {len(state.get('errors', []))}",
        "",
    ]
    output_path.write_text("\n".join(header) + tree, encoding="utf-8")

    print(
        json.dumps(
            {
                "complete": not bool(state["queue"]),
                "output": str(output_path),
                "state": str(state_path),
                "nodes": len(node_store.nodes),
                "pages_processed": state.get("pages_processed", 0),
                "remaining_queue": len(state["queue"]),
                "errors": len(state.get("errors", [])),
                "processed_this_run": processed_this_run,
            },
            ensure_ascii=False,
        )
    )
    node_store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
