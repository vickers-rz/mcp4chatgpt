"""把高层浏览器工具转换为 Chrome 扩展桥接命令。

每个函数都调用 :func:`ext_bridge.send_command`，经实时 WebSocket 把请求发送到
扩展 Service Worker，等待结果后再返回适合 MCP 工具响应的结构化字典。扩展必须
已经安装并连接；调用其他工具前可先用 :func:`ext_connection_status` 检查状态。

本模块是 ``tools.py`` 与 ``ext_bridge.py`` 之间的适配层：它验证连接、整理参数并
规范化结果，使工具注册表无需了解 WebSocket、标签页 ID、截图编码或 DOM 事件。

导航、点击、填表和执行 JavaScript 会改变浏览器或外部世界状态，必须与只读操作
区分，准确标记副作用，并对脚本执行、超时和返回体大小施加更严格限制。
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

from . import ext_bridge
from .config import Config
from .safety import redact, truncate_text


class ExtNotConnectedError(RuntimeError):
    """Raised when the Chrome extension is not connected to the bridge."""


def _require_connected() -> None:
    if not ext_bridge.is_connected():
        raise ExtNotConnectedError(
            "Chrome extension is not connected. "
            "Open Chrome, click the MCP4ChatGPT extension icon, and ensure it shows 'Connected'."
        )


# ---------------------------------------------------------------------------
# Connection / status
# ---------------------------------------------------------------------------

def ext_connection_status(_config: Config) -> dict[str, Any]:
    """Return current extension connection status."""
    info = ext_bridge.connection_info()
    if info["connected"]:
        info["hint"] = "Extension is connected. All browser tools are available."
    else:
        info["hint"] = (
            "Extension is NOT connected. Open Chrome, install the MCP4ChatGPT extension, "
            "and click the extension icon to connect."
        )
    return info


# ---------------------------------------------------------------------------
# Tab listing
# ---------------------------------------------------------------------------

def ext_list_tabs(config: Config, max_tabs: int = 100) -> dict[str, Any]:
    """List all open Chrome tabs with their window, index, URL and title."""
    _require_connected()
    result = ext_bridge.send_command("list_tabs", {"maxTabs": max_tabs})
    tabs = result.get("tabs", [])
    redacted = [
        {
            "window_id": t.get("windowId"),
            "tab_id": t.get("tabId"),
            "index": t.get("index"),
            "active": t.get("active", False),
            "pinned": t.get("pinned", False),
            "title": redact(str(t.get("title", ""))),
            "url": redact(str(t.get("url", ""))),
            "status": t.get("status", ""),
        }
        for t in (tabs if isinstance(tabs, list) else [])
    ]
    return {
        "tabs": redacted,
        "count": len(redacted),
        "truncated": result.get("truncated", False),
    }


# ---------------------------------------------------------------------------
# Active tab context
# ---------------------------------------------------------------------------

def ext_get_active_tab(
    config: Config,
    max_chars: int = 12000,
    include_text: bool = True,
    include_selection: bool = True,
    include_meta: bool = True,
) -> dict[str, Any]:
    """Read front Chrome tab: title, URL, visible text, selection, meta tags."""
    _require_connected()
    max_chars = max(1000, min(max_chars, config.max_output_chars))
    result = ext_bridge.send_command(
        "get_active_tab",
        {
            "includeText": include_text,
            "includeSelection": include_selection,
            "includeMeta": include_meta,
        },
        timeout=20,
    )
    out: dict[str, Any] = {
        "title": redact(str(result.get("title", ""))),
        "url": redact(str(result.get("url", ""))),
        "tab_id": result.get("tabId"),
        "window_id": result.get("windowId"),
    }
    if include_meta:
        out["meta"] = {
            redact(str(k)): redact(str(v))
            for k, v in (result.get("meta") or {}).items()
        }
    if include_selection:
        sel, sel_trunc = truncate_text(redact(str(result.get("selection", ""))), max_chars)
        out["selection"] = sel
        out["selection_truncated"] = sel_trunc
    if include_text:
        text, text_trunc = truncate_text(redact(str(result.get("text", ""))), max_chars)
        out["text"] = text
        out["text_truncated"] = text_trunc
    return out


# ---------------------------------------------------------------------------
# Full DOM
# ---------------------------------------------------------------------------

def ext_get_dom(
    config: Config,
    tab_id: int | None = None,
    selector: str = "body",
    max_chars: int = 50000,
) -> dict[str, Any]:
    """Get the outerHTML of a DOM node (default: document body) from a Chrome tab."""
    _require_connected()
    max_chars = max(1000, min(max_chars, config.max_output_chars))
    args: dict[str, Any] = {"selector": selector}
    if tab_id is not None:
        args["tabId"] = tab_id
    result = ext_bridge.send_command("get_dom", args, timeout=20)
    html_raw = str(result.get("html", ""))
    html, truncated = truncate_text(html_raw, max_chars)
    return {
        "tab_id": result.get("tabId"),
        "url": redact(str(result.get("url", ""))),
        "selector": selector,
        "html": html,
        "truncated": truncated,
        "original_length": len(html_raw),
    }


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def ext_get_selection(config: Config, tab_id: int | None = None) -> dict[str, Any]:
    """Get currently selected text from a Chrome tab."""
    _require_connected()
    args: dict[str, Any] = {}
    if tab_id is not None:
        args["tabId"] = tab_id
    result = ext_bridge.send_command("get_selection", args, timeout=10)
    return {
        "tab_id": result.get("tabId"),
        "url": redact(str(result.get("url", ""))),
        "selection": redact(str(result.get("selection", ""))),
    }


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------

def ext_screenshot(
    config: Config,
    tab_id: int | None = None,
    save_to_file: bool = True,
    quality: int = 80,
) -> dict[str, Any]:
    """Take a screenshot of a Chrome tab.

    Returns the saved file path (recommended) plus a thumbnail base64 string.
    Set save_to_file=False to skip saving and return only base64 (large payload).
    """
    _require_connected()
    args: dict[str, Any] = {"quality": max(10, min(quality, 100))}
    if tab_id is not None:
        args["tabId"] = tab_id
    result = ext_bridge.send_command("screenshot", args, timeout=20)

    b64_data: str = result.get("dataUrl", "")
    # Strip the data:image/...;base64, prefix
    if "," in b64_data:
        b64_data = b64_data.split(",", 1)[1]

    out: dict[str, Any] = {
        "tab_id": result.get("tabId"),
        "url": redact(str(result.get("url", ""))),
        "width": result.get("width"),
        "height": result.get("height"),
        "format": "png",
    }

    if save_to_file and b64_data:
        screenshot_dir = config.ext_screenshot_dir
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time() * 1000)
        filename = f"screenshot_{timestamp}.png"
        file_path = screenshot_dir / filename
        file_path.write_bytes(base64.b64decode(b64_data))
        out["file_path"] = str(file_path)
        out["file_size_bytes"] = file_path.stat().st_size
        out["note"] = f"Screenshot saved to {file_path}"
        # Include small b64 only if caller also wants raw
    else:
        out["data_base64"] = b64_data

    return out


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

def ext_navigate(
    config: Config,
    url: str,
    tab_id: int | None = None,
    new_tab: bool = False,
) -> dict[str, Any]:
    """Navigate a Chrome tab to a URL (or open a new tab)."""
    _require_connected()
    if not url.startswith(("http://", "https://", "about:", "chrome:")):
        raise ValueError(f"URL must start with http:// or https://: {url!r}")
    args: dict[str, Any] = {"url": url, "newTab": new_tab}
    if tab_id is not None:
        args["tabId"] = tab_id
    result = ext_bridge.send_command("navigate", args, timeout=30)
    return {
        "tab_id": result.get("tabId"),
        "url": redact(str(result.get("url", url))),
        "status": result.get("status", "navigating"),
    }


# ---------------------------------------------------------------------------
# Click element
# ---------------------------------------------------------------------------

def ext_click_element(
    config: Config,
    selector: str,
    tab_id: int | None = None,
) -> dict[str, Any]:
    """Click a DOM element identified by a CSS selector in a Chrome tab."""
    _require_connected()
    if not selector or len(selector) > 500:
        raise ValueError("selector must be a non-empty CSS selector (max 500 chars)")
    args: dict[str, Any] = {"selector": selector}
    if tab_id is not None:
        args["tabId"] = tab_id
    result = ext_bridge.send_command("click_element", args, timeout=15)
    return {
        "tab_id": result.get("tabId"),
        "selector": selector,
        "clicked": result.get("clicked", False),
        "element_text": redact(str(result.get("elementText", ""))),
    }


# ---------------------------------------------------------------------------
# Fill input
# ---------------------------------------------------------------------------

def ext_fill_input(
    config: Config,
    selector: str,
    value: str,
    tab_id: int | None = None,
    submit: bool = False,
) -> dict[str, Any]:
    """Fill a form input / textarea identified by CSS selector."""
    _require_connected()
    if not selector or len(selector) > 500:
        raise ValueError("selector must be a non-empty CSS selector (max 500 chars)")
    if len(value) > 100_000:
        raise ValueError("value is too long (max 100,000 chars)")
    args: dict[str, Any] = {"selector": selector, "value": value, "submit": submit}
    if tab_id is not None:
        args["tabId"] = tab_id
    result = ext_bridge.send_command("fill_input", args, timeout=15)
    output = {
        "tab_id": result.get("tabId"),
        "selector": selector,
        "filled": result.get("filled", False),
        "submitted": result.get("submitted", False),
        "element_tag": result.get("tagName"),
    }
    if error := result.get("error"):
        output["error"] = str(error)
    return output


# ---------------------------------------------------------------------------
# Run JavaScript (advanced — user must enable in popup)
# ---------------------------------------------------------------------------

def ext_run_js(
    config: Config,
    code: str,
    tab_id: int | None = None,
    max_chars: int = 10000,
) -> dict[str, Any]:
    """Execute JavaScript in a Chrome tab and return the result.

    Requires the user to enable 'Allow JS execution' in the extension popup.
    The extension will show a confirmation badge for each execution.
    """
    _require_connected()
    if not code or not code.strip():
        raise ValueError("JS code cannot be empty")
    if len(code) > 200_000:
        raise ValueError("JS code is too long (max 200,000 chars)")
    max_chars = max(100, min(max_chars, config.max_output_chars))
    args: dict[str, Any] = {"code": code}
    if tab_id is not None:
        args["tabId"] = tab_id
    result = ext_bridge.send_command("run_js", args, timeout=30)

    if error := result.get("error"):
        return {
            "tab_id": result.get("tabId"),
            "error": str(error),
            "result": None,
            "execution_world": result.get("executionWorld"),
        }

    raw_result = result.get("result")
    result_str = (
        str(raw_result)
        if not isinstance(raw_result, str)
        else raw_result
    )
    result_str, truncated = truncate_text(result_str, max_chars)
    return {
        "tab_id": result.get("tabId"),
        "result": result_str,
        "truncated": truncated,
        "type": result.get("resultType", "unknown"),
        "execution_world": result.get("executionWorld"),
    }


# ---------------------------------------------------------------------------
# Page change monitoring
# ---------------------------------------------------------------------------

def ext_listen_changes(
    config: Config,
    duration_sec: int = 30,
    tab_id: int | None = None,
) -> dict[str, Any]:
    """Listen for page navigation / DOM changes for up to duration_sec seconds.

    Returns a list of captured change events (URL navigations, title changes,
    load completions) that occurred during the listening period.
    """
    _require_connected()
    duration_sec = max(1, min(duration_sec, 120))

    events: list[dict[str, Any]] = []

    def _on_event(evt: dict[str, Any]) -> None:
        if tab_id is None or evt.get("tabId") == tab_id:
            events.append(
                {
                    "type": evt.get("event"),
                    "tab_id": evt.get("tabId"),
                    "url": redact(str(evt.get("url", ""))),
                    "title": redact(str(evt.get("title", ""))),
                    "timestamp": evt.get("timestamp", time.time()),
                }
            )

    ext_bridge.subscribe_changes(_on_event)
    # Also tell extension to start active MutationObserver for this period
    try:
        ext_bridge.send_command(
            "subscribe_changes",
            {"durationSec": duration_sec, "tabId": tab_id},
            timeout=10,
        )
        time.sleep(duration_sec)
    except Exception as exc:
        raise RuntimeError(f"Failed to subscribe to page changes: {exc}") from exc
    finally:
        ext_bridge.unsubscribe_changes(_on_event)

    return {
        "events": events,
        "count": len(events),
        "duration_sec": duration_sec,
        "tab_id": tab_id,
    }
