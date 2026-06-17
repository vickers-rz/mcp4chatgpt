from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import Config


class WebOpsNotConfigured(RuntimeError):
    pass


def _request(config: Config, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Firecrawl request failed: HTTP {exc.code}: {text}") from exc


def search(config: Config, query: str, limit: int = 5, **options: Any) -> dict[str, Any]:
    payload = {"query": query, "limit": max(1, min(limit, 20)), **{k: v for k, v in options.items() if v is not None}}
    return _request(config, "/v2/search", payload)


def scrape(config: Config, url: str, formats: list[str] | None = None, **options: Any) -> dict[str, Any]:
    payload = {"url": url, "formats": formats or ["markdown"], **{k: v for k, v in options.items() if v is not None}}
    return _request(config, "/v2/scrape", payload)


def crawl(config: Config, url: str, limit: int = 10, max_depth: int = 2, **options: Any) -> dict[str, Any]:
    payload = {
        "url": url,
        "limit": max(1, min(limit, 100)),
        "maxDepth": max(1, min(max_depth, 10)),
        **{k: v for k, v in options.items() if v is not None},
    }
    return _request(config, "/v2/crawl", payload)


def map_site(config: Config, url: str, limit: int = 100, **options: Any) -> dict[str, Any]:
    payload = {"url": url, "limit": max(1, min(limit, 1000)), **{k: v for k, v in options.items() if v is not None}}
    return _request(config, "/v2/map", payload)


def extract(config: Config, urls: list[str], prompt: str | None = None, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"urls": urls}
    if prompt:
        payload["prompt"] = prompt
    if schema:
        payload["schema"] = schema
    return _request(config, "/v2/extract", payload)


def interact(config: Config, url: str, prompt: str | None = None, actions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    # Firecrawl v2 interaction is attached to a scrape session. Create the scrape first.
    scraped = scrape(config, url, formats=["markdown"], actions=actions)
    scrape_id = scraped.get("scrapeId") or scraped.get("id") or scraped.get("data", {}).get("scrapeId")
    if not scrape_id:
        return {"scrape": scraped, "interact": None, "warning": "No scrapeId returned by Firecrawl."}
    payload = {"prompt": prompt or "Interact with the page and return the relevant extracted content."}
    return {"scrape": scraped, "interact": _request(config, f"/v2/scrape/{scrape_id}/interact", payload)}
