from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from test_mcpc_acceptance import _exercise_roundtrip


LOCAL_BASE = "http://127.0.0.1:8766"


@pytest.mark.parametrize("exposure", ["full", "compact"])
def test_protocol_probe(tmp_path, exposure, request):
    _exercise_roundtrip(tmp_path, exposure, request)


def _get_json(path: str):
    with urllib.request.urlopen(LOCAL_BASE + path, timeout=3) as response:
        return response.status, dict(response.headers), json.loads(response.read().decode("utf-8"))


def _post_mcp(method: str, params: dict, request_id: int = 1):
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }).encode("utf-8")
    request = urllib.request.Request(
        LOCAL_BASE + "/mcp",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers), json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, dict(exc.headers), json.loads(raw) if raw else {}


def _call_tool(name: str, arguments: dict, request_id: int):
    status, _, payload = _post_mcp(
        "tools/call",
        {"name": name, "arguments": arguments},
        request_id=request_id,
    )
    assert status == 200, payload
    assert "error" not in payload, payload
    result = payload.get("result")
    assert isinstance(result, dict), payload
    return result


def test_restarted_local_service_health_and_auth_boundary():
    status, _, payload = _get_json("/health")
    assert status == 200
    assert payload.get("ok") is True
    print("LIVE_LOCAL_HEALTH", json.dumps(payload, ensure_ascii=False, sort_keys=True))

    status, _, protected = _get_json("/.well-known/oauth-protected-resource")
    assert status == 200
    assert protected.get("resource", "").endswith("/mcp")

    status, headers, payload = _post_mcp(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "restart-probe", "version": "1"},
        },
    )
    print("LIVE_LOCAL_MCP_INITIALIZE", json.dumps({
        "status": status,
        "www_authenticate": headers.get("WWW-Authenticate"),
        "body": payload,
    }, ensure_ascii=False, sort_keys=True))

    if status == 401:
        assert "Bearer" in headers.get("WWW-Authenticate", "")
        pytest.skip("live service requires bearer auth; health/auth boundary verified")

    assert status == 200, payload
    assert payload.get("result", {}).get("serverInfo", {}).get("name") == "mcp4chatgpt"

    list_status, _, listed = _post_mcp("tools/list", {}, request_id=2)
    assert list_status == 200, listed
    tools = listed.get("result", {}).get("tools", [])
    by_name = {item.get("name"): item for item in tools}
    assert "capability_list" not in by_name
    search_schema = by_name["capability_search"]["inputSchema"]["properties"]
    assert {"source", "backend", "category"} <= set(search_schema)
    print("LIVE_LOCAL_TOOL_COUNT", len(tools))


def test_restarted_live_discovery_contract():
    search = _call_tool(
        "capability_search",
        {"query": "PDF 编辑", "category": "pdf", "limit": 5},
        10,
    )
    search_data = search.get("structuredContent")
    assert isinstance(search_data, dict), search
    matches = search_data["matches"]
    assert matches and matches[0]["name"] == "pdf_insert_text"

    local_get = _call_tool("capability_get", {"name": "pdf_insert_text"}, 11)
    local_data = local_get.get("structuredContent")
    assert local_data["category"] == "pdf"
    assert local_data["availability"] == {"status": "unknown", "evidence": "not_checked"}
    assert local_data["examples"]
    assert "MCP4ChatGPT.pdf_insert_text" in local_data["aliases"]

    first = _call_tool("capability_list", {"category": "pdf", "limit": 2}, 12)
    first_data = first.get("structuredContent")
    assert len(first_data["items"]) == 2
    assert first_data["has_more"] is True
    cursor = first_data["next_cursor"]
    second = _call_tool("capability_list", {"cursor": cursor}, 13)
    second_data = second.get("structuredContent")
    names = [item["name"] for item in first_data["items"] + second_data["items"]]
    assert len(names) == len(set(names)) == 4

    downstream_search = _call_tool(
        "capability_search",
        {"query": "navigate", "backend": "chrome_devtools", "limit": 5},
        14,
    )
    downstream_matches = downstream_search["structuredContent"]["matches"]
    assert downstream_matches, downstream_search
    downstream_name = downstream_matches[0]["name"]
    downstream_get = _call_tool("capability_get", {"name": downstream_name}, 15)
    downstream_data = downstream_get["structuredContent"]
    assert downstream_data["backend_id"] == "chrome_devtools"
    assert downstream_data["availability"] == {
        "status": "running",
        "evidence": "downstream_manager",
    }

    called = _call_tool(
        "capability_call",
        {"name": "server_info", "arguments": {}},
        16,
    )
    called_data = called.get("structuredContent")
    assert isinstance(called_data, dict)
    assert "tool_catalog" in called_data

    print("LIVE_DISCOVERY", json.dumps({
        "pdf_top": matches[0]["name"],
        "pdf_pages": names,
        "downstream_tool": downstream_name,
        "downstream_availability": downstream_data["availability"],
        "catalog_version": local_data["catalog_version"],
    }, ensure_ascii=False, sort_keys=True))
