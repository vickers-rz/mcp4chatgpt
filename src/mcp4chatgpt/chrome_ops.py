"""通过 macOS AppleScript 读取 Chrome 标签页与页面上下文。

这是无需浏览器扩展时的降级路径：Python 调用 ``osascript``，Chrome 再执行受限的
标签页查询或页面脚本。与 ``ext_bridge`` 相比，它部署简单，但受 macOS 自动化权限、
Chrome Apple Events 设置和脚本执行能力限制。

所有跨语言边界都必须转义字符串、限制超时并规范化错误。尤其不能把未经转义的模型
输入直接嵌入 AppleScript，否则会形成脚本注入。返回页面文本时还需截断，以避免
单个网页耗尽 MCP 响应预算。
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from .config import Config
from .safety import redact, truncate_text


class ChromeOpsError(RuntimeError):
    pass


def _as_applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run_osascript(script: str, timeout_sec: int = 10) -> str:
    try:
        completed = subprocess.run(
            ["osascript", "-"],
            input=script,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ChromeOpsError("Timed out while asking Google Chrome for tab context.") from exc
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "osascript failed"
        raise ChromeOpsError(message)
    return completed.stdout.strip()


def _chrome_not_running_script() -> str:
    return """
tell application "System Events"
    if not (exists process "Google Chrome") then
        return "not_running"
    end if
end tell
return "running"
"""


def _ensure_chrome_running() -> None:
    if _run_osascript(_chrome_not_running_script()) != "running":
        raise ChromeOpsError("Google Chrome is not running.")


def list_tabs(config: Config, max_tabs: int = 80) -> dict[str, Any]:
    _ensure_chrome_running()
    max_tabs = max(1, min(max_tabs, 300))
    delimiter = "|||"
    script = f"""
set rows to {{}}
set rowDelimiter to "{delimiter}"
tell application "Google Chrome"
    set tabCounter to 0
    repeat with wi from 1 to count windows
        set win to window wi
        set activeIndex to active tab index of win
        repeat with ti from 1 to count tabs of win
            set tabCounter to tabCounter + 1
            if tabCounter > {max_tabs} then exit repeat
            set tabObj to tab ti of win
            set isActive to "false"
            if ti = activeIndex then set isActive to "true"
            set end of rows to (wi as text) & rowDelimiter & (ti as text) & rowDelimiter & isActive & rowDelimiter & (title of tabObj as text) & rowDelimiter & (URL of tabObj as text)
        end repeat
        if tabCounter > {max_tabs} then exit repeat
    end repeat
end tell
set AppleScript's text item delimiters to linefeed
return rows as text
"""
    output = _run_osascript(script)
    tabs: list[dict[str, Any]] = []
    for raw in output.splitlines():
        parts = raw.split(delimiter, 4)
        if len(parts) != 5:
            continue
        window_index, tab_index, active, title, url = parts
        tabs.append(
            {
                "window_index": int(window_index),
                "tab_index": int(tab_index),
                "active": active == "true",
                "title": redact(title),
                "url": redact(url),
            }
        )
    return {
        "tabs": tabs,
        "count": len(tabs),
        "truncated": len(tabs) >= max_tabs,
        "note": "This tool returns tab metadata only. Use chrome_get_active_tab_context to read the active page text.",
    }


def get_active_tab_context(
    config: Config,
    max_chars: int = 12000,
    include_text: bool = True,
    include_selection: bool = True,
) -> dict[str, Any]:
    _ensure_chrome_running()
    max_chars = max(1000, min(max_chars, config.max_output_chars))
    payload_js = """
(() => {
  const meta = {};
  for (const el of Array.from(document.querySelectorAll("meta"))) {
    const key = el.getAttribute("name") || el.getAttribute("property");
    const value = el.getAttribute("content");
    if (key && value && key.length < 120 && value.length < 2000) meta[key] = value;
  }
  const selection = String(window.getSelection ? window.getSelection() : "");
  const text = document.body ? document.body.innerText : "";
  return JSON.stringify({
    title: document.title || "",
    url: location.href,
    selection,
    meta,
    text
  });
})()
""".strip()
    script = f"""
set jsCode to {_as_applescript_string(payload_js)}
tell application "Google Chrome"
    if (count windows) = 0 then error "Google Chrome has no open windows."
    set tabObj to active tab of front window
    return execute tabObj javascript jsCode
end tell
"""
    try:
        raw = _run_osascript(script)
    except ChromeOpsError as exc:
        message = str(exc)
        if "Executing JavaScript through Apple Events" in message or "not allowed" in message:
            message = (
                "Chrome blocked JavaScript from Apple Events. In Chrome, enable "
                "View > Developer > Allow JavaScript from Apple Events, then retry."
            )
        raise ChromeOpsError(message) from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ChromeOpsError("Chrome returned non-JSON page context.") from exc

    result: dict[str, Any] = {
        "title": redact(str(data.get("title", ""))),
        "url": redact(str(data.get("url", ""))),
        "meta": {redact(str(k)): redact(str(v)) for k, v in dict(data.get("meta") or {}).items()},
    }
    if include_selection:
        selection, selection_truncated = truncate_text(redact(str(data.get("selection", ""))), max_chars)
        result["selection"] = selection
        result["selection_truncated"] = selection_truncated
    if include_text:
        text, text_truncated = truncate_text(redact(str(data.get("text", ""))), max_chars)
        result["text"] = text
        result["text_truncated"] = text_truncated
    return result


def get_links(config: Config, max_links: int = 100) -> dict[str, Any]:
    _ensure_chrome_running()
    max_links = max(1, min(max_links, 500))
    payload_js = f"""
(() => {{
  const links = Array.from(document.links).slice(0, {max_links}).map((link, index) => ({{
    index,
    text: (link.innerText || link.textContent || link.getAttribute("aria-label") || "").trim().replace(/\\s+/g, " "),
    href: link.href,
    title: link.title || ""
  }}));
  return JSON.stringify({{
    title: document.title || "",
    url: location.href,
    links,
    totalLinks: document.links.length
  }});
}})()
""".strip()
    script = f"""
set jsCode to {_as_applescript_string(payload_js)}
tell application "Google Chrome"
    if (count windows) = 0 then error "Google Chrome has no open windows."
    set tabObj to active tab of front window
    return execute tabObj javascript jsCode
end tell
"""
    try:
        raw = _run_osascript(script)
    except ChromeOpsError as exc:
        message = str(exc)
        if "Executing JavaScript through Apple Events" in message or "not allowed" in message:
            message = (
                "Chrome blocked JavaScript from Apple Events. In Chrome, enable "
                "View > Developer > Allow JavaScript from Apple Events, then retry."
            )
        raise ChromeOpsError(message) from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ChromeOpsError("Chrome returned non-JSON link context.") from exc
    links = []
    for link in data.get("links") or []:
        if not isinstance(link, dict):
            continue
        links.append(
            {
                "index": link.get("index"),
                "text": redact(str(link.get("text", ""))),
                "href": redact(str(link.get("href", ""))),
                "title": redact(str(link.get("title", ""))),
            }
        )
    return {
        "title": redact(str(data.get("title", ""))),
        "url": redact(str(data.get("url", ""))),
        "links": links,
        "count": len(links),
        "total_links": int(data.get("totalLinks") or len(links)),
        "truncated": int(data.get("totalLinks") or len(links)) > len(links),
    }
