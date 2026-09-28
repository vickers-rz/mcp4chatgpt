"""Capability catalog metadata, filtering, ranking, and cursor helpers.

This module intentionally owns semantic discovery metadata so ToolRegistry stays a
routing/validation surface rather than accumulating search heuristics.
"""
from __future__ import annotations

import base64
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class CapabilityMetadata:
    category: str
    keywords: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    deprecated: bool = False
    replacement: str | None = None
    examples: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    description: str
    source: str
    backend_id: str | None
    category: str
    keywords: tuple[str, ...]
    aliases: tuple[str, ...]
    deprecated: bool
    replacement: str | None
    examples: tuple[dict[str, Any], ...]


_CATEGORY_MEMBERS: dict[str, frozenset[str]] = {
    "pdf": frozenset({
        "pdf_inspect", "pdf_search_text", "pdf_redact_text", "pdf_insert_text",
    }),
    "files": frozenset({
        "local_list_files", "local_read_text", "local_expose_file",
        "local_write_file", "local_apply_patch",
    }),
    "jobs": frozenset({
        "local_start_job", "local_job_status", "local_job_logs",
        "local_list_jobs", "local_cancel_job",
        "ext_start_js_job", "ext_get_job", "ext_get_job_result", "ext_cancel_job",
    }),
    "shell": frozenset({
        "local_run_command", "local_command_log_tail",
    }),
    "git": frozenset({
        "local_git_status", "local_git_diff", "local_git_log", "local_git_show",
    }),
    "browser": frozenset({
        "chrome_list_tabs", "chrome_get_active_tab_context",
        "browser_list_tabs", "browser_current_tab", "browser_get_page_text",
        "browser_get_selection", "browser_get_links",
        "search_web", "read_webpage",
        "ext_connection_status", "ext_list_tabs", "ext_get_active_tab",
        "ext_get_dom", "ext_get_selection", "ext_screenshot", "ext_navigate",
        "ext_click_element", "ext_fill_input", "ext_run_js",
        "ext_listen_changes", "ext_search_web", "ext_read_webpage",
        "ext_web_rag", "ext_archive_webpage",
    }),
    "discovery": frozenset({
        "capability_search", "capability_get", "capability_list", "capability_call",
    }),
}


_KEYWORDS: dict[str, tuple[str, ...]] = {
    "pdf_inspect": ("pdf", "read", "inspect", "metadata", "pages", "读取", "阅读", "元数据"),
    "pdf_search_text": ("pdf", "search", "text", "find", "查找", "搜索", "文本"),
    "pdf_redact_text": ("pdf", "edit", "redact", "remove", "编辑", "修改", "脱敏", "删除文本"),
    "pdf_insert_text": ("pdf", "edit", "insert", "write", "编辑", "修改", "插入文本"),
    "local_list_files": ("file", "files", "directory", "list", "文件", "目录", "列出文件"),
    "local_read_text": ("file", "read", "text", "文件", "读取", "文本"),
    "local_expose_file": ("file", "resource", "binary", "文件", "资源", "原始文件"),
    "local_write_file": ("file", "write", "create", "replace", "文件", "写入", "创建", "覆盖"),
    "local_apply_patch": ("file", "patch", "edit", "replace", "文件", "补丁", "编辑", "修改"),
    "local_start_job": ("job", "background", "async", "shell", "后台任务", "后台", "异步任务"),
    "local_job_status": ("job", "background", "status", "后台任务", "状态"),
    "local_job_logs": ("job", "background", "logs", "stdout", "stderr", "后台任务", "日志"),
    "local_list_jobs": ("job", "background", "list", "后台任务", "任务列表"),
    "local_cancel_job": ("job", "background", "cancel", "stop", "后台任务", "取消", "停止"),
    "capability_search": ("discovery", "search", "tool search", "capability", "发现", "检索", "工具搜索"),
    "capability_get": ("discovery", "schema", "definition", "capability", "发现", "定义", "模式"),
    "capability_list": ("discovery", "catalog", "inventory", "list", "发现", "目录", "清单"),
    "capability_call": ("discovery", "call", "invoke", "capability", "发现", "调用"),
    "chrome_list_tabs": ("browser", "chrome", "tabs", "浏览器", "标签页"),
    "chrome_get_active_tab_context": ("browser", "chrome", "active tab", "context", "浏览器", "当前标签页"),
    "browser_list_tabs": ("browser", "tabs", "浏览器", "标签页"),
    "browser_current_tab": ("browser", "current tab", "浏览器", "当前标签页"),
    "browser_get_page_text": ("browser", "page text", "read", "浏览器", "网页文本"),
    "browser_get_selection": ("browser", "selection", "浏览器", "选中文本"),
    "browser_get_links": ("browser", "links", "浏览器", "链接"),
    "search_web": (
        "browser", "local chrome", "web search", "browser session",
        "网页搜索", "本机浏览器", "本机 chrome", "反向gfw", "云端访问失败", "地域限制",
    ),
    "read_webpage": (
        "browser", "local chrome", "read webpage", "url", "browser session",
        "读取网页", "本机浏览器", "本机 chrome", "反向gfw", "云端访问失败", "地域限制", "登录态",
    ),
    "ext_connection_status": ("browser", "chrome extension", "extension", "浏览器扩展", "连接状态"),
    "ext_list_tabs": ("browser", "chrome extension", "tabs", "浏览器扩展", "标签页"),
    "ext_get_active_tab": ("browser", "chrome extension", "active tab", "浏览器扩展", "当前标签页"),
    "ext_get_dom": ("browser", "chrome extension", "dom", "浏览器扩展", "页面结构"),
    "ext_get_selection": ("browser", "chrome extension", "selection", "浏览器扩展", "选中文本"),
    "ext_screenshot": ("browser", "chrome extension", "screenshot", "浏览器扩展", "截图"),
    "ext_navigate": ("browser", "chrome extension", "navigate", "浏览器扩展", "导航"),
    "ext_click_element": ("browser", "chrome extension", "click", "浏览器扩展", "点击"),
    "ext_fill_input": ("browser", "chrome extension", "fill", "input", "浏览器扩展", "输入"),
    "ext_run_js": ("browser", "chrome extension", "javascript", "浏览器扩展", "脚本"),
    "ext_listen_changes": ("browser", "chrome extension", "changes", "浏览器扩展", "变化"),
    "ext_search_web": ("browser", "chrome extension", "web search", "浏览器扩展", "网页搜索"),
    "ext_read_webpage": ("browser", "chrome extension", "read webpage", "浏览器扩展", "读取网页"),
    "ext_web_rag": ("browser", "chrome extension", "rag", "search", "浏览器扩展", "检索增强"),
    "ext_archive_webpage": ("browser", "chrome extension", "archive", "浏览器扩展", "网页归档"),
}


_EXAMPLES: dict[str, tuple[dict[str, Any], ...]] = {
    "pdf_inspect": (
        {"path": "/allowed/report.pdf", "max_chars": 12000},
    ),
    "pdf_search_text": (
        {"path": "/allowed/report.pdf", "query": "invoice", "max_results": 50},
    ),
    "pdf_redact_text": (
        {"path": "/allowed/input.pdf", "query": "SECRET", "replacement": "[redacted]",
         "output_path": "/allowed/redacted.pdf", "case_sensitive": True},
    ),
    "pdf_insert_text": (
        {"path": "/allowed/input.pdf", "page_number": 1, "x": 72, "y": 72,
         "text": "Reviewed", "fontsize": 11, "output_path": "/allowed/reviewed.pdf"},
    ),
    "local_read_text": (
        {"path": "/allowed/project/README.md", "max_chars": 20000},
    ),
    "local_write_file": (
        {"path": "/allowed/project/new.txt", "content": "hello\n", "overwrite": False},
    ),
    "local_apply_patch": (
        {"path": "/allowed/project/app.py", "old": "DEBUG = True", "new": "DEBUG = False"},
    ),
    "local_start_job": (
        {"operation_id": "build-20260927-01", "command": "python -m pytest", "cwd": "/allowed/project",
         "timeout_sec": 900},
    ),
    "local_job_status": (
        {"job_id": "0123456789abcdef0123456789abcdef"},
    ),
    "local_job_logs": (
        {"job_id": "0123456789abcdef0123456789abcdef", "stdout_offset": 0,
         "stderr_offset": 0, "max_bytes": 16384},
    ),
    "local_cancel_job": (
        {"job_id": "0123456789abcdef0123456789abcdef", "grace_sec": 2},
    ),
    "capability_search": (
        {"query": "PDF 编辑", "limit": 5},
    ),
    "capability_get": (
        {"name": "pdf_insert_text"},
    ),
    "capability_list": (
        {"category": "pdf", "limit": 20},
    ),
    "capability_call": (
        {"name": "server_info", "arguments": {}},
    ),
    "search_web": (
        {"query": "site access policy", "result_count": 3, "backend": "browser"},
    ),
    "read_webpage": (
        {"url": "https://example.com/", "max_chars": 30000},
    ),
}


def _category_for(name: str, source: str) -> str:
    for category, members in _CATEGORY_MEMBERS.items():
        if name in members:
            return category
    if source.startswith("downstream:"):
        return "downstream"
    return "other"


def metadata_for(name: str, source: str, *, legacy_alias: bool = False) -> CapabilityMetadata:
    aliases = (f"MCP4ChatGPT.{name}",) if legacy_alias else ()
    return CapabilityMetadata(
        category=_category_for(name, source),
        keywords=_KEYWORDS.get(name, ()),
        aliases=aliases,
        deprecated=False,
        replacement=None,
        examples=deepcopy(_EXAMPLES.get(name, ())),
    )


def build_entry(
    name: str,
    description: str,
    source: str,
    backend_id: str | None,
    *,
    legacy_alias: bool = False,
) -> CatalogEntry:
    metadata = metadata_for(name, source, legacy_alias=legacy_alias)
    return CatalogEntry(
        name=name,
        description=description,
        source=source,
        backend_id=backend_id,
        category=metadata.category,
        keywords=metadata.keywords,
        aliases=metadata.aliases,
        deprecated=metadata.deprecated,
        replacement=metadata.replacement,
        examples=metadata.examples,
    )


def normalize_filters(args: dict[str, Any]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for key in ("source", "backend", "category"):
        value = args.get(key)
        if value is None:
            result[key] = None
            continue
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 256:
            raise ValueError(f"capability_{key}_invalid")
        result[key] = value.strip().casefold()
    return result


def entry_matches_filters(entry: CatalogEntry, filters: dict[str, str | None]) -> bool:
    if filters.get("source") is not None and entry.source.casefold() != filters["source"]:
        return False
    backend = entry.backend_id.casefold() if isinstance(entry.backend_id, str) else None
    if filters.get("backend") is not None and backend != filters["backend"]:
        return False
    if filters.get("category") is not None and entry.category.casefold() != filters["category"]:
        return False
    return True


_WORD_RE = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)


def _query_terms(query: str) -> list[str]:
    terms = [part.casefold() for part in _WORD_RE.findall(query)]
    return terms or [query.strip().casefold()]


def _score_entry(entry: CatalogEntry, query_text: str, terms: list[str]) -> int | None:
    name = entry.name.casefold()
    name_words = name.replace("_", " ").replace("-", " ")
    description = entry.description.casefold()
    category = entry.category.casefold()
    source = entry.source.casefold()
    backend = (entry.backend_id or "").casefold()
    keyword_values = tuple(keyword.casefold() for keyword in entry.keywords)
    keywords = " ".join(keyword_values)
    aliases = " ".join(alias.casefold() for alias in entry.aliases)

    fields = (name, name_words, keywords, description, category, source, backend, aliases)
    if any(not any(term in field for field in fields) for term in terms):
        return None

    score = 0
    if name == query_text:
        score += 100_000
    elif name_words == query_text:
        score += 90_000

    if query_text in keyword_values:
        score += 20_000

    name_tokens = set(_query_terms(name_words))
    keyword_tokens = set()
    for keyword in keyword_values:
        keyword_tokens.update(_query_terms(keyword))

    for term in terms:
        if term in name_tokens:
            score += 5_000
        elif term in name or term in name_words:
            score += 3_000

        if term in keyword_tokens:
            score += 2_000
        elif term in keywords:
            score += 1_200

        if term in description:
            score += 400
        if term in category:
            score += 150
        if term in backend:
            score += 120
        if term in source:
            score += 80
        if term in aliases:
            score += 60

    return score


def rank_entries(
    entries: Iterable[CatalogEntry],
    query: str,
    filters: dict[str, str | None],
) -> list[CatalogEntry]:
    query_text = query.strip().casefold()
    terms = _query_terms(query_text)
    ranked: list[tuple[int, str, CatalogEntry]] = []
    for entry in entries:
        if not entry_matches_filters(entry, filters):
            continue
        score = _score_entry(entry, query_text, terms)
        if score is None:
            continue
        ranked.append((-score, entry.name, entry))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in ranked]


def filtered_entries(
    entries: Iterable[CatalogEntry],
    filters: dict[str, str | None],
) -> list[CatalogEntry]:
    return sorted(
        (entry for entry in entries if entry_matches_filters(entry, filters)),
        key=lambda entry: entry.name,
    )


def encode_cursor(catalog_version: str, filters: dict[str, str | None], next_position: int) -> str:
    payload = {
        "v": catalog_version,
        "f": {key: filters.get(key) for key in ("source", "backend", "category")},
        "p": next_position,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("capability_cursor_invalid")
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except Exception as exc:
        raise ValueError("capability_cursor_invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("capability_cursor_invalid")
    version = payload.get("v")
    filters = payload.get("f")
    position = payload.get("p")
    if (
        not isinstance(version, str)
        or not isinstance(filters, dict)
        or type(position) is not int
        or position < 0
        or set(filters) != {"source", "backend", "category"}
        or any(value is not None and not isinstance(value, str) for value in filters.values())
    ):
        raise ValueError("capability_cursor_invalid")
    return {"catalog_version": version, "filters": filters, "position": position}


def metadata_payload(entry: CatalogEntry) -> dict[str, Any]:
    return {
        "category": entry.category,
        "aliases": list(entry.aliases),
        "deprecated": entry.deprecated,
        "replacement": entry.replacement,
        "examples": deepcopy(list(entry.examples)),
    }


def examples_for(name: str, source: str = "local") -> tuple[dict[str, Any], ...]:
    return deepcopy(metadata_for(name, source).examples)
