from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from .config import Config
from .safety import resolve_allowed_path, truncate_text

SUPPORTED_EXTENSIONS = {".md", ".txt", ".json", ".csv"}
WORD_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _store_path(config: Config) -> Path:
    config.knowledge_store_dir.mkdir(parents=True, exist_ok=True)
    return config.knowledge_store_dir / "sources.json"


def _load_store(config: Config) -> dict[str, Any]:
    path = _store_path(config)
    if not path.exists():
        return {"sources": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_store(config: Config, store: dict[str, Any]) -> None:
    path = _store_path(config)
    path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_source_file(path: Path) -> str:
    if path.suffix.lower() == ".csv":
        rows = []
        with path.open(newline="", encoding="utf-8", errors="replace") as fh:
            for row in csv.reader(fh):
                rows.append("\t".join(row))
        return "\n".join(rows)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return json.dumps(data, ensure_ascii=False, indent=2)
    return path.read_text(encoding="utf-8", errors="replace")


def _chunk_text(text: str, chunk_chars: int = 1800, overlap: int = 200) -> list[dict[str, Any]]:
    # Chunks are intentionally overlapping so a citation can retain nearby
    # context when a relevant passage crosses a fixed-size boundary.
    chunk_chars = max(1, chunk_chars)
    overlap = max(0, min(overlap, chunk_chars // 2))
    chunks = []
    start = 0
    idx = 0
    while start < len(text):
        end = min(len(text), start + chunk_chars)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append({"chunk_id": f"chunk-{idx}", "start": start, "end": end, "text": chunk})
            idx += 1
        if end == len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in WORD_RE.finditer(text)}


def add_source(
    config: Config,
    *,
    path: str | None = None,
    title: str | None = None,
    text: str | None = None,
    url: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if path:
        source_path = resolve_allowed_path(path, config.knowledge_roots or config.allowed_roots, must_exist=True)
        if source_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"Unsupported source extension: {source_path.suffix}")
        content = _read_source_file(source_path)
        source_title = title or source_path.name
        source_ref = str(source_path)
    elif text is not None:
        content = text
        source_title = title or url or "Untitled source"
        source_ref = url or source_title
    else:
        raise ValueError("Either path or text is required.")

    digest = hashlib.sha256((source_ref + "\n" + content).encode("utf-8")).hexdigest()
    source_id = digest[:16]
    chunks = _chunk_text(content)
    record = {
        "source_id": source_id,
        "title": source_title,
        "path": path,
        "url": url,
        "metadata": metadata or {},
        "content_hash": digest,
        "created_at": time.time(),
        "text": content,
        "chunks": chunks,
    }
    store = _load_store(config)
    store["sources"][source_id] = record
    _save_store(config, store)
    return {"source_id": source_id, "title": source_title, "chunks": len(chunks), "content_hash": digest}


def list_sources(config: Config) -> dict[str, Any]:
    store = _load_store(config)
    sources = []
    for source in store["sources"].values():
        sources.append(
            {
                "source_id": source["source_id"],
                "title": source["title"],
                "path": source.get("path"),
                "url": source.get("url"),
                "chunks": len(source.get("chunks", [])),
                "created_at": source.get("created_at"),
            }
        )
    return {"sources": sorted(sources, key=lambda item: item["created_at"] or 0.0, reverse=True)}


def search(config: Config, query: str, limit: int = 8) -> dict[str, Any]:
    query_tokens = _tokens(query)
    if not query_tokens:
        raise ValueError("Query cannot be empty.")
    store = _load_store(config)
    hits = []
    for source in store["sources"].values():
        for chunk in source.get("chunks", []):
            # v1 retrieval is lexical. The output shape is designed to survive
            # a later embedding/vector backend without changing tool contracts.
            chunk_tokens = _tokens(chunk["text"])
            score = len(query_tokens & chunk_tokens)
            if score <= 0:
                continue
            quote, _ = truncate_text(chunk["text"], 500)
            hits.append(
                {
                    "score": score,
                    "source_id": source["source_id"],
                    "title": source["title"],
                    "url": source.get("url"),
                    "path": source.get("path"),
                    "chunk_id": chunk["chunk_id"],
                    "quote": quote,
                }
            )
    hits.sort(key=lambda item: item["score"], reverse=True)
    return {"query": query, "results": hits[: max(1, min(limit, 50))]}


def fetch(config: Config, source_id: str, chunk_id: str | None = None, max_chars: int = 12000) -> dict[str, Any]:
    store = _load_store(config)
    source = store["sources"].get(source_id)
    if not source:
        raise ValueError(f"Unknown source_id: {source_id}")
    if chunk_id:
        for chunk in source.get("chunks", []):
            if chunk["chunk_id"] == chunk_id:
                text, truncated = truncate_text(chunk["text"], max_chars)
                return {"source_id": source_id, "chunk_id": chunk_id, "title": source["title"], "text": text, "truncated": truncated}
        raise ValueError(f"Unknown chunk_id for source {source_id}: {chunk_id}")
    text, truncated = truncate_text(source["text"], max_chars)
    return {"source_id": source_id, "title": source["title"], "url": source.get("url"), "path": source.get("path"), "text": text, "truncated": truncated}


def summarize(config: Config, source_id: str, max_points: int = 8) -> dict[str, Any]:
    source = fetch(config, source_id, max_chars=20000)
    lines = [line.strip() for line in source["text"].splitlines() if line.strip()]
    points = lines[: max(1, min(max_points, 20))]
    markdown = "# Summary\n\n" + "\n".join(f"- {point}" for point in points)
    return {"source_id": source_id, "markdown": markdown}


def study_guide(config: Config, source_id: str) -> dict[str, Any]:
    source = fetch(config, source_id, max_chars=20000)
    words = list(_tokens(source["text"]))[:20]
    markdown = "# Study Guide\n\n## Key Terms\n\n" + "\n".join(f"- {word}" for word in words)
    markdown += "\n\n## Suggested Questions\n\n- What are the main claims in this source?\n- Which evidence supports those claims?\n- What should be verified against another source?"
    return {"source_id": source_id, "markdown": markdown}


def quiz(config: Config, source_id: str, count: int = 5) -> dict[str, Any]:
    source = fetch(config, source_id, max_chars=12000)
    lines = [line.strip() for line in source["text"].splitlines() if len(line.strip()) > 40]
    items = []
    for idx, line in enumerate(lines[: max(1, min(count, 20))], start=1):
        items.append({"question": f"What does this source say about item {idx}?", "answer": line})
    return {"source_id": source_id, "items": items}


def flashcards(config: Config, source_id: str, count: int = 10) -> dict[str, Any]:
    source = fetch(config, source_id, max_chars=12000)
    terms = list(_tokens(source["text"]))[: max(1, min(count, 50))]
    cards = [{"front": term, "back": f"Review this term in source {source_id}."} for term in terms]
    return {"source_id": source_id, "cards": cards}
