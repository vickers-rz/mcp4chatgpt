from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace

from mcp4chatgpt import skill_resources
from mcp4chatgpt.server import create_server
from test_core import make_config


def _params(**values):
    return {
        **values,
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {
                "name": "skill-import-test",
                "version": "1.0.0",
            },
            "io.modelcontextprotocol/clientCapabilities": {},
        },
    }


def _headers(method: str, name: str | None = None) -> dict[str, str]:
    result = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
    }
    if name is not None:
        result["Mcp-Name"] = name
    return result


def _post(url: str, method: str, params: dict, *, name: str | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps({
            "jsonrpc": "2.0",
            "id": method,
            "method": method,
            "params": params,
        }).encode("utf-8"),
        headers=_headers(method, name),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        finally:
            exc.close()


def _post_standard(url: str, method: str, params: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps({
            "jsonrpc": "2.0",
            "id": method,
            "method": method,
            "params": params,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_standard_skill_methods_accept_empty_first_list_request(tmp_path):
    config = replace(make_config(tmp_path), local_auth_disabled=True)
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    endpoint = f"http://{host}:{port}/mcp"

    try:
        status, listed = _post_standard(endpoint, "skills/list", {})
        assert status == 200
        skill = listed["result"]["skills"][0]
        assert skill["uri"] == skill_resources.SKILL_URI

        status, fetched = _post_standard(
            endpoint, "skills/get", {"uri": skill_resources.SKILL_URI}
        )
        assert status == 200
        assert fetched["result"]["skill"] == skill

        status, resource = _post_standard(
            endpoint, "resources/read", {"uri": skill_resources.SKILL_URI}
        )
        assert status == 200
        assert resource["result"]["contents"][0]["uri"] == skill_resources.SKILL_URI
    finally:
        server.shutdown()
        server.server_close()


def test_modern_mcp_skill_import_contract_end_to_end(tmp_path):
    config = replace(make_config(tmp_path), local_auth_disabled=True)
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    endpoint = f"http://{host}:{port}/mcp"

    try:
        status, discover = _post(endpoint, "server/discover", _params())
        assert status == 200
        assert discover["result"]["capabilities"]["extensions"] == {
            "io.modelcontextprotocol/skills": {}
        }

        status, listed = _post(endpoint, "skills/list", _params())
        assert status == 200
        list_result = listed["result"]
        assert list_result["resultType"] == "complete"
        assert list_result["ttlMs"] == 0
        assert list_result["cacheScope"] == "private"
        assert "nextCursor" not in list_result
        assert len(list_result["skills"]) == 1
        catalog_entry = list_result["skills"][0]
        assert catalog_entry["uri"] == skill_resources.SKILL_URI

        status, fetched = _post(
            endpoint,
            "skills/get",
            _params(uri=skill_resources.SKILL_URI),
            name=skill_resources.SKILL_URI,
        )
        assert status == 200
        assert fetched["result"]["skill"] == catalog_entry

        status, resource = _post(
            endpoint,
            "resources/read",
            _params(uri=skill_resources.SKILL_URI),
            name=skill_resources.SKILL_URI,
        )
        assert status == 200
        contents = resource["result"]["contents"]
        assert len(contents) == 1
        assert contents[0]["uri"] == skill_resources.SKILL_URI
        assert contents[0]["mimeType"] == "text/markdown"
        digest = "sha256:" + hashlib.sha256(
            contents[0]["text"].encode("utf-8")
        ).hexdigest()
        assert catalog_entry["resources"] == [
            {"uri": skill_resources.SKILL_URI, "digest": digest}
        ]

        status, resources = _post(endpoint, "resources/list", _params())
        assert status == 200
        assert skill_resources.SKILL_URI in {
            item["uri"] for item in resources["result"]["resources"]
        }
    finally:
        server.shutdown()
        server.server_close()


def test_modern_skills_get_requires_matching_mcp_name_header(tmp_path):
    config = replace(make_config(tmp_path), local_auth_disabled=True)
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address

    try:
        status, response = _post(
            f"http://{host}:{port}/mcp",
            "skills/get",
            _params(uri=skill_resources.SKILL_URI),
        )
        assert status == 400
        assert response["error"]["code"] == -32020
    finally:
        server.shutdown()
        server.server_close()
