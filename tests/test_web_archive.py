from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from test_core import make_config
from mcp4chatgpt import web_archive


def _page(text: str, *, url: str = "https://example.org/policy", html: str | None = None,
          source_hash: str | None = None, extractor_version: str = "browser_read_v3",
          attachments: list[dict] | None = None):
    page = {
        "url": url,
        "canonical_url": url,
        "title": "陕西政策",
        "published_at": "2026-09-17",
        "extraction_method": "heuristic:.article-content",
        "extractor_version": extractor_version,
        "text": text,
    }
    if html is not None:
        page["html"] = html
    if source_hash is not None:
        page["source_hash"] = source_hash
    if attachments is not None:
        page["attachments"] = attachments
    return page


def test_archive_deduplicates_same_content_and_versions_changes():
    with tempfile.TemporaryDirectory() as directory:
        config = make_config(Path(directory))
        first = web_archive.archive_page(config, _page("陕西省传统医学师承出师考核通知。"))
        again = web_archive.archive_page(config, _page("陕西省传统医学师承出师考核通知。"))
        changed = web_archive.archive_page(config, _page("陕西省传统医学师承出师考核通知。新增备案要求。"))

        assert first["new_version"] is True
        assert again["new_version"] is False
        assert again["version_id"] == first["version_id"]
        assert changed["new_version"] is True
        assert changed["document_id"] == first["document_id"]
        assert changed["version_id"] != first["version_id"]
        history = web_archive.versions(config, first["document_id"])
        assert history["current_version_id"] == changed["version_id"]
        assert len(history["versions"]) == 2


def test_archive_chinese_search_and_stable_chunk_fetch():
    with tempfile.TemporaryDirectory() as directory:
        config = make_config(Path(directory))
        saved = web_archive.archive_page(
            config,
            _page("陕西省传统医学师承出师考核通知已经发布。报名人员须按规定备案。" * 120),
        )
        result = web_archive.search(config, "中医师承 备案", limit=5)
        assert result["results"]
        hit = result["results"][0]
        assert hit["document_id"] == saved["document_id"]
        assert hit["chunk_id"].startswith(saved["version_id"] + ":chunk-")
        fetched = web_archive.fetch(config, chunk_id=hit["chunk_id"], max_chars=5000)
        assert "师承出师考核通知" in fetched["text"]
        assert "师承出师考核通知" in hit["quote"]
        current = web_archive.fetch(config, document_id=saved["document_id"], max_chars=20000)
        assert current["version_id"] == saved["version_id"]
        assert "中医" not in current["text"] or "师承" in current["text"]


def test_archive_optional_html_and_worker_metadata():
    with tempfile.TemporaryDirectory() as directory:
        config = make_config(Path(directory))
        saved = web_archive.archive_page(
            config,
            _page("政策正文", html="<html><body>政策正文</body></html>"),
            worker_id="cn-mac-mini",
            worker_region="CN-unverified-egress",
        )
        fetched = web_archive.fetch(config, version_id=saved["version_id"])
        assert fetched["html_saved"] is True
        assert fetched["worker_id"] == "cn-mac-mini"
        assert fetched["worker_region"] == "CN-unverified-egress"
        root = config.knowledge_store_dir / "web_archive" / "documents" / saved["document_id"]
        version_dir = root / saved["version_id"]
        snapshot_dir = root / "snapshots" / saved["snapshot_id"]
        assert (version_dir / "article.txt").exists()
        assert (snapshot_dir / "page.html").exists()
        assert (version_dir / "metadata.json").exists()


def test_source_snapshot_and_extraction_are_versioned_independently():
    with tempfile.TemporaryDirectory() as directory:
        config = make_config(Path(directory))
        first = web_archive.archive_page(
            config, _page("同一正文", source_hash="a" * 64, extractor_version="v1"),
            download_attachments=False,
        )
        extractor_upgrade = web_archive.archive_page(
            config, _page("同一正文", source_hash="a" * 64, extractor_version="v2"),
            download_attachments=False,
        )
        source_changed = web_archive.archive_page(
            config, _page("同一正文", source_hash="b" * 64, extractor_version="v2"),
            download_attachments=False,
        )

        assert first["new_snapshot"] is True and first["new_extraction"] is True
        assert extractor_upgrade["new_snapshot"] is False
        assert extractor_upgrade["new_extraction"] is False
        assert extractor_upgrade["version_id"] == first["version_id"]
        assert source_changed["new_snapshot"] is True
        assert source_changed["new_extraction"] is False
        assert source_changed["version_id"] == first["version_id"]
        assert source_changed["snapshot_id"] != first["snapshot_id"]
        assert source_changed["snapshot_count"] == 2
        assert source_changed["version_count"] == 1


def test_attachment_discovery_is_linked_to_snapshot(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        config = make_config(Path(directory))

        def fake_download(url, destination, *, referer, max_bytes):
            web_archive._atomic_write_bytes(destination, b"attachment-bytes")
            return {
                "status": "saved",
                "content_type": "application/pdf",
                "size_bytes": 16,
                "content_hash": "f" * 64,
                "local_path": str(destination),
            }

        monkeypatch.setattr(web_archive, "_download_attachment", fake_download)
        saved = web_archive.archive_page(
            config,
            _page(
                "政策正文",
                source_hash="c" * 64,
                attachments=[{"url": "https://example.org/files/policy.pdf", "name": "附件1", "link_text": "附件1 下载"}],
            ),
        )
        assert len(saved["attachments"]) == 1
        attachment = saved["attachments"][0]
        assert attachment["status"] == "saved"
        assert attachment["name"].endswith(".pdf")
        assert Path(attachment["local_path"]).exists()


def test_archive_concurrent_distinct_documents():
    with tempfile.TemporaryDirectory() as directory:
        config = make_config(Path(directory))

        def save(index: int):
            return web_archive.archive_page(
                config,
                _page(f"并发归档正文 {index} 师承政策", url=f"https://example.org/policy/{index}"),
                download_attachments=False,
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            saved = list(pool.map(save, range(12)))
        assert len({item["document_id"] for item in saved}) == 12
        result = web_archive.search(config, "师承政策", limit=20)
        assert len(result["results"]) >= 12
