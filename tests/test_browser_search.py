import tempfile
from pathlib import Path
from unittest import mock

import pytest
from test_core import make_config
from mcp4chatgpt import browser_search


def test_rag_chinese_citations_and_partial_failure():
    with tempfile.TemporaryDirectory() as directory:
        config = make_config(Path(directory))
        with mock.patch.object(browser_search, "search", return_value={"status": "ok", "results": [
            {"url": "https://example.org/a"}, {"url": "https://example.org/b"}]}), \
             mock.patch.object(browser_search, "read", side_effect=[
                 {"url": "https://example.org/a", "title": "说明", "text": "本机浏览器搜索支持中文检索，并返回引用。"},
                 RuntimeError("page timed out")]):
            result = browser_search.rag(config, "浏览器搜索")
        assert result["chunks"][0]["citation_id"] == "S1"
        assert result["chunks"][0]["url"] == result["sources"][0]["url"]
        assert result["chunks"][0]["score"] > 0
        assert len(result["errors"]) == 1
        assert not config.knowledge_store_dir.exists()


def test_save_sources_and_no_matching_evidence():
    with tempfile.TemporaryDirectory() as directory:
        config = make_config(Path(directory))
        with mock.patch.object(browser_search, "search", return_value={"results": [{"url": "https://example.org"}]}), \
             mock.patch.object(browser_search, "read", return_value={"text": "oranges", "title": "Fruit"}):
            result = browser_search.rag(config, "python", save_sources=True)
        assert result["status"] == "no_matching_context"
        assert result["chunks"] == []
        assert result["sources"][0]["source_id"]
        assert (config.knowledge_store_dir / "sources.json").exists()


def test_saved_chunk_ids_match_live_rag_chunks():
    with tempfile.TemporaryDirectory() as directory:
        config = make_config(Path(directory))
        text = "浏览器搜索支持中文检索。" * 300
        with mock.patch.object(browser_search, "search", return_value={"results": [{"url": "https://example.org/doc"}]}), \
             mock.patch.object(browser_search, "read", return_value={"url": "https://example.org/doc", "text": text, "title": "说明"}):
            result = browser_search.rag(config, "中文检索", save_sources=True, max_chunks=12)
        assert result["chunks"]
        live = result["chunks"][0]
        assert live["source_id"] == result["sources"][0]["source_id"]
        fetched = browser_search.knowledge_ops.fetch(config, live["source_id"], live["chunk_id"], max_chars=5000)
        assert fetched["text"] == live["text"]
        assert browser_search.knowledge_ops.search(config, "中文检索")["results"]


def test_invalid_inputs_do_not_contact_browser():
    with mock.patch.object(browser_search.ext_bridge, "send_command") as command:
        with pytest.raises(ValueError):
            browser_search.search(None, "  ")
        for url in ("file:///etc/passwd", "javascript:alert(1)", "https://user:pass@example.org"):
            with pytest.raises(ValueError):
                browser_search.read(None, url)
        command.assert_not_called()


def test_bridge_protocol_and_redaction():
    with mock.patch.object(browser_search, "_require_connected"), \
         mock.patch.object(browser_search, "validate_research_url", side_effect=lambda url: url) as validate, \
         mock.patch.object(browser_search.ext_bridge, "send_command", return_value={"text": "hello"}) as command:
        assert browser_search.read(None, "https://example.org")["text"] == "hello"
        assert validate.call_count == 2
        command.assert_called_once_with(
            "browser_read",
            {"url": "https://example.org", "maxChars": 30000, "includeHtml": False, "maxHtmlChars": 0},
            timeout=30,
        )
