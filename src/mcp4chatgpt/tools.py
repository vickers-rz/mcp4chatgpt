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

import json
from dataclasses import dataclass
from typing import Any, Callable

from . import __version__
from .audit import AuditLogger
from .config import Config
from . import chrome_ops, ext_ops, knowledge_ops, local_ops, terminal_ops, web_ops


ToolHandler = Callable[[Config, dict[str, Any]], Any]

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

    def definition(self, *, auth_required: bool = True) -> dict[str, Any]:
        security_schemes = [{"type": "oauth2", "scopes": ["local", "web", "knowledge"]}]
        annotations = _annotations_for_tool(self.name)
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
        if auth_required:
            definition["securitySchemes"] = security_schemes
            definition["_meta"]["securitySchemes"] = security_schemes
        return definition


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
        "local_write_file",
        "local_apply_patch",
        "local_run_command",
        "app_write_text",
        "terminal_run_command",
        "terminal_send_input",
        "web_add_to_knowledge",
        "knowledge_add_source",
        "ext_navigate",
        "ext_click_element",
        "ext_fill_input",
        "ext_run_js",
    }
    open_world = name.startswith("web_") or name in {
        "local_run_command",
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
        "ext_listen_changes",
    }
    destructive = name in {
        "local_write_file",
        "local_apply_patch",
        "local_run_command",
        "app_write_text",
        "terminal_run_command",
        "terminal_send_input",
        "ext_navigate",
        "ext_click_element",
        "ext_fill_input",
        "ext_run_js",
    }
    return {
        "readOnlyHint": name not in mutating,
        "destructiveHint": destructive,
        "openWorldHint": open_world,
        "idempotentHint": name not in mutating,
    }


def _ok(result: Any) -> dict[str, Any]:
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "structuredContent": result}


def _server_info(config: Config, _args: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "mcp4chatgpt",
        "version": __version__,
        "mcp_url": config.mcp_url,
        "allowed_roots": [str(root) for root in config.allowed_roots],
        "knowledge_store_dir": str(config.knowledge_store_dir),
        "firecrawl_configured": bool(config.firecrawl_api_key),
        "co_te_path": str(config.co_te_path),
    }


def _web_add_to_knowledge(config: Config, args: dict[str, Any]) -> dict[str, Any]:
    scraped = web_ops.scrape(config, args["url"], formats=["markdown"])
    data = scraped.get("data", scraped)
    text = data.get("markdown") or data.get("content") or json.dumps(data, ensure_ascii=False)
    title = data.get("metadata", {}).get("title") or args.get("title") or args["url"]
    added = knowledge_ops.add_source(config, title=title, text=text, url=args["url"], metadata={"web_result": data.get("metadata", {})})
    return {"scrape": scraped, "knowledge": added}


def build_tools() -> list[Tool]:
    # The tool list is intentionally centralized. The HTTP layer only knows
    # about JSON-RPC; capability grouping and schemas live here.
    return [
        Tool("server_info", "Return service status, enabled backends, and safety boundaries.", _schema({}), _server_info),
        Tool(
            "local_list_files",
            "List files in an allowed local directory.",
            _schema({"path": {"type": "string", "default": "."}, "max_entries": {"type": "integer", "default": 200}}),
            lambda c, a: local_ops.list_files(c, a.get("path", "."), int(a.get("max_entries", 200))),
        ),
        Tool(
            "local_read_text",
            "Read a UTF-8 text file from an allowed local path.",
            _schema({"path": {"type": "string"}, "max_chars": {"type": "integer"}}, ["path"]),
            lambda c, a: local_ops.read_text(c, a["path"], a.get("max_chars")),
        ),
        Tool(
            "local_write_file",
            "Write a UTF-8 file under MCP_ALLOWED_ROOTS.",
            _schema({"path": {"type": "string"}, "content": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}}, ["path", "content"]),
            lambda c, a: local_ops.write_file(c, a["path"], a["content"], bool(a.get("overwrite", False))),
        ),
        Tool(
            "local_apply_patch",
            "Replace one exact text block in an allowed file and return a unified diff.",
            _schema({"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, ["path", "old", "new"]),
            lambda c, a: local_ops.apply_patch(c, a["path"], a["old"], a["new"]),
        ),
        Tool(
            "local_run_command",
            "Run a non-dangerous shell command in an allowed cwd, returning stdout/stderr and writing a local execution log.",
            _schema({"command": {"type": "string"}, "cwd": {"type": "string"}, "timeout_sec": {"type": "integer", "default": 30}}, ["command"]),
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
            "Alias for chrome_list_tabs. List titles and URLs for open Google Chrome tabs on this Mac.",
            _schema({"max_tabs": {"type": "integer", "default": 80}}),
            lambda c, a: chrome_ops.list_tabs(c, int(a.get("max_tabs", 80))),
        ),
        Tool(
            "browser_current_tab",
            "Return the front Google Chrome tab title, URL, metadata, and selected text without body text.",
            _schema({"max_chars": {"type": "integer", "default": 12000}}),
            lambda c, a: chrome_ops.get_active_tab_context(c, int(a.get("max_chars", 12000)), False, True),
        ),
        Tool(
            "browser_get_page_text",
            "Read visible body text from the front Google Chrome tab.",
            _schema({"max_chars": {"type": "integer", "default": 12000}}),
            lambda c, a: chrome_ops.get_active_tab_context(c, int(a.get("max_chars", 12000)), True, False),
        ),
        Tool(
            "browser_get_selection",
            "Read selected text from the front Google Chrome tab.",
            _schema({"max_chars": {"type": "integer", "default": 12000}}),
            lambda c, a: chrome_ops.get_active_tab_context(c, int(a.get("max_chars", 12000)), False, True),
        ),
        Tool(
            "browser_get_links",
            "Read links from the front Google Chrome tab.",
            _schema({"max_links": {"type": "integer", "default": 100}}),
            lambda c, a: chrome_ops.get_links(c, int(a.get("max_links", 100))),
        ),
        Tool(
            "terminal_get_app_context",
            "Compatibility alias for app_get_context. Read recent context from any co-te supported macOS app.",
            _schema({"app": {"type": "string", "enum": CO_TE_APP_KEYS}, "max_chars": {"type": "integer", "default": 12000}, "redact_secrets": {"type": "boolean", "default": True}, "label": {"type": "string"}}, ["app"]),
            lambda c, a: terminal_ops.get_app_context(c, a["app"], int(a.get("max_chars", 12000)), bool(a.get("redact_secrets", True)), a.get("label")),
        ),
        Tool(
            "app_get_context",
            "Read selected text, focused editor content, window context, or terminal history from any co-te supported macOS app.",
            _schema({"app": {"type": "string", "enum": CO_TE_APP_KEYS}, "max_chars": {"type": "integer", "default": 12000}, "redact_secrets": {"type": "boolean", "default": True}, "label": {"type": "string"}}, ["app"]),
            lambda c, a: terminal_ops.get_app_context(c, a["app"], int(a.get("max_chars", 12000)), bool(a.get("redact_secrets", True)), a.get("label")),
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
            "Send a visible command to the front Terminal.app/iTerm2/Termius tab and press Return.",
            _schema({"command": {"type": "string"}, "app": {"type": "string", "enum": CO_TE_TERMINAL_APP_KEYS, "default": "terminal"}, "label": {"type": "string"}}, ["command"]),
            lambda c, a: terminal_ops.run_command(c, a["command"], a.get("app", "terminal"), a.get("label")),
        ),
        Tool(
            "terminal_send_input",
            "Type or paste text into Terminal.app/iTerm2/Termius; set press_return=false to paste without executing.",
            _schema({"text": {"type": "string"}, "press_return": {"type": "boolean", "default": True}, "sensitive": {"type": "boolean", "default": False}, "app": {"type": "string", "enum": CO_TE_TERMINAL_APP_KEYS, "default": "terminal"}, "label": {"type": "string"}}, ["text"]),
            lambda c, a: terminal_ops.send_input(c, a["text"], bool(a.get("press_return", True)), bool(a.get("sensitive", False)), a.get("app", "terminal"), a.get("label")),
        ),
        Tool("web_search", "Search the web via Firecrawl.", _schema({"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}}, ["query"]), lambda c, a: web_ops.search(c, a["query"], int(a.get("limit", 5)))),
        Tool("web_brave_search", "Search the web via Brave Search API.", _schema({"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}}, ["query"]), lambda c, a: web_ops.brave_search(c, a["query"], int(a.get("limit", 5)))),
        Tool(
            "web_combined_search",
            "Search with Brave or Firecrawl, optionally fetching top result pages through Firecrawl.",
            _schema({
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
                "engine": {"type": "string", "enum": ["brave", "firecrawl", "auto"], "default": "brave"},
                "fetch_content": {"type": "boolean", "default": False},
                "fetch_limit": {"type": "integer", "default": 3},
            }, ["query"]),
            lambda c, a: web_ops.combined_search(
                c,
                a["query"],
                int(a.get("limit", 5)),
                engine=a.get("engine", "brave"),
                fetch_content=bool(a.get("fetch_content", False)),
                fetch_limit=int(a.get("fetch_limit", 3)),
            ),
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
            "Fill an input or textarea by CSS selector and optionally submit its form. Pass the same tab_id returned by ext_navigate so the operation cannot drift to another active tab. Returns a diagnostic error when filling or submission fails. Requires the MCP4ChatGPT Chrome extension.",
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
            }, ["code"]),
            lambda c, a: ext_ops.ext_run_js(
                c,
                str(a["code"]),
                a.get("tab_id"),
                int(a.get("max_chars", 10000)),
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


class ToolRegistry:
    """工具目录及统一调用入口。

    注册表在启动时把工具列表索引为 ``name -> Tool``，使 ``tools/list`` 与
    ``tools/call`` 使用同一份定义，避免“声明存在但无法执行”或反向漂移。
    所有调用都在这里记录成功/失败审计事件；异常不被吞掉，而是交给传输层转换为
    JSON-RPC error，保证客户端能区分正常工具结果与执行失败。
    """

    def __init__(self, config: Config, audit: AuditLogger):
        self.config = config
        self.audit = audit
        self.tools = {tool.name: tool for tool in build_tools()}

    def list_tools(self) -> dict[str, Any]:
        return {
            "tools": [
                tool.definition(auth_required=not self.config.local_auth_disabled)
                for tool in self.tools.values()
            ]
        }

    def call_tool(self, name: str, arguments: dict[str, Any], client_id: str = "") -> dict[str, Any]:
        tool = self.tools.get(name)
        if not tool:
            raise ValueError(f"Unknown tool: {name}")
        try:
            # Each subsystem returns native structured data; _ok wraps it in
            # MCP text content while preserving structuredContent for clients
            # that can use it.
            result = tool.handler(self.config, arguments or {})
            self.audit.log("tool_call", tool=name, client_id=client_id, ok=True)
            return _ok(result)
        except Exception as exc:
            self.audit.log("tool_call", tool=name, client_id=client_id, ok=False, error=str(exc))
            raise
