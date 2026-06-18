from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from . import __version__
from .audit import AuditLogger
from .config import Config
from . import chrome_ops, ext_ops, knowledge_ops, local_ops, terminal_ops, web_ops


ToolHandler = Callable[[Config, dict[str, Any]], Any]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler

    def definition(self) -> dict[str, Any]:
        security_schemes = [{"type": "oauth2", "scopes": ["local", "web", "knowledge"]}]
        annotations = _annotations_for_tool(self.name)
        title = self.name.replace("_", " ").title()
        return {
            "name": self.name,
            "title": title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "securitySchemes": security_schemes,
            # ChatGPT still reads some descriptor data from _meta for
            # compatibility; keep this mirrored with the public field.
            "_meta": {
                "securitySchemes": security_schemes,
                "openai/toolInvocation/invoking": f"Running {title}",
                "openai/toolInvocation/invoked": f"Finished {title}",
            },
            "annotations": annotations,
        }


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
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
        "terminal_run_command",
        "terminal_send_input",
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
        Tool("terminal_list_supported_apps", "List supported macOS terminal apps.", _schema({}), lambda c, a: terminal_ops.list_supported_apps(c)),
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
            "Read recent context from Terminal.app, iTerm2, or Termius.",
            _schema({"app": {"type": "string", "enum": ["terminal", "iterm2", "termius"]}, "max_chars": {"type": "integer", "default": 12000}, "redact_secrets": {"type": "boolean", "default": True}, "label": {"type": "string"}}, ["app"]),
            lambda c, a: terminal_ops.get_app_context(c, a["app"], int(a.get("max_chars", 12000)), bool(a.get("redact_secrets", True)), a.get("label")),
        ),
        Tool(
            "terminal_run_command",
            "Send a visible command to the front Terminal.app/iTerm2/Termius tab and press Return.",
            _schema({"command": {"type": "string"}, "app": {"type": "string", "enum": ["terminal", "iterm2", "termius"], "default": "terminal"}, "label": {"type": "string"}}, ["command"]),
            lambda c, a: terminal_ops.run_command(c, a["command"], a.get("app", "terminal"), a.get("label")),
        ),
        Tool(
            "terminal_send_input",
            "Type or paste text into Terminal.app/iTerm2/Termius; set press_return=false to paste without executing.",
            _schema({"text": {"type": "string"}, "press_return": {"type": "boolean", "default": True}, "sensitive": {"type": "boolean", "default": False}, "app": {"type": "string", "enum": ["terminal", "iterm2", "termius"], "default": "terminal"}, "label": {"type": "string"}}, ["text"]),
            lambda c, a: terminal_ops.send_input(c, a["text"], bool(a.get("press_return", True)), bool(a.get("sensitive", False)), a.get("app", "terminal"), a.get("label")),
        ),
        Tool("web_search", "Search the web via Firecrawl.", _schema({"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}}, ["query"]), lambda c, a: web_ops.search(c, a["query"], int(a.get("limit", 5)))),
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
            "Navigate a Chrome tab to a URL, or open a new tab. Requires the MCP4ChatGPT Chrome extension.",
            _schema({
                "url": {"type": "string"},
                "tab_id": {"type": "integer"},
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
            "Fill a form input or textarea with a value (by CSS selector) in a Chrome tab. Optionally submit the form. Requires the MCP4ChatGPT Chrome extension.",
            _schema({
                "selector": {"type": "string"},
                "value": {"type": "string"},
                "tab_id": {"type": "integer"},
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
    def __init__(self, config: Config, audit: AuditLogger):
        self.config = config
        self.audit = audit
        self.tools = {tool.name: tool for tool in build_tools()}

    def list_tools(self) -> dict[str, Any]:
        return {"tools": [tool.definition() for tool in self.tools.values()]}

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
