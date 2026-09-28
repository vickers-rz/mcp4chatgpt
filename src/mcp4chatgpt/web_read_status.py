"""Conservative result classification for local-browser webpage reads."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

READ_STATUSES = frozenset({
    "ok", "empty", "login_required", "challenge",
    "access_blocked", "timeout", "unavailable",
})
CLASSIFIER_VERSION = "web_read_status_v1"
_MIN_MEANINGFUL_TEXT = 80

_CHALLENGE_MARKERS = (
    "verify you are human", "checking your browser", "just a moment",
    "security verification", "captcha", "robot check",
    "人机验证", "安全验证", "验证码", "请完成验证", "请验证您是真人",
)
_LOGIN_MARKERS = (
    "sign in", "log in", "login", "sign-in", "登录", "登入", "请登录",
)
_LOGIN_CONTEXT_MARKERS = (
    "username", "email address", "account", "用户名", "账号", "邮箱", "手机号",
)
_ACCESS_BLOCK_MARKERS = (
    "access denied", "403 forbidden", "request blocked",
    "not available in your region", "unavailable in your region",
    "not available in your country", "访问被拒绝", "拒绝访问",
    "禁止访问", "无权访问", "请求被拦截", "所在地区不可用",
)
_LOGIN_PATH_RE = re.compile(
    r"(?:^|/)(?:login|log-in|signin|sign-in|auth|sso)(?:/|$)",
    re.IGNORECASE,
)


def _compact(value: Any, *, limit: int = 8000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _contains_any(text: str, markers: tuple[str, ...]) -> list[str]:
    lowered = text.casefold()
    return [marker for marker in markers if marker.casefold() in lowered]


def _base_evidence(page: dict[str, Any]) -> dict[str, Any]:
    text = _compact(page.get("text"), limit=2_000_000)
    return {
        "classification_version": CLASSIFIER_VERSION,
        "signals": [],
        "text_length": len(text),
        "original_length": int(page.get("original_length") or len(text)),
        "title": _compact(page.get("title"), limit=500),
        "final_url": _compact(page.get("url"), limit=2000),
        "canonical_url": _compact(page.get("canonical_url"), limit=2000),
        "extraction_method": _compact(page.get("extraction_method"), limit=200),
        "truncated": bool(page.get("truncated")),
    }


def classify_page(page: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Classify the rendered result without inferring HTTP status or root cause."""
    evidence = _base_evidence(page)
    text = _compact(page.get("text"), limit=2_000_000)
    title = evidence["title"]
    final_url = evidence["final_url"]
    probe = text[:6000]

    title_hits = _contains_any(title, _CHALLENGE_MARKERS)
    text_hits = _contains_any(probe, _CHALLENGE_MARKERS)
    if title_hits or (text_hits and len(text) <= 6000):
        evidence["signals"] = [
            *(f"title:{x}" for x in title_hits),
            *(f"text:{x}" for x in text_hits[:3]),
        ]
        return "challenge", evidence

    try:
        login_url = bool(_LOGIN_PATH_RE.search(urlparse(final_url).path or ""))
    except ValueError:
        login_url = False
    title_hits = _contains_any(title, _LOGIN_MARKERS)
    text_hits = _contains_any(probe, _LOGIN_MARKERS)
    context_hits = _contains_any(probe, _LOGIN_CONTEXT_MARKERS)
    if login_url or title_hits or (text_hits and context_hits and len(text) <= 8000):
        signals: list[str] = []
        if login_url:
            signals.append("url:login_path")
        signals.extend(f"title:{x}" for x in title_hits)
        signals.extend(f"text:{x}" for x in text_hits[:2])
        signals.extend(f"text:{x}" for x in context_hits[:2])
        evidence["signals"] = signals
        return "login_required", evidence

    title_hits = _contains_any(title, _ACCESS_BLOCK_MARKERS)
    text_hits = _contains_any(probe[:3000], _ACCESS_BLOCK_MARKERS)
    if title_hits or (text_hits and len(text) <= 5000):
        evidence["signals"] = [
            *(f"title:{x}" for x in title_hits),
            *(f"text:{x}" for x in text_hits[:3]),
        ]
        return "access_blocked", evidence

    if len(text) < _MIN_MEANINGFUL_TEXT:
        evidence["signals"] = [f"text_below_meaningful_threshold:{_MIN_MEANINGFUL_TEXT}"]
        return "empty", evidence

    evidence["signals"] = ["meaningful_rendered_text"]
    return "ok", evidence


def classify_error(exc: BaseException) -> tuple[str, dict[str, Any]]:
    """Map browser transport/runtime failures to a structured retrieval status."""
    message = _compact(exc, limit=1200)
    lowered = message.casefold()
    evidence = {
        "classification_version": CLASSIFIER_VERSION,
        "signals": [],
        "error_type": type(exc).__name__,
        "error": message,
    }
    if isinstance(exc, TimeoutError) or "timed out" in lowered or "timeout" in lowered:
        evidence["signals"] = ["browser_timeout"]
        return "timeout", evidence
    if "not connected" in lowered:
        evidence["signals"] = ["extension_not_connected"]
    elif "not running" in lowered:
        evidence["signals"] = ["extension_bridge_not_running"]
    elif "queue is saturated" in lowered:
        evidence["signals"] = ["browser_queue_saturated"]
    else:
        evidence["signals"] = ["browser_runtime_unavailable"]
    return "unavailable", evidence
