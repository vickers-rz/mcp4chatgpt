"""Versioned SQLite/FTS5 archive for browser-fetched pages."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from .config import Config
from .knowledge_ops import _chunk_text
from .retrieval import fts5_query, indexed_text, unique_terms
from .safety import truncate_text

_DB_INIT_LOCK = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents(
 document_id TEXT PRIMARY KEY, canonical_url TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
 first_seen REAL NOT NULL, last_seen REAL NOT NULL, current_version_id TEXT,
 current_snapshot_id TEXT);
CREATE TABLE IF NOT EXISTS source_snapshots(
 snapshot_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, source_hash TEXT NOT NULL,
 source_hash_kind TEXT NOT NULL DEFAULT '', fetched_at REAL NOT NULL, final_url TEXT NOT NULL,
 backend TEXT NOT NULL, worker_id TEXT NOT NULL, worker_region TEXT NOT NULL,
 source_html_length INTEGER NOT NULL, html_path TEXT, metadata_json TEXT NOT NULL,
 UNIQUE(document_id, source_hash));
CREATE TABLE IF NOT EXISTS versions(
 version_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, content_hash TEXT NOT NULL,
 fetched_at REAL NOT NULL, final_url TEXT NOT NULL, title TEXT NOT NULL,
 published_at TEXT, extraction_method TEXT, backend TEXT NOT NULL,
 worker_id TEXT NOT NULL, worker_region TEXT NOT NULL, text_path TEXT NOT NULL,
 html_path TEXT, metadata_json TEXT NOT NULL, snapshot_id TEXT,
 extractor_version TEXT, extracted_hash TEXT, UNIQUE(document_id, content_hash));
CREATE TABLE IF NOT EXISTS snapshot_versions(
 snapshot_id TEXT NOT NULL, version_id TEXT NOT NULL, extractor_version TEXT NOT NULL,
 linked_at REAL NOT NULL, PRIMARY KEY(snapshot_id, version_id));
CREATE TABLE IF NOT EXISTS attachments(
 attachment_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, url TEXT NOT NULL,
 name TEXT NOT NULL, link_text TEXT, content_type TEXT, size_bytes INTEGER,
 content_hash TEXT, local_path TEXT, status TEXT NOT NULL, error TEXT,
 discovered_at REAL NOT NULL, UNIQUE(snapshot_id, url));
CREATE TABLE IF NOT EXISTS chunks(
 chunk_id TEXT PRIMARY KEY, version_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
 start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, text TEXT NOT NULL,
 UNIQUE(version_id, chunk_index));
CREATE INDEX IF NOT EXISTS idx_versions_document ON versions(document_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_document ON source_snapshots(document_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_attachments_snapshot ON attachments(snapshot_id);
"""


def _root(config: Config) -> Path:
    path = config.knowledge_store_dir / "web_archive"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            tmp_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _ensure_column(db: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _connect(config: Config) -> tuple[sqlite3.Connection, bool]:
    db = sqlite3.connect(_root(config) / "index.sqlite3", timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=15000")
    with _DB_INIT_LOCK:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(_SCHEMA)
        # Incremental migration for archives created by the first implementation.
        _ensure_column(db, "documents", "current_snapshot_id", "TEXT")
        _ensure_column(db, "source_snapshots", "source_hash_kind", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "versions", "snapshot_id", "TEXT")
        _ensure_column(db, "versions", "extractor_version", "TEXT")
        _ensure_column(db, "versions", "extracted_hash", "TEXT")
        db.execute("UPDATE versions SET extracted_hash=content_hash WHERE extracted_hash IS NULL OR extracted_hash='' ")
        db.execute("UPDATE versions SET extractor_version='legacy-pre-snapshot' WHERE extractor_version IS NULL OR extractor_version='' ")
        try:
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, search_text)")
            fts_available = True
        except sqlite3.OperationalError:
            fts_available = False
    return db, fts_available


def _normalize_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Expected an HTTP(S) URL.")
    host = parsed.hostname.lower().rstrip(".")
    if parsed.port and not ((parsed.scheme == "http" and parsed.port == 80) or (parsed.scheme == "https" and parsed.port == 443)):
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", parsed.query, ""))


def _document_id(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:20]


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snapshot_id(document_id: str, source_hash: str) -> str:
    return hashlib.sha256(f"{document_id}\nsource\n{source_hash}".encode("utf-8")).hexdigest()[:24]


def _version_id(document_id: str, extracted_hash: str) -> str:
    # Keep extraction identity content-addressed so the same extracted article can
    # be linked to multiple source snapshots without duplicating chunks.
    return hashlib.sha256(f"{document_id}\nextract\n{extracted_hash}".encode("utf-8")).hexdigest()[:24]


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            tmp_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _attachment_name(candidate: dict[str, Any], index: int) -> str:
    raw = str(candidate.get("name") or "").strip()
    url = str(candidate.get("url") or "")
    path_name = unquote(Path(urlsplit(url).path).name)
    name = raw or path_name or f"attachment-{index + 1}"
    # Link text can be a human label without an extension. Preserve the URL suffix
    # when available so archived binaries remain easy to open from Finder.
    suffix = Path(path_name).suffix
    if suffix and not Path(name).suffix:
        name += suffix
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", name).strip(" .")
    return (name or f"attachment-{index + 1}")[:180]


def _download_attachment(url: str, destination: Path, *, referer: str, max_bytes: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 MCP4ChatGPT-WebArchive/1.0",
            "Referer": referer,
            "Accept": "*/*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                return {"status": "skipped_too_large", "content_type": content_type,
                        "size_bytes": int(declared), "error": f"Attachment exceeds {max_bytes} bytes"}
            chunks: list[bytes] = []
            total = 0
            digest = hashlib.sha256()
            while True:
                block = response.read(min(1024 * 1024, max_bytes - total + 1))
                if not block:
                    break
                total += len(block)
                if total > max_bytes:
                    return {"status": "skipped_too_large", "content_type": content_type,
                            "size_bytes": total, "error": f"Attachment exceeds {max_bytes} bytes"}
                digest.update(block)
                chunks.append(block)
            _atomic_write_bytes(destination, b"".join(chunks))
            return {"status": "saved", "content_type": content_type,
                    "size_bytes": total, "content_hash": digest.hexdigest(), "local_path": str(destination)}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"status": "failed", "content_type": mimetypes.guess_type(destination.name)[0] or "",
                "size_bytes": None, "error": str(exc)}


def archive_page(config: Config, page: dict[str, Any], *, backend: str = "chrome",
                 worker_id: str = "local-chrome", worker_region: str = "unverified",
                 download_attachments: bool = True, max_attachment_bytes: int = 25 * 1024 * 1024) -> dict[str, Any]:
    text = str(page.get("text") or "").strip()
    if not text:
        raise ValueError("Cannot archive an empty page.")
    final_url = _normalize_url(str(page.get("url") or ""))
    try:
        canonical_url = _normalize_url(str(page.get("canonical_url") or final_url))
    except ValueError:
        canonical_url = final_url
    title = str(page.get("title") or canonical_url).strip() or canonical_url
    document_id = _document_id(canonical_url)
    extracted_hash = _sha256_text(text)
    html = page.get("html") if isinstance(page.get("html"), str) else None
    source_hash = str(page.get("source_hash") or "").strip() or _sha256_text(html or text)
    source_hash_kind = str(page.get("source_hash_kind") or "").strip() or (
        "html_sha256" if html else "fallback_text_sha256"
    )
    snapshot_id = _snapshot_id(document_id, source_hash)
    extractor_version = str(page.get("extractor_version") or "legacy").strip() or "legacy"
    proposed_version_id = _version_id(document_id, extracted_hash)
    now = time.time()

    root = _root(config) / "documents" / document_id
    version_dir = root / proposed_version_id
    snapshot_dir = root / "snapshots" / snapshot_id
    text_path = version_dir / "article.txt"
    snapshot_html_path = snapshot_dir / "page.html" if html else None
    metadata = {k: v for k, v in page.items() if k not in {"text", "html", "attachments"} and v is not None}
    metadata["source_hash_kind"] = source_hash_kind
    attachment_candidates = [item for item in page.get("attachments", []) if isinstance(item, dict)]

    db, fts = _connect(config)
    try:
        with db:
            db.execute(
                "INSERT INTO documents(document_id,canonical_url,title,first_seen,last_seen,current_version_id,current_snapshot_id) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET title=excluded.title,last_seen=excluded.last_seen",
                (document_id, canonical_url, title, now, now, None, None),
            )

            existing_snapshot = db.execute(
                "SELECT snapshot_id,html_path FROM source_snapshots WHERE document_id=? AND source_hash=?",
                (document_id, source_hash),
            ).fetchone()
            new_snapshot = existing_snapshot is None
            if new_snapshot:
                if snapshot_html_path:
                    _atomic_write(snapshot_html_path, html or "")
                db.execute(
                    "INSERT INTO source_snapshots(snapshot_id,document_id,source_hash,source_hash_kind,fetched_at,final_url,backend,worker_id,worker_region,source_html_length,html_path,metadata_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (snapshot_id, document_id, source_hash, source_hash_kind, now, final_url, backend, worker_id, worker_region,
                     int(page.get("source_html_length") or (len(html) if html else 0)),
                     str(snapshot_html_path) if snapshot_html_path else None,
                     json.dumps(metadata, ensure_ascii=False)),
                )
            elif snapshot_html_path and not existing_snapshot["html_path"]:
                _atomic_write(snapshot_html_path, html or "")
                db.execute("UPDATE source_snapshots SET html_path=? WHERE snapshot_id=?",
                           (str(snapshot_html_path), snapshot_id))

            existing_version = db.execute(
                "SELECT version_id FROM versions WHERE document_id=? AND content_hash=?",
                (document_id, extracted_hash),
            ).fetchone()
            new_extraction = existing_version is None
            if existing_version:
                version_id = str(existing_version["version_id"])
                db.execute(
                    "UPDATE versions SET snapshot_id=?, "
                    "extractor_version=CASE WHEN extractor_version IS NULL OR extractor_version='' OR extractor_version='legacy-pre-snapshot' THEN ? ELSE extractor_version END, "
                    "extracted_hash=COALESCE(NULLIF(extracted_hash,''),content_hash) WHERE version_id=?",
                    (snapshot_id, extractor_version, version_id),
                )
                chunks_count = db.execute("SELECT COUNT(*) FROM chunks WHERE version_id=?", (version_id,)).fetchone()[0]
            else:
                version_id = proposed_version_id
                version_dir = root / version_id
                text_path = version_dir / "article.txt"
                _atomic_write(text_path, text)
                _atomic_write(version_dir / "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
                db.execute(
                    "INSERT INTO versions(version_id,document_id,content_hash,fetched_at,final_url,title,published_at,extraction_method,backend,worker_id,worker_region,text_path,html_path,metadata_json,snapshot_id,extractor_version,extracted_hash) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (version_id, document_id, extracted_hash, now, final_url, title,
                     str(page.get("published_at") or ""), str(page.get("extraction_method") or ""),
                     backend, worker_id, worker_region, str(text_path),
                     str(snapshot_html_path) if snapshot_html_path else None,
                     json.dumps(metadata, ensure_ascii=False), snapshot_id, extractor_version, extracted_hash),
                )
                chunks = _chunk_text(text)
                for index, chunk in enumerate(chunks):
                    chunk_id = f"{version_id}:chunk-{index}"
                    db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?)",
                               (chunk_id, version_id, index, chunk["start"], chunk["end"], chunk["text"]))
                    if fts:
                        db.execute("INSERT INTO chunks_fts VALUES(?,?)", (chunk_id, indexed_text(chunk["text"])))
                chunks_count = len(chunks)

            db.execute(
                "INSERT OR IGNORE INTO snapshot_versions(snapshot_id,version_id,extractor_version,linked_at) VALUES(?,?,?,?)",
                (snapshot_id, version_id, extractor_version, now),
            )
            db.execute(
                "UPDATE documents SET title=?,last_seen=?,current_version_id=?,current_snapshot_id=? WHERE document_id=?",
                (title, now, version_id, snapshot_id, document_id),
            )

            for index, candidate in enumerate(attachment_candidates[:50]):
                raw_url = str(candidate.get("url") or "").strip()
                if not raw_url:
                    continue
                try:
                    attachment_url = _normalize_url(raw_url)
                except ValueError:
                    continue
                attachment_id = hashlib.sha256(f"{snapshot_id}\n{attachment_url}".encode("utf-8")).hexdigest()[:24]
                name = _attachment_name(candidate, index)
                db.execute(
                    "INSERT OR IGNORE INTO attachments(attachment_id,snapshot_id,url,name,link_text,status,discovered_at) VALUES(?,?,?,?,?,?,?)",
                    (attachment_id, snapshot_id, attachment_url, name,
                     str(candidate.get("link_text") or "")[:1000], "discovered", now),
                )

            version_count = db.execute("SELECT COUNT(*) FROM versions WHERE document_id=?", (document_id,)).fetchone()[0]
            snapshot_count = db.execute("SELECT COUNT(*) FROM source_snapshots WHERE document_id=?", (document_id,)).fetchone()[0]

        if download_attachments:
            rows = db.execute(
                "SELECT * FROM attachments WHERE snapshot_id=? AND status IN ('discovered','failed') ORDER BY discovered_at,attachment_id",
                (snapshot_id,),
            ).fetchall()
            attachment_dir = snapshot_dir / "attachments"
            for row in rows:
                destination = attachment_dir / f"{row['attachment_id'][:8]}-{row['name']}"
                result = _download_attachment(str(row["url"]), destination, referer=final_url,
                                              max_bytes=max(1_000_000, min(int(max_attachment_bytes), 100 * 1024 * 1024)))
                with db:
                    db.execute(
                        "UPDATE attachments SET content_type=?,size_bytes=?,content_hash=?,local_path=?,status=?,error=? WHERE attachment_id=?",
                        (result.get("content_type"), result.get("size_bytes"), result.get("content_hash"),
                         result.get("local_path"), result.get("status"), result.get("error"), row["attachment_id"]),
                    )

        attachment_rows = db.execute(
            "SELECT attachment_id,url,name,content_type,size_bytes,content_hash,local_path,status,error FROM attachments WHERE snapshot_id=? ORDER BY discovered_at,attachment_id",
            (snapshot_id,),
        ).fetchall()
        attachments = [dict(row) for row in attachment_rows]
        return {
            "document_id": document_id,
            "snapshot_id": snapshot_id,
            "version_id": version_id,
            "canonical_url": canonical_url,
            "source_hash": source_hash,
            "content_hash": extracted_hash,
            "extractor_version": extractor_version,
            "new_snapshot": new_snapshot,
            "new_extraction": new_extraction,
            "new_version": new_extraction,
            "snapshot_count": snapshot_count,
            "version_count": version_count,
            "chunks": chunks_count,
            "saved_html": bool(snapshot_html_path),
            "attachments": attachments,
        }
    finally:
        db.close()


def search(config: Config, query: str, limit: int = 8) -> dict[str, Any]:
    terms = unique_terms(query)
    if not terms:
        raise ValueError("Query cannot be empty.")
    limit = max(1, min(int(limit), 50))
    db, fts = _connect(config)
    try:
        if fts:
            rows = db.execute(
                "SELECT c.*,v.document_id,v.title,v.final_url,v.fetched_at,d.canonical_url,bm25(chunks_fts) rank FROM chunks_fts JOIN chunks c ON c.chunk_id=chunks_fts.chunk_id JOIN versions v ON v.version_id=c.version_id JOIN documents d ON d.document_id=v.document_id WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts5_query(query), limit),
            ).fetchall()
            backend = "sqlite_fts5"
        else:
            rows = db.execute("SELECT c.*,v.document_id,v.title,v.final_url,v.fetched_at,d.canonical_url FROM chunks c JOIN versions v ON v.version_id=c.version_id JOIN documents d ON d.document_id=v.document_id ORDER BY v.fetched_at DESC").fetchall()
            term_set = set(terms)
            rows = [r for r in rows if term_set & set(unique_terms(str(r["text"]), limit=512))][:limit]
            backend = "sqlite_lexical_fallback"
        results = []
        for row in rows:
            quote, truncated = truncate_text(str(row["text"]), 700)
            results.append({"document_id": row["document_id"], "version_id": row["version_id"],
                            "chunk_id": row["chunk_id"], "title": row["title"], "url": row["final_url"],
                            "canonical_url": row["canonical_url"], "fetched_at": row["fetched_at"],
                            "quote": quote, "quote_truncated": truncated})
        return {"query": query, "backend": backend, "results": results}
    finally:
        db.close()


def fetch(config: Config, *, document_id: str | None = None, version_id: str | None = None,
          chunk_id: str | None = None, max_chars: int = 12000) -> dict[str, Any]:
    db, _ = _connect(config)
    try:
        if chunk_id:
            row = db.execute("SELECT c.*,v.document_id,v.title,v.final_url,d.canonical_url FROM chunks c JOIN versions v ON v.version_id=c.version_id JOIN documents d ON d.document_id=v.document_id WHERE c.chunk_id=?", (chunk_id,)).fetchone()
            if not row:
                raise ValueError(f"Unknown chunk_id: {chunk_id}")
            text, truncated = truncate_text(str(row["text"]), max_chars)
            return {**dict(row), "text": text, "truncated": truncated}
        if not version_id and document_id:
            row = db.execute("SELECT current_version_id FROM documents WHERE document_id=?", (document_id,)).fetchone()
            if not row:
                raise ValueError(f"Unknown document_id: {document_id}")
            version_id = row["current_version_id"]
        if not version_id:
            raise ValueError("Provide chunk_id, version_id, or document_id.")
        row = db.execute("SELECT v.*,d.canonical_url FROM versions v JOIN documents d ON d.document_id=v.document_id WHERE v.version_id=?", (version_id,)).fetchone()
        if not row:
            raise ValueError(f"Unknown version_id: {version_id}")
        text = Path(row["text_path"]).read_text(encoding="utf-8", errors="replace")
        bounded, truncated = truncate_text(text, max_chars)
        return {"document_id": row["document_id"], "version_id": row["version_id"], "title": row["title"],
                "url": row["final_url"], "canonical_url": row["canonical_url"], "fetched_at": row["fetched_at"],
                "published_at": row["published_at"], "extraction_method": row["extraction_method"],
                "extractor_version": row["extractor_version"], "snapshot_id": row["snapshot_id"],
                "extracted_hash": row["extracted_hash"] or row["content_hash"],
                "worker_id": row["worker_id"], "worker_region": row["worker_region"],
                "text": bounded, "truncated": truncated, "html_saved": bool(row["html_path"])}
    finally:
        db.close()


def versions(config: Config, document_id: str, limit: int = 20) -> dict[str, Any]:
    db, _ = _connect(config)
    try:
        doc = db.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
        if not doc:
            raise ValueError(f"Unknown document_id: {document_id}")
        rows = db.execute("SELECT version_id,content_hash,extracted_hash,snapshot_id,extractor_version,fetched_at,final_url,title,published_at,extraction_method,worker_id,worker_region FROM versions WHERE document_id=? ORDER BY fetched_at DESC LIMIT ?", (document_id, max(1, min(int(limit), 100)))).fetchall()
        return {"document_id": document_id, "canonical_url": doc["canonical_url"],
                "current_version_id": doc["current_version_id"], "current_snapshot_id": doc["current_snapshot_id"],
                "versions": [dict(r) for r in rows]}
    finally:
        db.close()


def snapshots(config: Config, document_id: str, limit: int = 20) -> dict[str, Any]:
    db, _ = _connect(config)
    try:
        doc = db.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
        if not doc:
            raise ValueError(f"Unknown document_id: {document_id}")
        rows = db.execute(
            "SELECT snapshot_id,source_hash,source_hash_kind,fetched_at,final_url,backend,worker_id,worker_region,source_html_length,html_path "
            "FROM source_snapshots WHERE document_id=? ORDER BY fetched_at DESC LIMIT ?",
            (document_id, max(1, min(int(limit), 100))),
        ).fetchall()
        result = []
        for row in rows:
            linked = db.execute(
                "SELECT version_id,extractor_version,linked_at FROM snapshot_versions WHERE snapshot_id=? ORDER BY linked_at DESC",
                (row["snapshot_id"],),
            ).fetchall()
            item = dict(row)
            item["html_saved"] = bool(item.pop("html_path"))
            item["extractions"] = [dict(link) for link in linked]
            result.append(item)
        return {
            "document_id": document_id,
            "canonical_url": doc["canonical_url"],
            "current_snapshot_id": doc["current_snapshot_id"],
            "snapshots": result,
        }
    finally:
        db.close()


def list_attachments(config: Config, *, snapshot_id: str | None = None,
                     document_id: str | None = None) -> dict[str, Any]:
    db, _ = _connect(config)
    try:
        if not snapshot_id and document_id:
            row = db.execute("SELECT current_snapshot_id FROM documents WHERE document_id=?", (document_id,)).fetchone()
            if not row:
                raise ValueError(f"Unknown document_id: {document_id}")
            snapshot_id = row["current_snapshot_id"]
        if not snapshot_id:
            raise ValueError("Provide snapshot_id or document_id.")
        snapshot = db.execute(
            "SELECT snapshot_id,document_id,source_hash,fetched_at,final_url FROM source_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if not snapshot:
            raise ValueError(f"Unknown snapshot_id: {snapshot_id}")
        rows = db.execute(
            "SELECT attachment_id,url,name,link_text,content_type,size_bytes,content_hash,local_path,status,error,discovered_at "
            "FROM attachments WHERE snapshot_id=? ORDER BY discovered_at,attachment_id",
            (snapshot_id,),
        ).fetchall()
        return {**dict(snapshot), "attachments": [dict(row) for row in rows]}
    finally:
        db.close()
