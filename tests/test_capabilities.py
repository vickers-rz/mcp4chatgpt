from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from mcp4chatgpt.audit import AuditLogger
from mcp4chatgpt.config import load_config
from mcp4chatgpt.downstream.models import DownstreamToolInfo
from mcp4chatgpt.mcp_types import RawMCPToolResult
from mcp4chatgpt.tools import ToolRegistry
from test_core import make_config


class FakeDownstream:
    def __init__(self, tools=None):
        self.calls = []
        self.tools = tools if tools is not None else [
            DownstreamToolInfo(
                "lookup",
                "docs__lookup",
                "Search internal documentation records.",
                {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                "docs",
            )
        ]

    def get_tools(self):
        return list(self.tools)

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"content": [{"type": "image", "mimeType": "image/png", "data": "AA=="}],
                "structuredContent": {"found": True}}


@pytest.fixture
def registry(tmp_path):
    config = replace(make_config(tmp_path), computer_mode="off", tool_exposure="full")
    return ToolRegistry(config, AuditLogger(config.audit_log))


def test_full_default_and_compact_exposure_contract(tmp_path):
    config = replace(make_config(tmp_path), computer_mode="interact",
                     computer_allowed_apps=("com.example.App",), tool_exposure="full")
    full = ToolRegistry(config, AuditLogger(config.audit_log))
    names = [x["name"] for x in full.list_tools(auth_required=False)["tools"]]
    assert names[:2] == ["server_info", "pdf_inspect"]
    assert names[-3:] == ["capability_search", "capability_get", "capability_call"]
    assert "capability_list" not in names
    assert "local_read_text" in names
    compact = ToolRegistry(replace(config, tool_exposure="compact"), AuditLogger(config.audit_log))
    names = [x["name"] for x in compact.list_tools(auth_required=False)["tools"]]
    assert names == ["server_info", "capability_search", "capability_get", "capability_call"]
    assert any(n.startswith("computer_") for n in compact._catalog_names)
    assert not any(n.startswith("computer_") for n in names)
    assert "capability_list" not in names
    assert "local_read_text" not in names
    listed = compact.call_tool("capability_list", {"category": "pdf", "limit": 1})["structuredContent"]
    assert listed["items"] and listed["items"][0]["category"] == "pdf"
    full_size = len(json.dumps(full.list_tools(auth_required=False), sort_keys=True, separators=(",", ":")).encode())
    compact_size = len(json.dumps(compact.list_tools(auth_required=False), sort_keys=True, separators=(",", ":")).encode())
    assert compact_size <= full_size * .3
    path = compact.config.allowed_roots[0] / "kept.txt"
    path.write_text("still callable", encoding="utf-8")
    assert "still callable" in json.dumps(compact.call_tool("local_read_text", {"path": str(path)}))


def test_search_casefold_source_ranking_and_truncation(registry):
    got = registry.call_tool("capability_search", {"query": " PDF INSPECT ", "limit": 1})["structuredContent"]
    assert got["matches"][0]["name"] == "pdf_inspect" and not got["truncated"]
    manager = FakeDownstream()
    ds = ToolRegistry(registry.config, AuditLogger(registry.config.audit_log), downstream_manager=manager)
    got = ds.call_tool("capability_search", {"query": "internal documentation"})["structuredContent"]
    assert got["matches"][0]["source"] == "downstream:docs"
    got = ds.call_tool("capability_search", {"query": "documentation query"})["structuredContent"]
    assert not got["matches"]
    got = ds.call_tool("capability_search", {"query": "documentation query extra-term"})["structuredContent"]
    assert not got["matches"]
    assert ds.call_tool("capability_search", {"query": "pdf", "limit": 1})["structuredContent"]["truncated"]


def test_catalog_refresh_tracks_runtime_downstream_changes(tmp_path):
    manager = FakeDownstream()
    config = replace(make_config(tmp_path), computer_mode="off", tool_exposure="full")
    registry = ToolRegistry(config, AuditLogger(config.audit_log), downstream_manager=manager)
    original_version = registry.catalog_version
    assert "docs__lookup" in registry._catalog_name_set

    manager.tools = [
        DownstreamToolInfo(
            "fetch",
            "docs__fetch",
            "Fetch updated documentation records.",
            {
                "type": "object",
                "properties": {"document_id": {"type": "string"}},
                "required": ["document_id"],
            },
            "docs",
        )
    ]

    got = registry.call_tool(
        "capability_search",
        {"query": "updated documentation"},
    )["structuredContent"]
    assert got["matches"][0]["name"] == "docs__fetch"
    assert registry.catalog_version != original_version
    assert "docs__fetch" in registry._catalog_name_set
    assert "docs__lookup" not in registry._catalog_name_set
    listed = {tool["name"] for tool in registry.list_tools(auth_required=False)["tools"]}
    assert "docs__fetch" in listed and "docs__lookup" not in listed
    with pytest.raises(ValueError, match="capability_not_found"):
        registry.call_tool("capability_get", {"name": "docs__lookup"})


def test_get_excludes_aliases_and_recursive_discovery_tools(registry):
    got = registry.call_tool("capability_get", {"name": "server_info"})["structuredContent"]
    assert got["tool"]["name"] == "server_info"
    for name in ("MCP4ChatGPT.server_info", "web_search", "missing"):
        with pytest.raises(ValueError, match="capability_not_found"):
            registry.call_tool("capability_get", {"name": name})
    with pytest.raises(ValueError, match="capability_call_recursive"):
        registry.call_tool("capability_call", {"name": "capability_search", "arguments": {"query": "x"}})


def test_schema_validation_and_external_ref_rejection(registry):
    path = registry.config.allowed_roots[0] / "test.txt"
    path.write_text("safe", encoding="utf-8")
    result = registry.call_tool("capability_call", {"name": "local_read_text", "arguments": {"path": str(path)}})
    assert "safe" in json.dumps(result)
    for args in ({}, {"path": str(path), "extra": True}):
        with pytest.raises(ValueError, match="capability_arguments_invalid"):
            registry.call_tool("capability_call", {"name": "local_read_text", "arguments": args})
    original = registry.tools["local_read_text"]
    for ref_keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
        registry.tools["local_read_text"] = replace(original, input_schema={
            "type": "object", ref_keyword: "https://example.invalid/schema.json",
        })
        with pytest.raises(ValueError, match="external_ref_unsupported"):
            registry.call_tool("capability_call", {"name": "local_read_text", "arguments": {}})
    registry.tools["local_read_text"] = replace(original, input_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {"path": {"type": "string"}}, "type": "object",
        "properties": {"path": {"$ref": "#/$defs/path"}}, "required": ["path"],
    })
    with pytest.raises(ValueError, match="capability_arguments_invalid"):
        registry.call_tool("capability_call", {"name": "local_read_text", "arguments": {"path": 4}})
    registry.tools["local_read_text"] = replace(original, input_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "$ref": "#/$defs/missing", "properties": {},
    })
    with pytest.raises(ValueError, match="capability_schema_invalid"):
        registry.call_tool("capability_call", {"name": "local_read_text", "arguments": {}})


def test_catalog_hash_tracks_schema_and_ignores_object_key_order(registry):
    original = registry.catalog_version
    tool = registry.tools["server_info"]
    registry.tools["server_info"] = replace(tool, input_schema={"type": "object", "properties": {"b": {}, "a": {}}})
    changed = registry._catalog_hash()
    registry.tools["server_info"] = replace(tool, input_schema={"type": "object", "properties": {"a": {}, "b": {}}})
    same = registry._catalog_hash()
    assert original != changed and changed == same


def test_toolset_hash_tracks_schema_and_exposure(registry, tmp_path):
    original = registry.toolset_hash
    tool = registry.tools["server_info"]
    registry.tools["server_info"] = replace(tool, input_schema={"type": "object", "properties": {"x": {"type": "string"}}})
    changed = registry._definition_hash(registry._all_listed_names, auth_required=False,
                                        exposure=registry.tool_exposure)
    assert changed != original
    compact = ToolRegistry(replace(registry.config, tool_exposure="compact"),
                           AuditLogger(tmp_path / "compact.jsonl"))
    assert compact.toolset_hash != registry.toolset_hash


def test_downstream_schema_resource_is_readable(tmp_path):
    config = replace(make_config(tmp_path), computer_mode="off")
    registry = ToolRegistry(config, AuditLogger(config.audit_log), downstream_manager=FakeDownstream())
    uri = "mcp4chatgpt://tools/docs__lookup"
    assert uri in {r["uri"] for r in registry.list_tool_resources()["resources"]}
    assert "docs__lookup" in registry.read_tool_resource(uri, auth_required=False)["contents"][0]["text"]


def test_server_info_reports_catalog_and_exposure(registry):
    result = registry.call_tool("server_info", {})["structuredContent"]["tool_catalog"]
    assert result == {"exposure": "full", "capability_count": len(registry._catalog_names),
                      "listed_count": len(registry._all_listed_names), "catalog_version": registry.catalog_version}


def test_exposure_setting_defaults_and_rejects_invalid(monkeypatch):
    monkeypatch.delenv("MCP_TOOL_EXPOSURE", raising=False)
    assert load_config().tool_exposure == "full"
    monkeypatch.setenv("MCP_TOOL_EXPOSURE", "mystery")
    with pytest.raises(ValueError, match="MCP_TOOL_EXPOSURE"):
        load_config()


def test_capability_call_uses_conservative_effect_annotations(registry):
    annotations = registry.tools["capability_call"].definition(auth_required=False)["annotations"]
    assert annotations["readOnlyHint"] is False
    assert annotations["idempotentHint"] is False
    assert annotations["destructiveHint"] is True


def test_capability_call_invokes_once_and_records_target_context(tmp_path):
    config = replace(make_config(tmp_path), computer_mode="off")
    audit = AuditLogger(config.audit_log)
    registry = ToolRegistry(config, audit, downstream_manager=FakeDownstream())
    original = registry.tools["docs__lookup"]
    count = {"calls": 0}
    def handler(_config, _args):
        count["calls"] += 1
        return RawMCPToolResult({"content": [{"type": "image", "mimeType": "image/png", "data": "AA=="}],
                                 "structuredContent": {"ok": True}})
    registry.tools["docs__lookup"] = replace(original, handler=handler)
    result = registry.call_tool("capability_call", {
        "name": "docs__lookup", "arguments": {"query": "x"},
    }, client_id="client-123")
    assert count["calls"] == 1
    assert result["content"][0]["type"] == "image"
    event = json.loads(config.audit_log.read_text(encoding="utf-8").splitlines()[-1])
    assert {key: event[key] for key in ("tool", "client_id", "channel", "invoked_via")} == {
        "tool": "docs__lookup", "client_id": "client-123", "channel": "docs",
        "invoked_via": "capability_call",
    }


def test_capability_get_metadata_examples_and_output_schema(tmp_path):
    config = replace(make_config(tmp_path), computer_mode="off")
    downstream = FakeDownstream([DownstreamToolInfo(
        original_name="lookup",
        namespaced_name="docs__lookup",
        description="Look up controlled records.",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        downstream_id="docs",
        output_schema={"type": "object", "properties": {"found": {"type": "boolean"}}},
    )])
    registry = ToolRegistry(config, AuditLogger(config.audit_log), downstream_manager=downstream)

    pdf = registry._capability_get({"name": "pdf_insert_text"})
    assert pdf["category"] == "pdf"
    assert pdf["availability"] == {"status": "unknown", "evidence": "not_checked"}
    assert pdf["aliases"] == ["MCP4ChatGPT.pdf_insert_text"]
    assert pdf["deprecated"] is False and pdf["replacement"] is None
    assert pdf["examples"]
    for example in pdf["examples"]:
        registry._validate_capability_arguments(registry.tools["pdf_insert_text"], example)

    got = registry._capability_get({"name": "docs__lookup"})
    assert got["tool"]["outputSchema"] == {
        "type": "object", "properties": {"found": {"type": "boolean"}}
    }
    assert got["availability"]["status"] == "unknown"


def test_maintained_examples_cover_required_workflows_and_validate(registry):
    required = {
        "pdf_inspect", "pdf_redact_text", "pdf_insert_text",
        "local_read_text", "local_write_file", "local_apply_patch",
        "local_start_job", "local_job_status", "local_job_logs", "local_cancel_job",
    }
    for name in required:
        got = registry._capability_get({"name": name})
        assert got["examples"], name
        for example in got["examples"]:
            registry._validate_capability_arguments(registry.tools[name], example)

    for name in ("capability_search", "capability_get", "capability_list", "capability_call"):
        from mcp4chatgpt.capability_catalog import examples_for
        examples = examples_for(name)
        assert examples, name
        for example in examples:
            registry._validate_capability_arguments(registry.tools[name], example)


def test_weighted_search_filters_and_bilingual_queries(tmp_path):
    config = replace(make_config(tmp_path), computer_mode="off")
    downstream = FakeDownstream([
        DownstreamToolInfo(
            "navigate", "devtools__navigate", "Navigate a controlled browser target.",
            {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
            "chrome_devtools",
        ),
        DownstreamToolInfo(
            "navigate", "headless__navigate", "Navigate a controlled browser target.",
            {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
            "chrome_headless",
        ),
    ])
    registry = ToolRegistry(config, AuditLogger(config.audit_log), downstream_manager=downstream)

    assert registry._capability_search({"query": "PDF 编辑", "category": "pdf"})["matches"][0]["name"] == "pdf_insert_text"
    assert registry._capability_search({"query": "文件 写入", "source": "local"})["matches"][0]["name"] == "local_write_file"
    assert registry._capability_search({"query": "后台任务 start", "category": "jobs"})["matches"][0]["name"] == "local_start_job"
    assert registry._capability_search({
        "query": "chrome devtools navigate", "backend": "chrome_devtools",
    })["matches"][0]["name"] == "devtools__navigate"
    assert registry._capability_search({
        "query": "chrome headless navigate", "backend": "chrome_headless",
    })["matches"][0]["name"] == "headless__navigate"


def test_capability_list_pagination_filter_binding_and_catalog_invalidation(tmp_path):
    manager = FakeDownstream()
    config = replace(make_config(tmp_path), computer_mode="off")
    registry = ToolRegistry(config, AuditLogger(config.audit_log), downstream_manager=manager)

    first = registry._capability_list({"category": "pdf", "limit": 2})
    assert len(first["items"]) == 2 and first["has_more"] is True
    cursor = first["next_cursor"]
    second = registry._capability_list({"cursor": cursor})
    names = [item["name"] for item in first["items"] + second["items"]]
    assert len(names) == len(set(names)) == 4
    assert all(item["category"] == "pdf" for item in first["items"] + second["items"])

    with pytest.raises(ValueError, match="capability_cursor_mismatch"):
        registry._capability_list({"cursor": cursor, "category": "files"})

    manager.tools = [DownstreamToolInfo(
        "fetch", "docs__fetch", "Fetch a record.",
        {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        "docs",
    )]
    registry.refresh_catalog()
    with pytest.raises(ValueError, match="capability_catalog_changed"):
        registry._capability_list({"cursor": cursor})
