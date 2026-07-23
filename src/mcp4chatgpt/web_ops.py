"""外部网页检索、抓取与站点交互服务的 HTTP 客户端封装。

MCP 工具层不直接理解第三方网页 API，而是把稳定参数交给本模块。本模块负责拼接
端点、认证请求、序列化 JSON、处理超时与把远端错误转换为本项目的异常。这样即使
以后替换网页服务提供方，``tools.py`` 暴露给客户端的工具契约仍可保持稳定。

所有这些操作都属于 open-world 行为：输入 URL 和返回内容来自本机信任边界之外，
因此调用方不能把抓取结果当作可信指令，也不应把本机密钥或私有文件内容拼入请求。
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import Config


class WebOpsNotConfigured(RuntimeError):
    pass


def _open_json(req: urllib.request.Request, *, timeout: int, provider: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.25)
    assert last_error is not None
    detail = getattr(last_error, "reason", last_error)
    raise RuntimeError(f"{provider} request failed: {detail}") from last_error


def _firecrawl_request(
    config: Config,
    endpoint: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not config.firecrawl_api_key:
        raise WebOpsNotConfigured("web_ops_not_configured: FIRECRAWL_API_KEY is not set.")
    # This module stays a thin adapter: Firecrawl owns crawl/browser scale,
    # while MCP4ChatGPT owns auth, routing, and knowledge-store ingestion.
    url = f"{config.firecrawl_base_url.rstrip('/')}{endpoint}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {config.firecrawl_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        return _open_json(req, timeout=60, provider="Firecrawl")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Firecrawl request failed: HTTP {exc.code}: {text}") from exc


def _brave_request(config: Config, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    if not config.brave_api_key:
        raise WebOpsNotConfigured("web_ops_not_configured: BRAVE_SEARCH_API_KEY is not set.")
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{config.brave_base_url.rstrip('/')}{endpoint}?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "X-Subscription-Token": config.brave_api_key,
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        return _open_json(req, timeout=30, provider="Brave")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Brave request failed: HTTP {exc.code}: {text}") from exc


def _normalize_search_result(item: dict[str, Any], source: str) -> dict[str, Any]:
    url = str(item.get("url") or item.get("link") or "")
    title = str(item.get("title") or item.get("name") or url)
    snippet = str(item.get("description") or item.get("snippet") or item.get("content") or "")
    return {
        "title": title,
        "url": url,
        "link": url,
        "snippet": snippet,
        "content": snippet,
        "source": source,
    }


def _firecrawl_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data", payload)
    if isinstance(data, dict):
        for key in ("web", "results", "items"):
            if isinstance(data.get(key), list):
                return [item for item in data[key] if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _brave_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    web = payload.get("web")
    if isinstance(web, dict) and isinstance(web.get("results"), list):
        return [item for item in web["results"] if isinstance(item, dict)]
    return []


def search(config: Config, query: str, limit: int = 5, **options: Any) -> dict[str, Any]:
    payload = {"query": query, "limit": max(1, min(limit, 20)), **{k: v for k, v in options.items() if v is not None}}
    return _firecrawl_request(config, "/v2/search", payload)


def brave_search(config: Config, query: str, limit: int = 5, **options: Any) -> dict[str, Any]:
    params = {
        "q": query,
        "count": max(1, min(limit, 20)),
        "extra_snippets": "true",
        **{k: v for k, v in options.items() if v is not None},
    }
    return _brave_request(config, "/web/search", params)


def combined_search(
    config: Config,
    query: str,
    limit: int = 5,
    *,
    engine: str = "brave",
    fetch_content: bool = False,
    fetch_limit: int = 3,
) -> dict[str, Any]:
    engine = (engine or "brave").strip().lower()
    selected_engine = engine
    if engine in {"brave", "brave_search"}:
        raw = brave_search(config, query, limit)
        results = [_normalize_search_result(item, "brave") for item in _brave_items(raw)]
        selected_engine = "brave"
    elif engine in {"firecrawl", "fc"}:
        raw = search(config, query, limit)
        results = [_normalize_search_result(item, "firecrawl") for item in _firecrawl_items(raw)]
        selected_engine = "firecrawl"
    elif engine == "auto":
        try:
            raw = brave_search(config, query, limit)
            results = [_normalize_search_result(item, "brave") for item in _brave_items(raw)]
            if not results:
                raise RuntimeError("Brave returned no web results.")
            selected_engine = "brave"
        except (WebOpsNotConfigured, RuntimeError):
            raw = search(config, query, limit)
            results = [_normalize_search_result(item, "firecrawl") for item in _firecrawl_items(raw)]
            selected_engine = "firecrawl"
    else:
        raise ValueError("engine must be one of: brave, firecrawl, auto")

    if fetch_content:
        for result in results[: max(0, min(fetch_limit, len(results)))]:
            if not result["url"]:
                continue
            scraped = scrape(config, result["url"], formats=["markdown"])
            data = scraped.get("data", scraped)
            if isinstance(data, dict):
                markdown = data.get("markdown") or data.get("content")
                if markdown:
                    result["markdown"] = markdown

    return {"query": query, "engine": selected_engine, "results": results, "raw": raw}


def scrape(config: Config, url: str, formats: list[str] | None = None, **options: Any) -> dict[str, Any]:
    payload = {"url": url, "formats": formats or ["markdown"], **{k: v for k, v in options.items() if v is not None}}
    return _firecrawl_request(config, "/v2/scrape", payload)


def crawl(config: Config, url: str, limit: int = 10, max_depth: int = 2, **options: Any) -> dict[str, Any]:
    payload = {
        "url": url,
        "limit": max(1, min(limit, 100)),
        "maxDepth": max(1, min(max_depth, 10)),
        **{k: v for k, v in options.items() if v is not None},
    }
    return _firecrawl_request(config, "/v2/crawl", payload)


def map_site(config: Config, url: str, limit: int = 100, **options: Any) -> dict[str, Any]:
    payload = {"url": url, "limit": max(1, min(limit, 1000)), **{k: v for k, v in options.items() if v is not None}}
    return _firecrawl_request(config, "/v2/map", payload)


def extract(config: Config, urls: list[str], prompt: str | None = None, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"urls": urls}
    if prompt:
        payload["prompt"] = prompt
    if schema:
        payload["schema"] = schema
    return _firecrawl_request(config, "/v2/extract", payload)


def interact(config: Config, url: str, prompt: str | None = None, actions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    # Firecrawl v2 interaction is attached to a scrape session. Create the scrape first.
    scraped = scrape(config, url, formats=["markdown"], actions=actions)
    scrape_id = scraped.get("scrapeId") or scraped.get("id") or scraped.get("data", {}).get("scrapeId")
    if not scrape_id:
        return {"scrape": scraped, "interact": None, "warning": "No scrapeId returned by Firecrawl."}
    payload = {"prompt": prompt or "Interact with the page and return the relevant extracted content."}
    return {"scrape": scraped, "interact": _firecrawl_request(config, f"/v2/scrape/{scrape_id}/interact", payload)}
