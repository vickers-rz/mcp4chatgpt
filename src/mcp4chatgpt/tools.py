"""MCP 工具声明、JSON Schema 与运行时分派中心。

MCP 服务向客户端暴露的核心不是 Python 函数本身，而是一组可发现的工具描述。
每个 :class:`Tool` 同时包含：稳定的工具名、给模型阅读的说明、用于参数校验和
生成调用界面的 JSON Schema，以及真正执行本机操作的 handler。

``build_tools()`` 负责声明能力；``ToolRegistry`` 负责执行能力。这种“声明与执行
分离”的结构非常重要：客户端可先通过 ``tools/list`` 获得机器可读契约，再通过
``tools/call`` 提交参数。新增工具时，应先设计最小且明确的输入 schema，再把
安全校验放入具体操作模块，而不是依赖模型遵守自然语言说明。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import threading
import time
from copy import deepcopy
from referencing import Registry
from referencing.exceptions import NoSuchResource
from dataclasses import dataclass
from typing import Any, Callable

from jsonschema import Draft202012Validator
from jsonschema.validators import validator_for
from jsonschema.exceptions import SchemaError, ValidationError

from . import __version__
from .audit import AuditLogger
from .capability_catalog import (
    build_entry,
    decode_cursor,
    encode_cursor,
    examples_for,
    filtered_entries,
    metadata_payload,
    normalize_filters,
    rank_entries,
)
from .config import Config
from . import chrome_ops, computer_ops, ext_ops, file_resources, knowledge_ops, local_ops, pdf_ops, terminal_ops, web_ops
from . import browser_search, web_archive
from .mcp_types import RawMCPToolResult
from .downstream.manager import DownstreamMCPManager


ToolHandler = Callable[[Config, dict[str, Any]], Any]

@dataclass(frozen=True)
class CallContext:
    """Host-created constraints; never populated from tool arguments.

    Deadline is admission-only: synchronous handlers are not interrupted.
    Existing subsystem authorization remains authoritative on every call.
    """

    run_id: str = ""
    entrypoint: str = "direct"
    allowed_tools: frozenset[str] | None = None
    expected_catalog_version: str | None = None
    deadline: float | None = None
    capability_bindings: tuple[tuple[str, str], ...] | None = None


CO_TE_APP_KEYS = [
    "android_studio",
    "appcode",
    "apple_notes",
    "bbedit",
    "clion",
    "cursor",
    "datagrip",
    "goland",
    "intellij",
    "iterm2",
    "notion",
    "phpstorm",
    "prompt",
    "pycharm",
    "quip",
    "rider",
    "rubymine",
    "script_editor",
    "sublime_text",
    "terminal",
    "termius",
    "textedit",
    "vscode",
    "vscode_insiders",
    "vscodium",
    "warp",
    "webstorm",
    "windsurf",
    "xcode",
]

CO_TE_TERMINAL_APP_KEYS = ["terminal", "iterm2", "termius"]


@dataclass(frozen=True)
class Tool:
    """一项可被 MCP 客户端发现和调用的工具定义。

    ``name`` 是协议级稳定标识；``description`` 主要供模型理解用途；
    ``input_schema`` 是机器可读的参数契约；``handler`` 才是实际 Python 实现。
    使用冻结 dataclass 可避免服务运行期间意外改写工具元数据。
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    annotations_override: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    backend_id: str | None = None
    backend_instance_id: str | None = None
    original_name: str | None = None

    def definition(self, *, auth_required: bool = True) -> dict[str, Any]:
        security_schemes = [{"type": "oauth2", "scopes": ["local", "web", "knowledge"]}]
        annotations = (
            self.annotations_override
            if self.annotations_override is not None
            else _annotations_for_tool(self.name)
        )
        title = self.name.replace("_", " ").title()
        definition = {
            "name": self.name,
            "title": title,
            "description": self.description,
            "inputSchema": self.input_schema,
            # ChatGPT still reads some descriptor data from _meta for
            # compatibility; keep this mirrored with the public field.
            "_meta": {
                "openai/toolInvocation/invoking": f"Running {title}",
                "openai/toolInvocation/invoked": f"Finished {title}",
            },
            "annotations": annotations,
        }
        if self.output_schema is not None:
            definition["outputSchema"] = self.output_schema
        if auth_required:
            definition["securitySchemes"] = security_schemes
            definition["_meta"]["securitySchemes"] = security_schemes
        return deepcopy(definition)


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    """构造 MCP 工具参数使用的严格 JSON Schema 对象。

    ``additionalProperties=False`` 会拒绝未声明字段，既能尽早发现模型拼错参数名，
    也能避免未来新增 handler 参数时被旧客户端无意触发。
    """
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def _annotations_for_tool(name: str) -> dict[str, bool]:
    """Classify tool side effects for ChatGPT's approval and safety UI.

    These hints are advisory only; the server still enforces OAuth, path
    allowlists, command blocking, and audit logging independently.
    """
    mutating = {
        "pdf_redact_text", "pdf_insert_text",
        "computer_click", "computer_press_key", "computer_type_text", "computer_type_keyboard", "computer_request_permissions", "computer_launch_app", "computer_activate_window", "computer_pointer_move", "computer_pointer_click", "computer_pointer_drag", "computer_pointer_scroll",
        "local_write_file",
        "local_apply_patch",
        "local_run_command",
        "local_start_job",
        "local_cancel_job",
        "app_write_text",
        "terminal_run_command",
        "terminal_send_input",
        "web_add_to_knowledge",
        "knowledge_add_source",
        "ext_navigate",
        "ext_click_element",
        "ext_fill_input",
        "ext_run_js",
        "ext_start_js_job",
        "ext_cancel_job",
        "ext_search_web",
        "ext_read_webpage",
        "ext_web_rag",
        "ext_archive_webpage",
    }
    open_world = (name.startswith("web_") and not name.startswith("web_archive_")) or name in {"ext_search_web", "ext_read_webpage", "ext_web_rag", "ext_archive_webpage"} or name == "search_web" or name in {
        "computer_list_apps", "computer_get_state", "computer_screenshot", "computer_list_displays", "computer_screenshot_display", "computer_launch_app", "computer_activate_window", "computer_pointer_move", "computer_pointer_click", "computer_pointer_drag", "computer_pointer_scroll", "computer_click", "computer_press_key", "computer_type_text", "computer_type_keyboard",
        "local_run_command",
        "local_start_job",
        "app_get_context",
        "app_write_text",
        "terminal_run_command",
        "terminal_send_input",
        "terminal_list_supported_apps",
        "terminal_get_app_context",
        "chrome_list_tabs",
        "chrome_get_active_tab_context",
        "browser_list_tabs",
        "browser_current_tab",
        "browser_get_page_text",
        "browser_get_selection",
        "browser_get_links",
        "ext_connection_status",
        "ext_list_tabs",
        "ext_get_active_tab",
        "ext_get_dom",
        "ext_get_selection",
        "ext_screenshot",
        "ext_navigate",
        "ext_click_element",
        "ext_fill_input",
        "ext_run_js",
        "ext_start_js_job",
        "ext_get_job",
        "ext_get_job_result",
        "ext_cancel_job",
        "ext_listen_changes",
    }
    destructive = name in {
        "pdf_redact_text", "pdf_insert_text",
        "computer_click", "computer_press_key", "computer_type_text", "computer_type_keyboard", "computer_launch_app", "computer_activate_window", "computer_pointer_move", "computer_pointer_click", "computer_pointer_drag", "computer_pointer_scroll",
        "local_write_file",
        "local_apply_patch",
        "local_run_command",
        "local_start_job",
        "local_cancel_job",
        "app_write_text",
        "terminal_run_command",
        "terminal_send_input",
        "ext_navigate",
        "ext_click_element",
        "ext_fill_input",
        "ext_run_js",
        "ext_start_js_job",
        "ext_cancel_job",
    }
    protocol_idempotent = name in {"local_start_job", "local_cancel_job"}
    return {
        "readOnlyHint": name not in mutating,
        "destructiveHint": destructive,
        "openWorldHint": open_world,
        "idempotentHint": name not in mutating or protocol_idempotent,
    }


def _ok(result: Any) -> dict[str, Any]:
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
    structured_content = result if isinstance(result, dict) else {"result": result}
    return {"content": [{"type": "text", "text": text}], "structuredContent": structured_content}


def _local_expose_file(config: Config, args: dict[str, Any]) -> RawMCPToolResult:
    _record, result = file_resources.expose(config, args["path"])
    return RawMCPToolResult(result)


def _server_info(config: Config, _args: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "mcp4chatgpt",
        "version": __version__,
        "mcp_url": config.mcp_url,
        "allowed_roots": [str(root) for root in config.allowed_roots],
        "knowledge_store_dir": str(config.knowledge_store_dir),
        "firecrawl_configured": bool(config.firecrawl_api_key),
        "co_te_path": str(config.co_te_path),
        "personal_full_access": getattr(config, "personal_full_access", False),
        "computer": {
            "mode": config.computer_mode,
            "backend": getattr(config, "computer_backend", "native"),
            "allowed_apps": list(config.computer_allowed_apps),
            "platform": platform.system(),
        },
    }


def _web_add_to_knowledge(config: Config, args: dict[str, Any]) -> dict[str, Any]:
    scraped = web_ops.scrape(config, args["url"], formats=["markdown"])
    data = scraped.get("data", scraped)
    text = data.get("markdown") or data.get("content") or json.dumps(data, ensure_ascii=False)
    title = data.get("metadata", {}).get("title") or args.get("title") or args["url"]
    added = knowledge_ops.add_source(config, title=title, text=text, url=args["url"], metadata={"web_result": data.get("metadata", {})})
    return {"scrape": scraped, "knowledge": added}


def _search_web(config: Config, args: dict[str, Any]) -> dict[str, Any]:
    deep_read = bool(args.get("deep_read", False))
    result_count = int(args.get("result_count", 3))
    backend = str(args.get("backend", "browser")).strip().lower()
    if backend not in {"browser", "auto", "brave", "firecrawl"}:
        raise ValueError("backend must be one of: browser, auto, brave, firecrawl")
    browser_fallback_reason = None
    if backend in {"browser", "auto"}:
        try:
            browser_result = browser_search.search(config, args["query"], result_count)
            browser_items = browser_result.get("results", [])
            if not browser_items:
                raise RuntimeError(browser_result.get("message") or "Browser search returned no results.")
            compact_results = []
            for index, item in enumerate(browser_items[:result_count]):
                compact = {
                    "title": str(item.get("title", "")),
                    "url": str(item.get("url", "")),
                    "snippet": str(item.get("snippet", ""))[:2000],
                    "source": "chrome_bing",
                }
                if deep_read and index < 2 and compact["url"]:
                    try:
                        page = browser_search.read(config, compact["url"], max_chars=8000)
                        compact["page_text"] = str(page.get("text", ""))[:8000]
                    except Exception as exc:
                        compact["fetch_error"] = str(exc)[:500]
                compact_results.append(compact)
            return {"query": args["query"], "engine": "chrome_bing", "backend": "browser", "results": compact_results}
        except Exception as exc:
            if backend == "browser":
                raise
            browser_fallback_reason = str(exc)[:500]
    response = web_ops.combined_search(
        config,
        args["query"],
        result_count,
        engine="auto" if backend == "auto" else backend,
        fetch_content=deep_read,
        fetch_limit=min(result_count, 2) if deep_read else 0,
    )
    compact_results = []
    for result in response.get("results", []):
        compact_result = {
            "title": str(result.get("title", "")),
            "url": str(result.get("url") or result.get("link") or ""),
            "snippet": str(result.get("snippet") or result.get("content") or "")[:2000],
            "source": str(result.get("source", "")),
        }
        if deep_read and result.get("markdown"):
            compact_result["markdown"] = str(result["markdown"])[:8000]
        if result.get("fetch_error"):
            compact_result["fetch_error"] = str(result["fetch_error"])[:500]
        compact_results.append(compact_result)

    compact_response = {
        "query": response.get("query", args["query"]),
        "engine": response.get("engine", "auto"),
        "results": compact_results,
        "backend": "api",
    }
    if browser_fallback_reason:
        compact_response["browser_fallback_reason"] = browser_fallback_reason
    if response.get("fallback_reason"):
        compact_response["fallback_reason"] = str(response["fallback_reason"])[:500]
    return compact_response


def _web_search_auto_compat(config: Config, args: dict[str, Any]) -> dict[str, Any]:
    deep_read = bool(args.get("deep_read", False))
    return web_ops.combined_search(
        config,
        args["query"],
        int(args.get("limit", 3)),
        engine="auto",
        fetch_content=deep_read,
        fetch_limit=int(args.get("fetch_limit", 1 if deep_read else 0)),
    )


def build_tools(*, computer_mode: str = "off") -> list[Tool]:
    # The tool list is intentionally centralized. The HTTP layer only knows
    # about JSON-RPC; capability grouping and schemas live here.
    tools = [
        Tool("server_info", "Return service status, enabled backends, and safety boundaries.", _schema({}), _server_info),
        Tool("pdf_inspect", "Read PDF metadata, table of contents, page dimensions, and extracted text from an allowed local PDF.", _schema({"path": {"type": "string"}, "max_chars": {"type": "integer", "minimum": 100, "maximum": 100000, "default": 20000}}, ["path"]), lambda c, a: pdf_ops.inspect(c, a["path"], max_chars=int(a.get("max_chars", 20000)))),
        Tool("pdf_search_text", "Search selectable text in an allowed local PDF and return matching page numbers and rectangles.", _schema({"path": {"type": "string"}, "query": {"type": "string", "minLength": 1}, "max_results": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100}}, ["path", "query"]), lambda c, a: pdf_ops.search(c, a["path"], a["query"], max_results=int(a.get("max_results", 100)))),
        Tool("pdf_redact_text", "Permanently remove every selectable-text match from an allowed local PDF, optionally inserting replacement text. Writes a new PDF and refuses to overwrite existing files; scanned images require OCR and are not text-searchable.", _schema({"path": {"type": "string"}, "query": {"type": "string", "minLength": 1}, "replacement": {"type": "string", "default": ""}, "output_path": {"type": "string"}, "case_sensitive": {"type": "boolean", "default": True}}, ["path", "query"]), lambda c, a: pdf_ops.redact_text(c, a["path"], a["query"], replacement=a.get("replacement", ""), output_path=a.get("output_path"), case_sensitive=bool(a.get("case_sensitive", True)))),
        Tool("pdf_insert_text", "Insert text at a page coordinate in an allowed local PDF. Page numbers are 1-based and coordinates use PDF points from the top-left. Writes a new PDF and refuses to overwrite existing files.", _schema({"path": {"type": "string"}, "page_number": {"type": "integer", "minimum": 1}, "x": {"type": "number"}, "y": {"type": "number"}, "text": {"type": "string", "minLength": 1, "maxLength": 10000}, "fontsize": {"type": "number", "minimum": 1, "maximum": 200, "default": 11}, "output_path": {"type": "string"}}, ["path", "page_number", "x", "y", "text"]), lambda c, a: pdf_ops.insert_text(c, a["path"], int(a["page_number"]), float(a["x"]), float(a["y"]), a["text"], fontsize=float(a.get("fontsize", 11)), output_path=a.get("output_path"))),
        Tool("computer_permissions", "Return current macOS Accessibility, Screen Recording, and event-posting permission status for native Computer Use.", _schema({}), lambda c, a: computer_ops.permission_status(c)),
        Tool("computer_request_permissions", "Request macOS native Computer Use permissions. This may trigger system permission prompts; a process restart can still be required after approval.", _schema({"accessibility": {"type": "boolean", "default": True}, "screen_recording": {"type": "boolean", "default": True}, "event_posting": {"type": "boolean", "default": True}}), lambda c, a: computer_ops.request_permissions(c, accessibility=bool(a.get("accessibility", True)), screen_recording=bool(a.get("screen_recording", True)), event_posting=bool(a.get("event_posting", True)))),
        Tool("computer_list_apps", "List running macOS apps in the explicit computer allowlist. In auto mode OpenAI CUA/Sky is the primary inventory source.", _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 64, "default": 32}}), lambda c, a: computer_ops.list_apps(c, int(a.get("limit", 32)))),
        Tool("computer_get_state", "Inspect one allowlisted macOS app window and its bounded Accessibility tree. In auto mode OpenAI CUA/Sky is preferred while native WindowServer/AX inventory supplies exact multi-window identity. If the app has multiple eligible windows and window_id is omitted, returns window_selection_required with stable CUA window tokens; select one and observe again. Background selection does not make the target app frontmost. Refresh after each action.", _schema({"app_id": {"type": "string"}, "pid": {"type": "integer", "minimum": 1, "description": "Exact process from computer_list_apps. Supplying pid deliberately uses the native identity-fenced path."}, "window_id": {"type": "string", "description": "Opaque window token returned by computer_get_state. CUA tokens are bound to the target process and WindowServer window identity."}, "max_elements": {"type": "integer", "minimum": 1, "maximum": 400, "default": 200}}, ["app_id"]), lambda c, a: computer_ops.get_state(c, a["app_id"], a.get("window_id"), int(a.get("max_elements", 200)), a.get("pid"))),
        Tool("computer_screenshot", "Capture an exact allowlisted app window as an MCP image. For a CUA window token, the adapter rebinds and verifies the same WindowServer/AX window in the background before Sky capture; stale or mismatched windows fail closed. Native capture remains the fallback for native-bound windows.", _schema({"app_id": {"type": "string"}, "pid": {"type": "integer", "minimum": 1, "description": "Exact process from computer_list_apps; supplying pid uses the native path."}, "window_id": {"type": "string", "description": "Exact opaque window token returned by computer_get_state."}}, ["app_id", "window_id"]), lambda c, a: computer_ops.screenshot(c, a["app_id"], a["window_id"], a.get("pid"))),
        Tool("computer_list_displays", "List active macOS displays for full-desktop Computer Use. Requires MCP_COMPUTER_ALLOWED_APPS=*.", _schema({}), lambda c, a: computer_ops.list_displays(c)),
        Tool("computer_screenshot_display", "Capture one active display. Returns source pixel dimensions plus a one-effect display_snapshot_id for pointer actions.", _schema({"display_id": {"type": "string"}}, ["display_id"]), lambda c, a: computer_ops.screenshot_display(c, a["display_id"])),
        Tool("computer_launch_app", "Launch or bind an allowlisted macOS application. CUA/Sky is preferred and can launch the app in the background; the native helper is fallback.", _schema({"app_id": {"type": "string"}}, ["app_id"]), lambda c, a: computer_ops.launch_app(c, a["app_id"])),
        Tool("computer_activate_window", "Bring an allowlisted app window to the foreground and verify it became the focused window.", _schema({"app_id": {"type": "string"}, "pid": {"type": "integer", "minimum": 1}, "window_id": {"type": "string"}}, ["app_id", "window_id"]), lambda c, a: computer_ops.activate_window(c, a)),
        Tool("computer_pointer_move", "Move the macOS pointer to source-pixel coordinates from a fresh computer_screenshot_display. The display snapshot is consumed once.", _schema({"display_id": {"type": "string"}, "display_snapshot_id": {"type": "string"}, "x": {"type": "integer", "minimum": 0}, "y": {"type": "integer", "minimum": 0}}, ["display_id", "display_snapshot_id", "x", "y"]), lambda c, a: computer_ops.pointer_move(c, a)),
        Tool("computer_pointer_click", "Move and click at source-pixel coordinates from a fresh computer_screenshot_display. Supports left/right click and single/double click; the display snapshot is consumed once.", _schema({"display_id": {"type": "string"}, "display_snapshot_id": {"type": "string"}, "x": {"type": "integer", "minimum": 0}, "y": {"type": "integer", "minimum": 0}, "button": {"type": "string", "enum": ["left", "right"], "default": "left"}, "click_count": {"type": "integer", "enum": [1, 2], "default": 1}}, ["display_id", "display_snapshot_id", "x", "y"]), lambda c, a: computer_ops.pointer_click(c, a)),
        Tool("computer_pointer_drag", "Drag with the left mouse button between source-pixel coordinates from a fresh display screenshot. The display snapshot is consumed once.", _schema({"display_id": {"type": "string"}, "display_snapshot_id": {"type": "string"}, "from_x": {"type": "integer", "minimum": 0}, "from_y": {"type": "integer", "minimum": 0}, "to_x": {"type": "integer", "minimum": 0}, "to_y": {"type": "integer", "minimum": 0}}, ["display_id", "display_snapshot_id", "from_x", "from_y", "to_x", "to_y"]), lambda c, a: computer_ops.pointer_drag(c, a)),
        Tool("computer_pointer_scroll", "Scroll at source-pixel coordinates from a fresh display screenshot. Positive/negative deltas select direction; the display snapshot is consumed once.", _schema({"display_id": {"type": "string"}, "display_snapshot_id": {"type": "string"}, "x": {"type": "integer", "minimum": 0}, "y": {"type": "integer", "minimum": 0}, "delta_y": {"type": "integer", "minimum": -120, "maximum": 120}, "delta_x": {"type": "integer", "minimum": -120, "maximum": 120, "default": 0}}, ["display_id", "display_snapshot_id", "x", "y", "delta_y"]), lambda c, a: computer_ops.pointer_scroll(c, a)),
        Tool("computer_click", "Click a fresh observed element. CUA/Sky is preferred for CUA snapshots so the target can stay in the background without moving the user's physical pointer; observe again afterward.", _schema({"app_id": {"type": "string"}, "pid": {"type": "integer", "minimum": 1, "description": "Exact process from computer_list_apps; required to disambiguate multiple instances."}, "window_id": {"type": "string"}, "snapshot_id": {"type": "string"}, "element_id": {"type": "string"}}, ["app_id", "window_id", "snapshot_id", "element_id"]), lambda c, a: computer_ops.click(c, a)),
        Tool("computer_press_key", "Send one key or shortcut chord to the observed app. CUA/Sky is preferred for CUA snapshots so the target can remain in the background; native Quartz is fallback.", _schema({"app_id": {"type": "string"}, "pid": {"type": "integer", "minimum": 1, "description": "Exact process from computer_list_apps; required to disambiguate multiple instances."}, "window_id": {"type": "string"}, "snapshot_id": {"type": "string"}, "key": {"type": "string", "enum": ["A","B","C","D","E","F","G","H","I","J","K","L","M","N","O","P","Q","R","S","T","U","V","W","X","Y","Z","0","1","2","3","4","5","6","7","8","9","Return","Tab","Escape","Left","Right","Up","Down","Backspace","Space","DeleteForward","Home","End","PageUp","PageDown"]}, "modifiers": {"type": "array", "items": {"type": "string", "enum": ["shift", "option", "control", "command"]}, "maxItems": 4}}, ["app_id", "window_id", "snapshot_id", "key"]), lambda c, a: computer_ops.press_key(c, a)),
        Tool("computer_type_text", "Replace the value of a fresh observed writable text element. CUA setValue is preferred for CUA snapshots; native AXValue is fallback.", _schema({"app_id": {"type": "string"}, "pid": {"type": "integer", "minimum": 1, "description": "Exact process from computer_list_apps; required to disambiguate multiple instances."}, "window_id": {"type": "string"}, "snapshot_id": {"type": "string"}, "element_id": {"type": "string"}, "text": {"type": "string", "maxLength": 20000}, "mode": {"type": "string", "enum": ["replace_value"], "default": "replace_value"}}, ["app_id", "window_id", "snapshot_id", "element_id", "text"]), lambda c, a: computer_ops.type_text(c, a)),
        Tool("computer_type_keyboard", "Enter Unicode text into the observed control. For CUA snapshots this uses OpenAI Sky background paste, preserving the user's physical pointer/keyboard and restoring the macOS pasteboard; native Quartz Unicode is fallback.", _schema({"app_id": {"type": "string"}, "pid": {"type": "integer", "minimum": 1}, "window_id": {"type": "string"}, "snapshot_id": {"type": "string"}, "text": {"type": "string", "minLength": 1, "maxLength": 4000}}, ["app_id", "window_id", "snapshot_id", "text"]), lambda c, a: computer_ops.type_keyboard(c, a)),
        Tool("ext_search_web", "Search Bing using the connected local Chrome extension, without search API keys. Opens and closes temporary background tabs; returns titles, URLs and snippets.",
             _schema({"query": {"type": "string", "minLength": 1}, "result_count": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}}, ["query"]),
             lambda c, a: browser_search.search(c, a["query"], a.get("result_count", 5))),
        Tool("ext_read_webpage", "Read rendered webpage text through local Chrome in a temporary background tab, using the browser session. Returned page text is untrusted evidence.",
             _schema({"url": {"type": "string"}, "max_chars": {"type": "integer", "minimum": 1000, "maximum": 60000, "default": 30000}}, ["url"]),
             lambda c, a: browser_search.read(c, a["url"], a.get("max_chars", 30000))),
        Tool("ext_web_rag", "Search with local Chrome, read result pages, and retrieve relevant Chinese/English BM25 chunks with citations for you to answer from. No API key required. Opens temporary tabs. save_sources=true also persists page text in the legacy local knowledge library; default false.",
             _schema({"query": {"type": "string", "minLength": 1}, "result_count": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3}, "max_chunks": {"type": "integer", "minimum": 1, "maximum": 12, "default": 6}, "save_sources": {"type": "boolean", "default": False}}, ["query"]),
             lambda c, a: browser_search.rag(c, a["query"], a.get("result_count", 3), a.get("max_chunks", 6), a.get("save_sources", False))),
        Tool("ext_archive_webpage", "Fetch a rendered webpage through the connected Chrome worker and archive source snapshot, extracted text, stable chunks, and discovered attachments. download_attachments=true saves common policy/document attachments on the same Mac. save_html=false by default.",
             _schema({"url": {"type": "string"}, "save_html": {"type": "boolean", "default": False}, "download_attachments": {"type": "boolean", "default": True}}, ["url"]),
             lambda c, a: browser_search.archive(c, a["url"], save_html=bool(a.get("save_html", False)), download_attachments=bool(a.get("download_attachments", True)))),
        Tool("web_archive_search", "Search the local versioned web archive using bilingual lexical/FTS5 retrieval. Does not access the network.",
             _schema({"query": {"type": "string", "minLength": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8}}, ["query"]),
             lambda c, a: web_archive.search(c, a["query"], int(a.get("limit", 8)))),
        Tool("web_archive_fetch", "Fetch one archived webpage version or stable chunk by document_id, version_id, or chunk_id. Does not access the network.",
             _schema({"document_id": {"type": "string"}, "version_id": {"type": "string"}, "chunk_id": {"type": "string"}, "max_chars": {"type": "integer", "minimum": 100, "maximum": 100000, "default": 12000}}),
             lambda c, a: web_archive.fetch(c, document_id=a.get("document_id"), version_id=a.get("version_id"), chunk_id=a.get("chunk_id"), max_chars=int(a.get("max_chars", 12000)))),
        Tool("web_archive_versions", "List extraction versions for a document_id, newest first. Does not access the network.",
             _schema({"document_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}, ["document_id"]),
             lambda c, a: web_archive.versions(c, a["document_id"], int(a.get("limit", 20)))),
        Tool("web_archive_snapshots", "List source snapshots for a document_id and the extraction versions linked to each snapshot. Use this to distinguish source-page changes from extractor changes.",
             _schema({"document_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}, ["document_id"]),
             lambda c, a: web_archive.snapshots(c, a["document_id"], int(a.get("limit", 20)))),
        Tool("web_archive_attachments", "List discovered/downloaded attachments for a source snapshot, or for a document's current snapshot. Does not access the network.",
             _schema({"snapshot_id": {"type": "string"}, "document_id": {"type": "string"}}),
             lambda c, a: web_archive.list_attachments(c, snapshot_id=a.get("snapshot_id"), document_id=a.get("document_id"))),
        Tool(
            "local_list_files",
            "List files in an allowed local directory.",
            _schema({"path": {"type": "string", "default": "."}, "max_entries": {"type": "integer", "default": 200}}),
            lambda c, a: local_ops.list_files(c, a.get("path", "."), int(a.get("max_entries", 200))),
        ),
        Tool(
            "local_read_text",
            "Read a UTF-8 text file from an allowed local path. A complete, non-truncated, lossless and unredacted read also returns a full_replace_token that authorizes whole-file replacement of exactly that observed version.",
            _schema({"path": {"type": "string"}, "max_chars": {"type": "integer"}}, ["path"]),
            lambda c, a: local_ops.read_text(c, a["path"], a.get("max_chars")),
        ),
        Tool(
            "local_expose_file",
            "Expose one allowed local file as an MCP resource_link so the client can read the original file bytes through resources/read. Use this for PDF, Word, Excel, images, archives, or any other file that should be analyzed directly by the client/model.",
            _schema({"path": {"type": "string"}}, ["path"]),
            _local_expose_file,
        ),
        Tool(
            "local_write_file",
            "Transactionally create or replace a UTF-8 file under MCP_ALLOWED_ROOTS. Replacing an existing file requires both expected_sha256 and full_replace_token from a complete local_read_text result; truncated/partial reads cannot authorize whole-file replacement. Existing contents are recovery-pinned in Git when possible and the final replacement is atomic.",
            _schema(
                {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean", "default": False},
                    "expected_sha256": {"type": "string"},
                    "full_replace_token": {"type": "string"},
                    "allow_large_reduction": {"type": "boolean", "default": False},
                },
                ["path", "content"],
            ),
            lambda c, a: local_ops.write_file(
                c,
                a["path"],
                a["content"],
                bool(a.get("overwrite", False)),
                a.get("expected_sha256"),
                a.get("full_replace_token"),
                bool(a.get("allow_large_reduction", False)),
            ),
        ),
        Tool(
            "local_apply_patch",
            "Transactionally replace one exact text block in an allowed file and return a unified diff. Pass expected_sha256 from local_read_text for compare-and-swap protection.",
            _schema(
                {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "expected_sha256": {"type": "string"},
                },
                ["path", "old", "new"],
            ),
            lambda c, a: local_ops.apply_patch(
                c,
                a["path"],
                a["old"],
                a["new"],
                a.get("expected_sha256"),
            ),
        ),
        Tool(
            "local_start_job",
            "Start a durable local shell job exactly once for one operation_id and request fingerprint. Replaying the same operation_id with the same request returns the existing job without spawning again; a different request returns idempotency_conflict.",
            _schema(
                {
                    "operation_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 86400, "default": 900},
                },
                ["operation_id", "command"],
            ),
            lambda c, a: local_ops.start_job(
                c,
                a["operation_id"],
                a["command"],
                a.get("cwd"),
                int(a.get("timeout_sec", 900)),
            ),
        ),
        Tool(
            "local_job_status",
            "Observe durable local job state and process liveness. Observation-only: never starts, retries, cancels, or repairs a job.",
            _schema({"job_id": {"type": "string"}}, ["job_id"]),
            lambda c, a: local_ops.job_status(c, a["job_id"]),
        ),
        Tool(
            "local_job_logs",
            "Read retained stdout/stderr from explicit byte cursors. Observation-only and safe to replay.",
            _schema(
                {
                    "job_id": {"type": "string"},
                    "stdout_offset": {"type": "integer", "minimum": 0, "default": 0},
                    "stderr_offset": {"type": "integer", "minimum": 0, "default": 0},
                    "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1000000, "default": 16384},
                },
                ["job_id"],
            ),
            lambda c, a: local_ops.job_logs(
                c,
                a["job_id"],
                int(a.get("stdout_offset", 0)),
                int(a.get("stderr_offset", 0)),
                int(a.get("max_bytes", 16384)),
            ),
        ),
        Tool(
            "local_list_jobs",
            "List durable local jobs from persisted metadata without starting or changing them.",
            _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}}),
            lambda c, a: local_ops.list_jobs(c, int(a.get("limit", 50))),
        ),
        Tool(
            "local_cancel_job",
            "Explicitly cancel one durable local job. Repeated cancellation of an already-terminal job is a no-op.",
            _schema(
                {
                    "job_id": {"type": "string"},
                    "grace_sec": {"type": "number", "minimum": 0, "maximum": 10, "default": 2},
                },
                ["job_id"],
            ),
            lambda c, a: local_ops.cancel_job(
                c,
                a["job_id"],
                float(a.get("grace_sec", 2)),
            ),
        ),
        Tool(
            "local_run_command",
            "Run a non-dangerous shell command synchronously in an allowed cwd and record it in the local execution log. Prefer local_start_job for work that may outlive the tool-call timeout.",
            _schema({"command": {"type": "string"}, "cwd": {"type": "string"}, "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 30, "default": 30}}, ["command"]),
            lambda c, a: local_ops.run_command(c, a["command"], a.get("cwd"), int(a.get("timeout_sec", 30))),
        ),
        Tool(
            "local_command_log_tail",
            "Read recent background shell execution logs written by local_run_command.",
            _schema({"limit": {"type": "integer", "default": 20}}),
            lambda c, a: local_ops.tail_command_log(c, int(a.get("limit", 20))),
        ),
        Tool("local_git_status", "Run git status in an allowed repo.", _schema({"cwd": {"type": "string"}}, ["cwd"]), lambda c, a: local_ops.git_status(c, a["cwd"])),
        Tool(
            "local_git_diff",
            "Run git diff in an allowed repo.",
            _schema({"cwd": {"type": "string"}, "staged": {"type": "boolean", "default": False}, "max_chars": {"type": "integer"}}, ["cwd"]),
            lambda c, a: local_ops.git_diff(c, a["cwd"], bool(a.get("staged", False)), a.get("max_chars")),
        ),
        Tool(
            "local_git_log",
            "Run git log --oneline in an allowed repo.",
            _schema({"cwd": {"type": "string"}, "limit": {"type": "integer", "default": 20}}, ["cwd"]),
            lambda c, a: local_ops.git_log(c, a["cwd"], int(a.get("limit", 20))),
        ),
        Tool(
            "local_git_show",
            "Run git show for a revision in an allowed repo.",
            _schema({"cwd": {"type": "string"}, "rev": {"type": "string", "default": "HEAD"}, "max_chars": {"type": "integer"}}, ["cwd"]),
            lambda c, a: local_ops.git_show(c, a["cwd"], a.get("rev", "HEAD"), a.get("max_chars")),
        ),
        Tool(
            "terminal_list_supported_apps",
            "List all macOS apps supported by co-te, including read/write capability flags.",
            _schema({}),
            lambda c, a: terminal_ops.list_supported_apps(c),
        ),
        Tool(
            "chrome_list_tabs",
            "List titles and URLs for open Google Chrome tabs on this Mac. Does not read page body text.",
            _schema({"max_tabs": {"type": "integer", "default": 80}}),
            lambda c, a: chrome_ops.list_tabs(c, int(a.get("max_tabs", 80))),
        ),
        Tool(
            "chrome_get_active_tab_context",
            "Read the front Google Chrome tab title, URL, metadata, selection, and visible page text.",
            _schema(
                {
                    "max_chars": {"type": "integer", "default": 12000},
                    "include_text": {"type": "boolean", "default": True},
                    "include_selection": {"type": "boolean", "default": True},
                }
            ),
            lambda c, a: chrome_ops.get_active_tab_context(
                c,
                int(a.get("max_chars", 12000)),
                bool(a.get("include_text", True)),
                bool(a.get("include_selection", True)),
            ),
        ),
        Tool(
            "browser_list_tabs",
            "[AppleScript/Chrome Apple Events fallback, read-only] List open Chrome tab titles and URLs. Use ext_list_tabs when the MCP4ChatGPT Chrome extension is connected.",
            _schema({"max_tabs": {"type": "integer", "default": 80}}),
            lambda c, a: chrome_ops.list_tabs(c, int(a.get("max_tabs", 80))),
        ),
        Tool(
            "browser_current_tab",
            "[AppleScript/Chrome Apple Events fallback, read-only] Return the front Chrome tab title, URL, metadata, and selected text. Use ext_get_active_tab when the Chrome extension is connected.",
            _schema({"max_chars": {"type": "integer", "default": 12000}}),
            lambda c, a: chrome_ops.get_active_tab_context(c, int(a.get("max_chars", 12000)), False, True),
        ),
        Tool(
            "browser_get_page_text",
            "[AppleScript/Chrome Apple Events fallback, read-only] Read visible body text from the front Chrome tab. Use ext_get_active_tab with include_text=true when the Chrome extension is connected.",
            _schema({"max_chars": {"type": "integer", "default": 12000}}),
            lambda c, a: chrome_ops.get_active_tab_context(c, int(a.get("max_chars", 12000)), True, False),
        ),
        Tool(
            "browser_get_selection",
            "[AppleScript/Chrome Apple Events fallback, read-only] Read selected text from the front Chrome tab. Use ext_get_active_tab with include_selection=true when the Chrome extension is connected.",
            _schema({"max_chars": {"type": "integer", "default": 12000}}),
            lambda c, a: chrome_ops.get_active_tab_context(c, int(a.get("max_chars", 12000)), False, True),
        ),
        Tool(
            "browser_get_links",
            "[AppleScript/Chrome Apple Events fallback, read-only] Read links from the front Chrome tab. Prefer ext_get_dom when the Chrome extension is connected.",
            _schema({"max_links": {"type": "integer", "default": 100}}),
            lambda c, a: chrome_ops.get_links(c, int(a.get("max_links", 100))),
        ),
        Tool(
            "terminal_get_app_context",
            "Compatibility alias for app_get_context. Read recent context from any co-te supported macOS app. The optional label is a safety check and must be a substring of the actual front-window title; omit it unless that title text is known.",
            _schema({"app": {"type": "string", "enum": CO_TE_APP_KEYS}, "max_chars": {"type": "integer"}, "redact_secrets": {"type": "boolean"}, "label": {"type": "string", "description": "Optional substring of the actual front-window title. Do not use a task description here; omit unless the title is known."}}, ["app"]),
            lambda c, a: terminal_ops.get_app_context(c, a["app"], a.get("max_chars"), a.get("redact_secrets"), a.get("label")),
        ),
        Tool(
            "app_get_context",
            "Read selected text, focused editor content, window context, or terminal history from any co-te supported macOS app. The optional label is a safety check and must be a substring of the actual front-window title; omit it unless that title text is known.",
            _schema({"app": {"type": "string", "enum": CO_TE_APP_KEYS}, "max_chars": {"type": "integer"}, "redact_secrets": {"type": "boolean"}, "label": {"type": "string", "description": "Optional substring of the actual front-window title. Do not use a task description here; omit unless the title is known."}}, ["app"]),
            lambda c, a: terminal_ops.get_app_context(c, a["app"], a.get("max_chars"), a.get("redact_secrets"), a.get("label")),
        ),
        Tool(
            "app_write_text",
            "Paste text into any co-te supported macOS app through Accessibility. Use mode insert, replace_selection, or replace_all.",
            _schema(
                {
                    "app": {"type": "string", "enum": CO_TE_APP_KEYS},
                    "text": {"type": "string"},
                    "mode": {"type": "string", "enum": ["insert", "replace_selection", "replace_all"], "default": "insert"},
                    "press_return": {"type": "boolean", "default": False},
                    "sensitive": {"type": "boolean", "default": False},
                    "label": {"type": "string"},
                },
                ["app", "text"],
            ),
            lambda c, a: terminal_ops.write_app_text(
                c,
                a["app"],
                a["text"],
                a.get("mode", "insert"),
                bool(a.get("press_return", False)),
                bool(a.get("sensitive", False)),
                a.get("label"),
            ),
        ),
        Tool(
            "apple_notes_inspect_store",
            "Inspect the local Apple Notes SQLite store and record counts through co-te. Read-only.",
            _schema({}),
            lambda c, a: terminal_ops.inspect_apple_notes_store(c),
        ),
        Tool(
            "apple_notes_list_sqlite",
            "List Apple Notes from a read-only local SQLite snapshot through co-te.",
            _schema({"limit": {"type": "integer", "default": 50}, "folder": {"type": "string"}}),
            lambda c, a: terminal_ops.list_apple_notes_sqlite(c, int(a.get("limit", 50)), a.get("folder")),
        ),
        Tool(
            "apple_notes_read_sqlite",
            "Read one Apple Note by UUID or numeric primary key from a read-only SQLite snapshot through co-te.",
            _schema({"note_id": {"type": "string"}}, ["note_id"]),
            lambda c, a: terminal_ops.read_apple_note_sqlite(c, a["note_id"]),
        ),
        Tool(
            "apple_notes_search_sqlite",
            "Search Apple Notes title, snippet, and decoded body text from a read-only SQLite snapshot through co-te.",
            _schema({"query": {"type": "string"}, "limit": {"type": "integer", "default": 20}}, ["query"]),
            lambda c, a: terminal_ops.search_apple_notes_sqlite(c, a["query"], int(a.get("limit", 20))),
        ),
        Tool(
            "terminal_run_command",
            "Send one visible single-line shell command to the front Terminal.app/iTerm2/Termius tab and press Return. For multiple non-interactive commands, join them on one line with semicolons. Use terminal_send_input for prompts or interactive programs. The optional label must be a substring of the actual front-window title; omit it unless known.",
            _schema({"command": {"type": "string", "description": "One shell command with no newline or carriage-return characters. Join multiple commands with semicolons."}, "app": {"type": "string", "enum": CO_TE_TERMINAL_APP_KEYS, "default": "terminal"}, "label": {"type": "string", "description": "Optional substring of the actual front-window title. Do not use a task description here; omit unless the title is known."}}, ["command"]),
            lambda c, a: terminal_ops.run_command(c, a["command"], a.get("app", "terminal"), a.get("label")),
        ),
        Tool(
            "terminal_send_input",
            "Type or paste text into Terminal.app/iTerm2/Termius; set press_return=false to paste without executing.",
            _schema({"text": {"type": "string"}, "press_return": {"type": "boolean", "default": True}, "sensitive": {"type": "boolean", "default": False}, "app": {"type": "string", "enum": CO_TE_TERMINAL_APP_KEYS, "default": "terminal"}, "label": {"type": "string"}}, ["text"]),
            lambda c, a: terminal_ops.send_input(c, a["text"], bool(a.get("press_return", True)), bool(a.get("sensitive", False)), a.get("app", "terminal"), a.get("label")),
        ),
        Tool(
            "search_web",
            "Search current web content. Defaults to the connected local Chrome extension and its network/session context. backend=auto permits fallback to Brave/Firecrawl and reports the reason; backend=brave or firecrawl selects that API explicitly. deep_read uses the browser for browser-backed searches.",
            _schema(
                {
                    "query": {"type": "string", "description": "The web search query to run."},
                    "result_count": {
                        "type": "integer",
                        "default": 3,
                        "minimum": 1,
                        "maximum": 10,
                        "description": "Number of search results to return.",
                    },
                    "deep_read": {
                        "type": "boolean",
                        "default": False,
                        "description": "Fetch up to two top result pages using the selected search path.",
                    },
                    "backend": {"type": "string", "enum": ["browser", "auto", "brave", "firecrawl"], "default": "browser"},
                },
                ["query"],
            ),
            _search_web,
        ),
        Tool("web_scrape", "Scrape a web page via Firecrawl.", _schema({"url": {"type": "string"}, "formats": {"type": "array", "items": {"type": "string"}}}, ["url"]), lambda c, a: web_ops.scrape(c, a["url"], a.get("formats"))),
        Tool("web_crawl", "Crawl a website via Firecrawl.", _schema({"url": {"type": "string"}, "limit": {"type": "integer", "default": 10}, "max_depth": {"type": "integer", "default": 2}}, ["url"]), lambda c, a: web_ops.crawl(c, a["url"], int(a.get("limit", 10)), int(a.get("max_depth", 2)))),
        Tool("web_map", "Map URLs from a website via Firecrawl.", _schema({"url": {"type": "string"}, "limit": {"type": "integer", "default": 100}}, ["url"]), lambda c, a: web_ops.map_site(c, a["url"], int(a.get("limit", 100)))),
        Tool("web_extract", "Extract structured data from URLs via Firecrawl.", _schema({"urls": {"type": "array", "items": {"type": "string"}}, "prompt": {"type": "string"}, "schema": {"type": "object"}}, ["urls"]), lambda c, a: web_ops.extract(c, a["urls"], a.get("prompt"), a.get("schema"))),
        Tool("web_interact", "Interact with a web page via Firecrawl.", _schema({"url": {"type": "string"}, "prompt": {"type": "string"}, "actions": {"type": "array", "items": {"type": "object"}}}, ["url"]), lambda c, a: web_ops.interact(c, a["url"], a.get("prompt"), a.get("actions"))),
        Tool("web_add_to_knowledge", "Scrape a URL and add its markdown to the local knowledge store.", _schema({"url": {"type": "string"}, "title": {"type": "string"}}, ["url"]), _web_add_to_knowledge),
        Tool("knowledge_add_source", "Add a local file or supplied text to the knowledge store.", _schema({"path": {"type": "string"}, "title": {"type": "string"}, "text": {"type": "string"}, "url": {"type": "string"}, "metadata": {"type": "object"}}), lambda c, a: knowledge_ops.add_source(c, path=a.get("path"), title=a.get("title"), text=a.get("text"), url=a.get("url"), metadata=a.get("metadata"))),
        Tool("knowledge_list_sources", "List knowledge sources.", _schema({}), lambda c, a: knowledge_ops.list_sources(c)),
        Tool("knowledge_search", "Search source-grounded local knowledge chunks.", _schema({"query": {"type": "string"}, "limit": {"type": "integer", "default": 8}}, ["query"]), lambda c, a: knowledge_ops.search(c, a["query"], int(a.get("limit", 8)))),
        Tool("knowledge_fetch", "Fetch a full source or one source chunk.", _schema({"source_id": {"type": "string"}, "chunk_id": {"type": "string"}, "max_chars": {"type": "integer", "default": 12000}}, ["source_id"]), lambda c, a: knowledge_ops.fetch(c, a["source_id"], a.get("chunk_id"), int(a.get("max_chars", 12000)))),
        Tool("knowledge_summarize", "Generate a source-grounded Markdown summary.", _schema({"source_id": {"type": "string"}, "max_points": {"type": "integer", "default": 8}}, ["source_id"]), lambda c, a: knowledge_ops.summarize(c, a["source_id"], int(a.get("max_points", 8)))),
        Tool("knowledge_study_guide", "Generate a simple study guide from a source.", _schema({"source_id": {"type": "string"}}, ["source_id"]), lambda c, a: knowledge_ops.study_guide(c, a["source_id"])),
        Tool("knowledge_quiz", "Generate quiz items from a source.", _schema({"source_id": {"type": "string"}, "count": {"type": "integer", "default": 5}}, ["source_id"]), lambda c, a: knowledge_ops.quiz(c, a["source_id"], int(a.get("count", 5)))),
        Tool("knowledge_flashcards", "Generate flashcards from a source.", _schema({"source_id": {"type": "string"}, "count": {"type": "integer", "default": 10}}, ["source_id"]), lambda c, a: knowledge_ops.flashcards(c, a["source_id"], int(a.get("count", 10)))),
        # ------------------------------------------------------------------ #
        # Browser Extension Tools (ext_*)                                     #
        # Requires the MCP4ChatGPT Chrome extension to be installed and        #
        # connected. Use ext_connection_status to check before calling others. #
        # ------------------------------------------------------------------ #
        Tool(
            "ext_connection_status",
            "Check whether the MCP4ChatGPT Chrome extension is connected to the bridge. Call this first before using any other ext_* tool.",
            _schema({}),
            lambda c, a: ext_ops.ext_connection_status(c),
        ),
        Tool(
            "ext_list_tabs",
            "List all open Chrome tabs (window ID, tab ID, URL, title, active status). Requires the MCP4ChatGPT Chrome extension.",
            _schema({"max_tabs": {"type": "integer", "default": 100}}),
            lambda c, a: ext_ops.ext_list_tabs(c, int(a.get("max_tabs", 100))),
        ),
        Tool(
            "ext_get_active_tab",
            "Read the front Chrome tab: title, URL, visible page text, selected text, and meta tags. Requires the MCP4ChatGPT Chrome extension.",
            _schema({
                "max_chars": {"type": "integer", "default": 12000},
                "include_text": {"type": "boolean", "default": True},
                "include_selection": {"type": "boolean", "default": True},
                "include_meta": {"type": "boolean", "default": True},
            }),
            lambda c, a: ext_ops.ext_get_active_tab(
                c,
                int(a.get("max_chars", 12000)),
                bool(a.get("include_text", True)),
                bool(a.get("include_selection", True)),
                bool(a.get("include_meta", True)),
            ),
        ),
        Tool(
            "ext_get_dom",
            "Get the outerHTML of a DOM element (default: body) from a Chrome tab. Requires the MCP4ChatGPT Chrome extension.",
            _schema({
                "tab_id": {"type": "integer"},
                "selector": {"type": "string", "default": "body"},
                "max_chars": {"type": "integer", "default": 50000},
            }),
            lambda c, a: ext_ops.ext_get_dom(
                c,
                a.get("tab_id"),
                str(a.get("selector", "body")),
                int(a.get("max_chars", 50000)),
            ),
        ),
        Tool(
            "ext_get_selection",
            "Get the currently selected text from a Chrome tab. Requires the MCP4ChatGPT Chrome extension.",
            _schema({"tab_id": {"type": "integer"}}),
            lambda c, a: ext_ops.ext_get_selection(c, a.get("tab_id")),
        ),
        Tool(
            "ext_screenshot",
            "Take a screenshot of a Chrome tab and save it as a PNG file. Returns the file path. Requires the MCP4ChatGPT Chrome extension.",
            _schema({
                "tab_id": {"type": "integer"},
                "save_to_file": {"type": "boolean", "default": True},
                "quality": {"type": "integer", "default": 80},
            }),
            lambda c, a: ext_ops.ext_screenshot(
                c,
                a.get("tab_id"),
                bool(a.get("save_to_file", True)),
                int(a.get("quality", 80)),
            ),
        ),
        Tool(
            "ext_navigate",
            "Navigate a Chrome tab to a URL, or open a new tab. When new_tab=true, reuse the returned tab_id in every follow-up browser tool; omitting tab_id targets whichever tab is currently active. Requires the MCP4ChatGPT Chrome extension.",
            _schema({
                "url": {"type": "string"},
                "tab_id": {
                    "type": "integer",
                    "description": "Target tab ID. For a new tab, use the tab_id returned by this tool in subsequent calls.",
                },
                "new_tab": {"type": "boolean", "default": False},
            }, ["url"]),
            lambda c, a: ext_ops.ext_navigate(
                c,
                str(a["url"]),
                a.get("tab_id"),
                bool(a.get("new_tab", False)),
            ),
        ),
        Tool(
            "ext_click_element",
            "Click a DOM element identified by a CSS selector in a Chrome tab. Requires the MCP4ChatGPT Chrome extension.",
            _schema({
                "selector": {"type": "string"},
                "tab_id": {"type": "integer"},
            }, ["selector"]),
            lambda c, a: ext_ops.ext_click_element(c, str(a["selector"]), a.get("tab_id")),
        ),
        Tool(
            "ext_fill_input",
            "Fill an input or textarea by CSS selector and optionally submit its form. Pass the same tab_id returned by ext_navigate so the operation cannot drift to another active tab. A synthetic Enter event on elements without a form is reported as attempted, not confirmed submitted. Requires the MCP4ChatGPT Chrome extension.",
            _schema({
                "selector": {"type": "string"},
                "value": {"type": "string"},
                "tab_id": {
                    "type": "integer",
                    "description": "Exact target tab ID, normally returned by ext_navigate or ext_list_tabs.",
                },
                "submit": {"type": "boolean", "default": False},
            }, ["selector", "value"]),
            lambda c, a: ext_ops.ext_fill_input(
                c,
                str(a["selector"]),
                str(a["value"]),
                a.get("tab_id"),
                bool(a.get("submit", False)),
            ),
        ),
        Tool(
            "ext_run_js",
            "Execute JavaScript in a Chrome tab and return the result. Requires 'Allow JS execution' to be enabled in the extension popup. Requires the MCP4ChatGPT Chrome extension.",
            _schema({
                "code": {"type": "string"},
                "tab_id": {"type": "integer"},
                "max_chars": {"type": "integer", "default": 10000},
                "timeout_sec": {"type": "integer", "default": 30, "minimum": 1, "maximum": 120},
            }, ["code"]),
            lambda c, a: ext_ops.ext_run_js(
                c,
                str(a["code"]),
                a.get("tab_id"),
                int(a.get("max_chars", 10000)),
                int(a.get("timeout_sec", 30)),
            ),
        ),
        Tool(
            "ext_listen_changes",
            "Listen for page navigation and DOM changes in a Chrome tab for up to duration_sec seconds. Returns a list of captured events. Requires the MCP4ChatGPT Chrome extension.",
            _schema({
                "duration_sec": {"type": "integer", "default": 30},
                "tab_id": {"type": "integer"},
            }),
            lambda c, a: ext_ops.ext_listen_changes(
                c,
                int(a.get("duration_sec", 30)),
                a.get("tab_id"),
            ),
        ),
    ]
    if computer_mode == "interact":
        return tools
    if computer_mode == "observe":
        return [tool for tool in tools if tool.name not in {"computer_click", "computer_press_key", "computer_type_text", "computer_type_keyboard", "computer_launch_app", "computer_activate_window", "computer_pointer_move", "computer_pointer_click", "computer_pointer_drag", "computer_pointer_scroll"}]
    return [tool for tool in tools if not tool.name.startswith("computer_")]


def _ext_async_jobs_enabled() -> bool:
    return os.environ.get("MCP_EXT_ASYNC_JOBS_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _build_ext_job_tools() -> list[Tool]:
    return [
        Tool(
            "ext_start_js_job",
            "Start an asynchronous checkpointed JavaScript job on one Chrome tab. The code must be a JavaScript function expression accepting (checkpoint, batchIndex) and returning a JSON object with done plus optional checkpoint, records, progress, counters, next_delay_ms, and final result. Each batch is bounded so one MCP request does not remain open for the full job lifetime.",
            _schema({
                "code": {"type": "string"},
                "tab_id": {"type": "integer"},
                "initial_checkpoint": {"description": "Any JSON-serializable checkpoint passed to the first batch."},
                "max_batches": {"type": "integer", "default": 1000, "minimum": 1, "maximum": 10000},
                "batch_timeout_sec": {"type": "number", "default": 20, "minimum": 1, "maximum": 25},
            }, ["code"]),
            lambda c, a: ext_ops.ext_start_js_job(
                c,
                str(a["code"]),
                a.get("tab_id"),
                a.get("initial_checkpoint"),
                int(a.get("max_batches", 1000)),
                float(a.get("batch_timeout_sec", 20)),
            ),
        ),
        Tool(
            "ext_get_job",
            "Get status for an asynchronous Extension JavaScript job without returning its potentially large result payload.",
            _schema({"job_id": {"type": "string"}}, ["job_id"]),
            lambda c, a: ext_ops.ext_get_job(c, str(a["job_id"])),
        ),
        Tool(
            "ext_get_job_result",
            "Read a bounded chunk of records from an asynchronous Extension JavaScript job and return artifact metadata for larger results.",
            _schema({
                "job_id": {"type": "string"},
                "cursor": {"type": "integer", "default": 0, "minimum": 0},
                "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 200},
                "max_chars": {"type": "integer", "default": 20000, "minimum": 500, "maximum": 100000},
            }, ["job_id"]),
            lambda c, a: ext_ops.ext_get_job_result(
                c,
                str(a["job_id"]),
                int(a.get("cursor", 0)),
                int(a.get("limit", 100)),
                int(a.get("max_chars", 20000)),
            ),
        ),
        Tool(
            "ext_cancel_job",
            "Request cancellation of a queued or running asynchronous Extension JavaScript job. Cancellation takes effect no later than the end of the current bounded batch.",
            _schema({"job_id": {"type": "string"}}, ["job_id"]),
            lambda c, a: ext_ops.ext_cancel_job(c, str(a["job_id"])),
        ),
    ]


class ToolRegistry:
    """工具目录及统一调用入口。

    注册表在启动时把工具列表索引为 ``name -> Tool``，使 ``tools/list`` 与
    ``tools/call`` 使用同一份定义，避免"声明存在但无法执行"或反向漂移。
    所有调用都在这里记录成功/失败审计事件；异常不被吞掉，而是交给传输层转换为
    JSON-RPC error，保证客户端能区分正常工具结果与执行失败。
    """

    def __init__(self, config: Config, audit: AuditLogger, *, downstream_manager: Any | None = None):
        self.config = config
        self.audit = audit
        self._downstream_manager = downstream_manager
        self._catalog_lock = threading.RLock()
        listed_tools = build_tools(computer_mode=getattr(config, "computer_mode", "off"))
        if _ext_async_jobs_enabled():
            listed_tools = [*listed_tools, *_build_ext_job_tools()]
        self.tools = {tool.name: tool for tool in listed_tools}
        self.tools.update(
            {
                "web_search": Tool(
                    "web_search",
                    "Compatibility alias for Firecrawl search.",
                    _schema({"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}}, ["query"]),
                    lambda c, a: web_ops.search(c, a["query"], int(a.get("limit", 5))),
                ),
                "web_brave_search": Tool(
                    "web_brave_search",
                    "Compatibility alias for Brave search.",
                    _schema({"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}}, ["query"]),
                    lambda c, a: web_ops.brave_search(c, a["query"], int(a.get("limit", 5))),
                ),
                "web_search_auto": Tool(
                    "web_search_auto",
                    "Compatibility alias for automatic web search.",
                    _schema(
                        {
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "default": 3},
                            "deep_read": {"type": "boolean", "default": False},
                            "fetch_limit": {"type": "integer", "default": 1},
                        },
                        ["query"],
                    ),
                    _web_search_auto_compat,
                ),
                "web_combined_search": Tool(
                    "web_combined_search",
                    "Compatibility alias for advanced combined search.",
                    _schema(
                        {
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "default": 5},
                            "engine": {
                                "type": "string",
                                "enum": ["brave", "firecrawl", "auto"],
                                "default": "brave",
                            },
                            "fetch_content": {"type": "boolean", "default": False},
                            "fetch_limit": {"type": "integer", "default": 3},
                        },
                        ["query"],
                    ),
                    lambda c, a: web_ops.combined_search(
                        c,
                        a["query"],
                        int(a.get("limit", 5)),
                        engine=a.get("engine", "brave"),
                        fetch_content=bool(a.get("fetch_content", False)),
                        fetch_limit=int(a.get("fetch_limit", 3)),
                    ),
                ),
            }
        )
        self._listed_tool_names = tuple(tool.name for tool in listed_tools)

        # Append downstream tool names (if any downstream manager is configured)
        self._downstream_tool_names: tuple[str, ...] = ()
        self._downstream_tool_channels: dict[str, str] = {}
        if self._downstream_manager is not None:
            ds_tools = self._downstream_manager.get_tools()
            for ds_tool in ds_tools:
                self.tools[ds_tool.namespaced_name] = self._make_downstream_tool(ds_tool)
                self._downstream_tool_channels[ds_tool.namespaced_name] = ds_tool.downstream_id
            self._downstream_tool_names = tuple(t.namespaced_name for t in ds_tools)

        self._catalog_names = self._listed_tool_names + self._downstream_tool_names
        self._catalog_name_set = frozenset(self._catalog_names)
        self._catalog_sources = {
            name: (f"downstream:{self._downstream_tool_channels[name]}" if name in self._downstream_tool_channels else "local")
            for name in self._catalog_names
        }
        self._rebuild_catalog_entries()
        self.catalog_version = self._catalog_hash()
        self.tools.update({
            "capability_search": Tool(
                "capability_search",
                "Search available MCP capabilities by name, description, source, backend, category, and maintained keywords.",
                _schema({
                    "query": {"type": "string", "minLength": 1, "maxLength": 256},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                    "source": {"type": "string", "minLength": 1, "maxLength": 256},
                    "backend": {"type": "string", "minLength": 1, "maxLength": 256},
                    "category": {"type": "string", "minLength": 1, "maxLength": 256},
                }, ["query"]),
                lambda _c, a: self._capability_search(a),
            ),
            "capability_get": Tool(
                "capability_get",
                "Get the complete definition, schema, metadata, examples, and evidenced availability for one capability.",
                _schema({"name": {"type": "string", "minLength": 1}}, ["name"]),
                lambda _c, a: self._capability_get(a),
            ),
            "capability_list": Tool(
                "capability_list",
                "List compact capability catalog entries for inventory/debugging. Use search/get/call for normal task execution.",
                _schema({
                    "source": {"type": "string", "minLength": 1, "maxLength": 256},
                    "backend": {"type": "string", "minLength": 1, "maxLength": 256},
                    "category": {"type": "string", "minLength": 1, "maxLength": 256},
                    "cursor": {"type": "string", "minLength": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                }),
                lambda _c, a: self._capability_list(a),
            ),
            "capability_call": Tool(
                "capability_call",
                "Validate arguments and call one named capability through its existing handler.",
                _schema({"name": {"type": "string", "minLength": 1},
                         "arguments": {"type": "object"},
                         "expected_revision": {"type": "string", "minLength": 1}}, ["name", "arguments"]),
                lambda _c, _a: (_ for _ in ()).throw(ValueError("capability_call_recursive")),
                annotations_override={"readOnlyHint": False, "destructiveHint": True,
                                      "idempotentHint": False, "openWorldHint": True},
            ),
        })
        self._capability_tool_names = ("capability_search", "capability_get", "capability_list", "capability_call")
        self._listed_capability_tool_names = ("capability_search", "capability_get", "capability_call")
        exposure = getattr(config, "tool_exposure", "full")
        if exposure not in {"full", "compact"}:
            raise ValueError("MCP_TOOL_EXPOSURE must be full or compact")
        self.tool_exposure = exposure
        if exposure == "compact":
            visible = tuple(name for name in self._catalog_names if name == "server_info")
            self._all_listed_names = visible + self._listed_capability_tool_names
        else:
            self._all_listed_names = self._catalog_names + self._listed_capability_tool_names
        self._listed_tool_name_set = frozenset(self._all_listed_names)
        self.toolset_hash = self._definition_hash(self._all_listed_names, auth_required=False, exposure=exposure)
        for name in self._listed_tool_names:
            self.tools[f"MCP4ChatGPT.{name}"] = self.tools[name]
        self._validate_catalog_examples()

    def _definition_hash(self, names: tuple[str, ...], *, auth_required: bool, exposure: str = "") -> str:
        definitions = [self.tools[name].definition(auth_required=auth_required) for name in sorted(names)]
        encoded = json.dumps({"exposure": exposure, "tools": definitions}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _capability_revision(tool: Tool) -> str:
        payload = {"definition": tool.definition(auth_required=False),
                   "backend_id": tool.backend_id,
                   "backend_instance_id": tool.backend_instance_id,
                   "original_name": tool.original_name}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode()).hexdigest()

    def bind_capabilities(self, names: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
        """Snapshot only selected canonical tools; unrelated catalog edits remain valid.

        This is an identity constraint, not an authorization grant. The host
        must supply its own allowlist; handler checks still run on every call.
        """
        self.refresh_catalog()
        with self._catalog_lock:
            if any(name not in self._catalog_name_set for name in names):
                raise ValueError("capability_not_found")
            return tuple((name, self._capability_revision(self.tools[name])) for name in sorted(set(names)))

    def _rebuild_catalog_entries(self) -> None:
        entries = tuple(
            build_entry(
                name,
                self.tools[name].description,
                self._catalog_sources[name],
                self.tools[name].backend_id,
                legacy_alias=name in self._listed_tool_names,
            )
            for name in self._catalog_names
        )
        self._catalog_entries = entries
        self._catalog_entry_by_name = {entry.name: entry for entry in entries}

    def _validate_catalog_examples(self) -> None:
        """Validate maintained examples against input schemas without executing them."""
        names = (*self._catalog_names, *self._capability_tool_names)
        for name in names:
            source = self._catalog_sources.get(name, "local")
            for example in examples_for(name, source):
                self._validate_capability_arguments(self.tools[name], example)

    def _catalog_hash(self) -> str:
        entries = [{
            "source": self._catalog_sources[name],
            "revision": self._capability_revision(self.tools[name]),
            "tool": self.tools[name].definition(auth_required=False),
            "metadata": {
                **metadata_payload(self._catalog_entry_by_name[name]),
                "keywords": list(self._catalog_entry_by_name[name].keywords),
            },
        } for name in sorted(self._catalog_names)]
        encoded = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _make_downstream_tool(self, ds_tool: Any) -> Tool:
        return Tool(
            name=ds_tool.namespaced_name,
            description=ds_tool.description,
            input_schema=deepcopy(ds_tool.input_schema),
            handler=self._make_downstream_handler(ds_tool.namespaced_name, deepcopy(ds_tool)),
            annotations_override={
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": True,
                **deepcopy(ds_tool.annotations or {}),
            },
            output_schema=deepcopy(ds_tool.output_schema),
            backend_id=ds_tool.downstream_id,
            backend_instance_id=ds_tool.backend_instance_id or None,
            original_name=ds_tool.original_name,
        )

    def _rebuild_catalog_metadata(self) -> None:
        self._catalog_names = self._listed_tool_names + self._downstream_tool_names
        self._catalog_name_set = frozenset(self._catalog_names)
        self._catalog_sources = {
            name: (
                f"downstream:{self._downstream_tool_channels[name]}"
                if name in self._downstream_tool_channels
                else "local"
            )
            for name in self._catalog_names
        }
        self._rebuild_catalog_entries()
        self.catalog_version = self._catalog_hash()
        if self.tool_exposure == "compact":
            visible = tuple(name for name in self._catalog_names if name == "server_info")
            self._all_listed_names = visible + self._listed_capability_tool_names
        else:
            self._all_listed_names = self._catalog_names + self._listed_capability_tool_names
        self._listed_tool_name_set = frozenset(self._all_listed_names)
        self.toolset_hash = self._definition_hash(
            self._all_listed_names,
            auth_required=False,
            exposure=self.tool_exposure,
        )

    def refresh_catalog(self) -> bool:
        """Synchronize the capability catalog with the manager's current downstream tools."""
        if self._downstream_manager is None:
            return False
        with self._catalog_lock:
            try:
                ds_tools = self._downstream_manager.get_tools()
            except Exception as exc:
                self.audit.log("capability_catalog_refresh", ok=False, error=str(exc))
                return False

            new_tools = {
                ds_tool.namespaced_name: self._make_downstream_tool(ds_tool)
                for ds_tool in ds_tools
            }
            new_names = tuple(new_tools)
            new_channels = {
                ds_tool.namespaced_name: ds_tool.downstream_id
                for ds_tool in ds_tools
            }

            changed = (
                new_names != self._downstream_tool_names
                or new_channels != self._downstream_tool_channels
            )
            if not changed:
                for name, tool in new_tools.items():
                    current = self.tools.get(name)
                    if (
                        current is None
                        or self._capability_revision(current) != self._capability_revision(tool)
                    ):
                        changed = True
                        break
            if not changed:
                return False

            old_version = self.catalog_version
            for name in self._downstream_tool_names:
                self.tools.pop(name, None)
            self.tools.update(new_tools)
            self._downstream_tool_names = new_names
            self._downstream_tool_channels = new_channels
            self._rebuild_catalog_metadata()
            self.audit.log(
                "capability_catalog_refresh",
                ok=True,
                old_catalog_version=old_version,
                catalog_version=self.catalog_version,
                capability_count=len(self._catalog_names),
                listed_count=len(self._all_listed_names),
            )
            return True

    def _capability_search(self, args: dict[str, Any]) -> dict[str, Any]:
        self.refresh_catalog()
        query = args.get("query")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 256:
            raise ValueError("capability_query_invalid")
        limit = args.get("limit", 10)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("capability_limit_invalid")
        filters = normalize_filters(args)

        with self._catalog_lock:
            version = self.catalog_version
            ranked = rank_entries(self._catalog_entries, query, filters)
            matches = [{
                "name": entry.name,
                "description": entry.description[:240],
                "source": entry.source,
                "annotations": self.tools[entry.name].definition(auth_required=False).get("annotations", {}),
            } for entry in ranked[:limit]]
        return {"catalog_version": version, "matches": matches, "truncated": len(ranked) > limit}

    def _availability_for(self, backend_id: str | None) -> dict[str, Any]:
        # Registration is not execution evidence. Local/browser dependencies are
        # deliberately unknown until an explicit health contract exists.
        if backend_id is None or not isinstance(self._downstream_manager, DownstreamMCPManager):
            return {"status": "unknown", "evidence": "not_checked"}
        try:
            statuses = self._downstream_manager.get_status().get("downstream", [])
        except Exception:
            return {"status": "unknown", "evidence": "manager_status_unavailable"}
        record = next((item for item in statuses if item.get("id") == backend_id), None)
        if not isinstance(record, dict):
            return {"status": "unknown", "evidence": "manager_status_missing"}
        return {
            "status": record.get("state", "unknown"),
            "evidence": "downstream_manager",
        }

    def _capability_get(self, args: dict[str, Any]) -> dict[str, Any]:
        self.refresh_catalog()
        name = args.get("name")
        with self._catalog_lock:
            if not isinstance(name, str) or name not in self._catalog_name_set:
                raise ValueError("capability_not_found")
            tool = self.tools[name]
            entry = self._catalog_entry_by_name[name]
            result = {
                "catalog_version": self.catalog_version,
                "tool": tool.definition(auth_required=True),
                "canonical_name": name,
                "source": entry.source,
                "backend_id": tool.backend_id,
                "backend_instance_id": tool.backend_instance_id,
                "capability_revision": self._capability_revision(tool),
                **metadata_payload(entry),
            }
        result["availability"] = self._availability_for(tool.backend_id)
        return result

    def _capability_list(self, args: dict[str, Any]) -> dict[str, Any]:
        self.refresh_catalog()
        limit = args.get("limit", 50)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("capability_limit_invalid")
        filters = normalize_filters(args)
        position = 0
        cursor = args.get("cursor")

        with self._catalog_lock:
            version = self.catalog_version
            if cursor is not None:
                decoded = decode_cursor(cursor)
                if decoded["catalog_version"] != version:
                    raise ValueError("capability_catalog_changed")
                cursor_filters = decoded["filters"]
                for key in ("source", "backend", "category"):
                    if key in args and filters[key] != cursor_filters[key]:
                        raise ValueError("capability_cursor_mismatch")
                filters = cursor_filters
                position = decoded["position"]

            entries = filtered_entries(self._catalog_entries, filters)
            if position > len(entries):
                raise ValueError("capability_cursor_invalid")
            page = entries[position:position + limit]
            next_position = position + len(page)
            next_cursor = (
                encode_cursor(version, filters, next_position)
                if next_position < len(entries)
                else None
            )
            items = [{
                "name": entry.name,
                "description": entry.description[:240],
                "source": entry.source,
                "backend_id": entry.backend_id,
                "category": entry.category,
                "deprecated": entry.deprecated,
            } for entry in page]
        return {
            "catalog_version": version,
            "items": items,
            "next_cursor": next_cursor,
            "has_more": next_cursor is not None,
        }

    @staticmethod
    def _check_schema_references(schema: Any) -> None:
        # Walk schema positions, not arbitrary instance data (examples/defaults
        # and the keys of properties may legitimately contain "$ref").
        if not isinstance(schema, dict):
            return
        if "$schema" in schema and validator_for(schema, default=None) is None:
            raise ValueError("capability_schema_dialect_unsupported")
        for key in ("$ref", "$dynamicRef", "$recursiveRef"):
            if key in schema:
                ref = schema[key]
                if not isinstance(ref, str) or not ref.startswith("#"):
                    raise ValueError("capability_external_ref_unsupported")
        children = []
        for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas", "dependencies"):
            value = schema.get(key)
            if isinstance(value, dict):
                children.extend(v for v in value.values() if isinstance(v, (dict, bool)))
        for key in ("additionalProperties", "additionalItems", "unevaluatedProperties", "unevaluatedItems",
                    "contains", "propertyNames", "not", "if", "then", "else", "contentSchema", "items", "extends"):
            value = schema.get(key)
            children.extend(value if isinstance(value, list) else [value])
        for key in ("allOf", "anyOf", "oneOf", "prefixItems", "type", "disallow"):
            value = schema.get(key)
            if isinstance(value, list):
                children.extend(value)
        for child in children:
            ToolRegistry._check_schema_references(child)

    @staticmethod
    def _deny_schema_retrieval(uri: str):
        raise NoSuchResource(ref=uri)

    def _validate_capability_arguments(self, tool: Tool, arguments: Any) -> None:
        if not isinstance(arguments, dict):
            raise ValueError("capability_arguments_invalid")
        schema = tool.input_schema
        self._check_schema_references(schema)
        try:
            validator_type = validator_for(schema, default=Draft202012Validator)
            validator_type.check_schema(schema)
            validator_type(schema, registry=Registry(retrieve=self._deny_schema_retrieval)).validate(arguments)
        except SchemaError as exc:
            raise ValueError("capability_schema_invalid") from exc
        except ValidationError as exc:
            raise ValueError(f"capability_arguments_invalid: {exc.message[:240]}") from exc
        except Exception as exc:
            raise ValueError("capability_schema_invalid") from exc

    def _make_downstream_handler(self, namespaced_name: str, expected_tool: Any) -> ToolHandler:
        """Create a handler that preserves downstream MCP content blocks verbatim."""
        def _handler(config: Config, arguments: dict[str, Any]) -> Any:
            if isinstance(self._downstream_manager, DownstreamMCPManager):
                result = self._downstream_manager.call_tool(
                    namespaced_name, arguments, expected_tool=expected_tool)
            else:
                result = self._downstream_manager.call_tool(namespaced_name, arguments)
            if isinstance(result, dict):
                return RawMCPToolResult(result)
            return result
        return _handler

    def list_tools_snapshot(self, *, auth_required: bool) -> tuple[dict[str, Any], dict[str, Any]]:
        self.refresh_catalog()
        with self._catalog_lock:
            result = {"tools": [self.tools[name].definition(auth_required=auth_required)
                                for name in self._all_listed_names]}
            return result, {"tool_count": len(result["tools"]), "toolset_hash": self.toolset_hash}

    def list_tools(self, *, auth_required: bool) -> dict[str, Any]:
        return self.list_tools_snapshot(auth_required=auth_required)[0]

    def list_tool_resources(self) -> dict[str, Any]:
        self.refresh_catalog()
        with self._catalog_lock:
            tool_resources = [
                {
                    "uri": f"mcp4chatgpt://tools/{name}",
                    "name": f"MCP4ChatGPT.{name}",
                    "title": self.tools[name].definition(auth_required=False)["title"],
                    "description": self.tools[name].description,
                    "mimeType": "application/json",
                }
                for name in sorted(self._catalog_names)
            ]
        return {"resources": tool_resources + file_resources.list_resources()}

    def read_tool_resource(self, uri: str, *, auth_required: bool) -> dict[str, Any]:
        if file_resources.is_file_resource_uri(uri):
            return file_resources.read_resource(self.config, uri)
        self.refresh_catalog()
        prefix = "mcp4chatgpt://tools/"
        if uri.startswith(prefix):
            name = uri.removeprefix(prefix)
        elif uri.startswith("MCP4ChatGPT."):
            name = uri.removeprefix("MCP4ChatGPT.")
        else:
            raise ValueError(f"Unknown resource: {uri}")
        with self._catalog_lock:
            if name not in self._catalog_name_set:
                raise ValueError(f"Unknown tool resource: {uri}")
            definition = self.tools[name].definition(auth_required=auth_required)
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(definition, ensure_ascii=False, indent=2),
                }
            ]
        }

    def _channel_for(self, name: str) -> str:
        # Caller holds _catalog_lock, so tool and audit identity are selected
        # from the same generation before validation or handler execution.
        return self._downstream_tool_channels.get(
            name, "extension" if name.startswith("ext_") else "native",
        )

    def call_tool(
        self, name: str, arguments: dict[str, Any], client_id: str = "",
        *, context: CallContext | None = None,
    ) -> dict[str, Any]:
        context = context or CallContext()
        target_name = name
        channel = "unresolved"
        invoked_via = "capability_call" if name == "capability_call" else None
        expected_revision = None
        revision = None
        try:
            self.refresh_catalog()
            with self._catalog_lock:
                version = self.catalog_version
                if context.expected_catalog_version is not None and context.expected_catalog_version != version:
                    raise ValueError("capability_catalog_changed")
                tool = self.tools.get(name)
                if tool is None:
                    raise ValueError(f"Unknown tool: {name}")
                if name == "capability_call":
                    self._validate_capability_arguments(tool, arguments)
                    target_name = arguments["name"]
                    if target_name in self._capability_tool_names:
                        raise ValueError("capability_call_recursive")
                    if target_name not in self._catalog_name_set:
                        raise ValueError("capability_not_found")
                    tool = self.tools[target_name]
                    expected_revision = arguments.get("expected_revision")
                    arguments = arguments["arguments"]
                # Resolve legacy aliases before policy checks and audit.
                target_name = tool.name
                channel = self._channel_for(target_name)
                revision = self._capability_revision(tool)
                if expected_revision is not None and expected_revision != revision:
                    raise ValueError("capability_revision_changed")
                if context.capability_bindings is not None:
                    bindings = dict(context.capability_bindings)
                    if target_name not in bindings:
                        raise ValueError("capability_not_bound")
                    if bindings[target_name] != revision:
                        raise ValueError("capability_revision_changed")
            self._validate_capability_arguments(tool, arguments)
            if context.allowed_tools is not None and target_name not in context.allowed_tools:
                raise ValueError("capability_not_allowed")
            if context.deadline is not None and time.monotonic() >= context.deadline:
                raise ValueError("capability_deadline_exceeded")
        except Exception as exc:
            # Preserve actionable reason codes without copying instance data.
            reason = str(exc).split(":", 1)[0]
            safe_reasons = {
                "capability_catalog_changed", "capability_revision_changed", "capability_not_bound",
                "capability_not_allowed", "capability_deadline_exceeded", "capability_not_found",
                "capability_call_recursive", "capability_arguments_invalid", "capability_schema_invalid",
                "capability_schema_dialect_unsupported", "capability_external_ref_unsupported",
            }
            # Do not log validation messages: they may contain argument values.
            self.audit.log("tool_call", tool=target_name, client_id=client_id,
                           ok=False, channel=channel, stage="admission",
                           error=type(exc).__name__, error_code=reason if reason in safe_reasons else "admission_failed",
                           run_id=context.run_id,
                           capability_revision=revision,
                           entrypoint=context.entrypoint,
                           **({"invoked_via": invoked_via} if invoked_via else {}))
            raise
        return self._invoke_tool(target_name, tool, arguments, client_id,
                                 channel=channel, invoked_via=invoked_via,
                                 context=context, catalog_version=version, capability_revision=revision)

    def _invoke_tool(
        self, name: str, tool: Tool, arguments: dict[str, Any], client_id: str,
        *, channel: str, invoked_via: str | None = None,
        context: CallContext, catalog_version: str, capability_revision: str,
    ) -> dict[str, Any]:
        try:
            # Each subsystem returns native structured data; _ok wraps it in
            # MCP text content while preserving structuredContent for clients
            # that can use it.
            result = tool.handler(self.config, arguments or {})
            audit_fields: dict[str, Any] = {}
            audit_ok = not (isinstance(result, RawMCPToolResult) and result.value.get("isError") is True)
            if name.startswith("computer_"):
                outcome = result.value.get("structuredContent", {}) if isinstance(result, RawMCPToolResult) else result
                if isinstance(outcome, dict):
                    audit_ok = audit_ok and outcome.get("success") is not False
                    if outcome.get("effect") in {"completed", "not_started", "outcome_unknown"}:
                        audit_fields["effect"] = outcome["effect"]
                    if not audit_ok:
                        audit_fields["error"] = outcome.get("error", "computer_failed")
            if name == "server_info" and isinstance(result, dict):
                with self._catalog_lock:
                    tool_catalog = {
                        "exposure": self.tool_exposure,
                        "capability_count": len(self._catalog_names),
                        "listed_count": len(self._all_listed_names),
                        "catalog_version": self.catalog_version,
                    }
                result = {**result, "tool_catalog": tool_catalog}
            self.audit.log("tool_call", tool=name, client_id=client_id, ok=audit_ok, channel=channel,
                           run_id=context.run_id, entrypoint=context.entrypoint, catalog_version=catalog_version,
                           capability_revision=capability_revision,
                           **({"invoked_via": invoked_via} if invoked_via else {}), **audit_fields)
            if isinstance(result, RawMCPToolResult):
                return result.value
            return _ok(result)
        except Exception as exc:
            self.audit.log("tool_call", tool=name, client_id=client_id, ok=False, error=str(exc), channel=channel,
                           run_id=context.run_id, entrypoint=context.entrypoint, catalog_version=catalog_version,
                           capability_revision=capability_revision,
                           **({"invoked_via": invoked_via} if invoked_via else {}))
            raise
