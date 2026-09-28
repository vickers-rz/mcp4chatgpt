from __future__ import annotations

import json
from dataclasses import replace
from unittest import mock

import pytest

from mcp4chatgpt.audit import AuditLogger
from mcp4chatgpt.tools import ToolRegistry, _read_webpage, _search_web
from test_core import make_config


def test_read_webpage_uses_local_browser_without_cloud_fallback(tmp_path):
    config = make_config(tmp_path)
    with mock.patch(
        "mcp4chatgpt.tools.browser_search.read",
        return_value={
            "url": "https://example.test/final",
            "title": "Example",
            "text": "rendered",
            "truncated": False,
        },
    ) as browser:
        result = _read_webpage(
            config,
            {"url": "https://example.test/start", "max_chars": 12000},
        )

    browser.assert_called_once_with(
        config,
        "https://example.test/start",
        max_chars=12000,
    )
    assert result["requested_url"] == "https://example.test/start"
    assert result["url"] == "https://example.test/final"
    assert result["backend"] == "browser"
    assert result["fallback_used"] is False


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("extension disconnected"),
        TimeoutError("browser read timed out"),
        ValueError("browser read failed"),
    ],
)
def test_read_webpage_failure_never_falls_back_to_cloud(tmp_path, error):
    config = make_config(tmp_path)
    with (
        mock.patch("mcp4chatgpt.tools.browser_search.read", side_effect=error) as browser,
        mock.patch("mcp4chatgpt.tools.web_ops.scrape") as scrape,
        mock.patch("mcp4chatgpt.tools.web_ops.combined_search") as combined,
    ):
        with pytest.raises(type(error), match=str(error)):
            _read_webpage(
                config,
                {"url": "https://example.test/start", "max_chars": 12000},
            )

    browser.assert_called_once()
    scrape.assert_not_called()
    combined.assert_not_called()


def test_search_web_browser_failure_never_falls_back_without_auto(tmp_path):
    config = make_config(tmp_path)
    with (
        mock.patch(
            "mcp4chatgpt.tools.browser_search.search",
            side_effect=TimeoutError("browser search timed out"),
        ) as browser,
        mock.patch("mcp4chatgpt.tools.web_ops.combined_search") as combined,
    ):
        with pytest.raises(TimeoutError, match="browser search timed out"):
            _search_web(
                config,
                {"query": "test", "result_count": 3, "backend": "browser"},
            )

    browser.assert_called_once_with(config, "test", 3)
    combined.assert_not_called()


def test_compact_public_web_entries_match_extension_side_effect_annotations(tmp_path):
    config = replace(make_config(tmp_path), tool_exposure="compact")
    registry = ToolRegistry(config, AuditLogger(config.audit_log))
    public = {
        item["name"]: item["annotations"]
        for item in registry.list_tools(auth_required=False)["tools"]
    }
    ext_search = registry.tools["ext_search_web"].definition(auth_required=False)["annotations"]
    ext_read = registry.tools["ext_read_webpage"].definition(auth_required=False)["annotations"]

    assert public["search_web"] == ext_search
    assert public["read_webpage"] == ext_read
    for name in ("search_web", "read_webpage"):
        assert public[name]["readOnlyHint"] is False
        assert public[name]["destructiveHint"] is False
        assert public[name]["openWorldHint"] is True
        assert public[name]["idempotentHint"] is False


def test_public_web_audit_records_web_channel_and_actual_backend(tmp_path):
    config = replace(make_config(tmp_path), tool_exposure="compact")
    registry = ToolRegistry(config, AuditLogger(config.audit_log))

    with mock.patch(
        "mcp4chatgpt.tools.browser_search.read",
        return_value={
            "url": "https://example.test/final",
            "title": "Example",
            "text": "rendered",
            "truncated": False,
        },
    ):
        registry.call_tool("read_webpage", {"url": "https://example.test/start"})

    event = json.loads(config.audit_log.read_text(encoding="utf-8").splitlines()[-1])
    assert event["tool"] == "read_webpage"
    assert event["channel"] == "web"
    assert event["backend"] == "browser"

    with mock.patch(
        "mcp4chatgpt.tools.web_ops.combined_search",
        return_value={
            "query": "test",
            "engine": "brave",
            "results": [{"title": "Result", "url": "https://example.test", "snippet": "hit"}],
        },
    ):
        registry.call_tool(
            "search_web",
            {"query": "test", "result_count": 1, "backend": "brave"},
        )

    event = json.loads(config.audit_log.read_text(encoding="utf-8").splitlines()[-1])
    assert event["tool"] == "search_web"
    assert event["channel"] == "web"
    assert event["requested_backend"] == "brave"
    assert event["backend"] == "api"
    assert event["engine"] == "brave"


def test_public_web_failure_audit_records_attempted_browser_without_fallback(tmp_path):
    config = replace(make_config(tmp_path), tool_exposure="compact")
    registry = ToolRegistry(config, AuditLogger(config.audit_log))

    with mock.patch(
        "mcp4chatgpt.tools.browser_search.read",
        side_effect=TimeoutError("browser read timed out"),
    ):
        with pytest.raises(TimeoutError, match="browser read timed out"):
            registry.call_tool("read_webpage", {"url": "https://example.test/start"})

    event = json.loads(config.audit_log.read_text(encoding="utf-8").splitlines()[-1])
    assert event["tool"] == "read_webpage"
    assert event["ok"] is False
    assert event["channel"] == "web"
    assert event["backend"] == "browser"
