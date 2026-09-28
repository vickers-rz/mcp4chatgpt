from __future__ import annotations

import pytest

from mcp4chatgpt import web_read_status


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        (
            {
                "url": "https://example.test/article",
                "title": "Policy article",
                "text": "Substantive rendered article body. " * 8,
                "extraction_method": "heuristic:article",
            },
            "ok",
        ),
        (
            {
                "url": "https://example.test/empty",
                "title": "Empty shell",
                "text": "   ",
                "extraction_method": "body_fallback",
            },
            "empty",
        ),
        (
            {
                "url": "https://example.test/login",
                "title": "Sign in",
                "text": "Sign in to your account",
                "extraction_method": "body_fallback",
            },
            "login_required",
        ),
        (
            {
                "url": "https://example.test/check",
                "title": "Just a moment...",
                "text": "Verify you are human before continuing.",
                "extraction_method": "body_fallback",
            },
            "challenge",
        ),
        (
            {
                "url": "https://example.test/blocked",
                "title": "Access denied",
                "text": "Access denied. Your request has been blocked.",
                "extraction_method": "body_fallback",
            },
            "access_blocked",
        ),
    ],
)
def test_classify_page_statuses(page, expected):
    status, evidence = web_read_status.classify_page(page)

    assert status == expected
    assert evidence["classification_version"] == "web_read_status_v1"
    assert evidence["final_url"] == page["url"]
    assert isinstance(evidence["signals"], list)
    assert evidence["signals"]


def test_long_article_discussing_block_pages_is_not_misclassified():
    page = {
        "url": "https://example.test/research",
        "title": "Browser reliability study",
        "text": (
            "This article explains why a site may show access denied or ask users "
            "to verify you are human. Those phrases are examples, not the current "
            "page state. "
        ) * 80,
        "extraction_method": "heuristic:article",
    }

    status, evidence = web_read_status.classify_page(page)

    assert status == "ok"
    assert evidence["signals"] == ["meaningful_rendered_text"]


def test_access_blocked_is_observation_not_censorship_inference():
    status, evidence = web_read_status.classify_page({
        "url": "https://example.test/blocked",
        "title": "403 Forbidden",
        "text": "403 Forbidden. Access denied.",
        "extraction_method": "body_fallback",
    })

    assert status == "access_blocked"
    serialized = repr(evidence).casefold()
    assert "gfw" not in serialized
    assert "censorship" not in serialized


@pytest.mark.parametrize(
    ("error", "expected_status", "signal"),
    [
        (TimeoutError("read timeout"), "timeout", "browser_timeout"),
        (RuntimeError("Extension command timed out after 30s"), "timeout", "browser_timeout"),
        (
            RuntimeError("Chrome extension is not connected"),
            "unavailable",
            "extension_not_connected",
        ),
        (
            RuntimeError("ext_bridge is not running"),
            "unavailable",
            "extension_bridge_not_running",
        ),
        (
            RuntimeError("Browser research queue is saturated"),
            "unavailable",
            "browser_queue_saturated",
        ),
        (
            RuntimeError("browser execution failed"),
            "unavailable",
            "browser_runtime_unavailable",
        ),
    ],
)
def test_classify_error(error, expected_status, signal):
    status, evidence = web_read_status.classify_error(error)

    assert status == expected_status
    assert evidence["signals"] == [signal]
    assert evidence["error_type"] == type(error).__name__
