from dataclasses import replace
import json
import time

import pytest

from mcp4chatgpt.audit import AuditLogger
from mcp4chatgpt.tools import CallContext, Tool, ToolRegistry
from mcp4chatgpt.downstream.manager import DownstreamMCPManager
from mcp4chatgpt.downstream.client import StdioMCPClientError
from mcp4chatgpt.downstream.models import DownstreamToolInfo
from test_core import make_config


@pytest.fixture
def registry(tmp_path):
    config = make_config(tmp_path)
    return ToolRegistry(config, AuditLogger(config.audit_log))


@pytest.mark.parametrize("via", ["direct", "discovery", "alias"])
def test_invalid_arguments_never_reach_handler_and_are_audited(registry, via):
    calls = []
    original = registry.tools["local_read_text"]
    tool = replace(original, handler=lambda c, a: calls.append(a))
    registry.tools["local_read_text"] = registry.tools["MCP4ChatGPT.local_read_text"] = tool
    name, args = "local_read_text", {"path": 42}
    if via == "discovery":
        name, args = "capability_call", {"name": name, "arguments": args}
    elif via == "alias":
        name = "MCP4ChatGPT.local_read_text"
    with pytest.raises(ValueError, match="capability_arguments_invalid"):
        registry.call_tool(name, args)
    assert not calls
    records = [json.loads(line) for line in registry.config.audit_log.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["tool"] == "local_read_text"
    assert records[0]["stage"] == "admission" and not records[0]["ok"]


@pytest.mark.parametrize("args", [None, [], "", 0, False])
def test_falsey_non_objects_are_rejected(registry, args):
    with pytest.raises(ValueError, match="capability_arguments_invalid"):
        registry.call_tool("server_info", args)


def test_constraints_and_alias_resolution(registry):
    for context, error in [
        (CallContext(allowed_tools=frozenset()), "not_allowed"),
        (CallContext(deadline=time.monotonic() - 1), "deadline_exceeded"),
        (CallContext(expected_catalog_version="stale"), "catalog_changed"),
    ]:
        with pytest.raises(ValueError, match=error):
            registry.call_tool("MCP4ChatGPT.server_info", {}, context=context)
    result = registry.call_tool("MCP4ChatGPT.server_info", {}, context=CallContext(
        allowed_tools=frozenset({"server_info"}), run_id="test-run"))
    assert "structuredContent" in result
    records = [json.loads(line) for line in registry.config.audit_log.read_text().splitlines()]
    assert records[-1]["run_id"] == "test-run"


def test_admission_audit_does_not_leak_instance(registry):
    secret = "private-value-not-for-log"
    with pytest.raises(ValueError):
        registry.call_tool("pdf_inspect", {"path": "x", "max_chars": secret})
    assert secret not in registry.config.audit_log.read_text()


@pytest.mark.parametrize("change,error", [
    ({"original_name": "different"}, "capability_definition_changed"),
    ({"backend_instance_id": "new-instance"}, "capability_backend_changed"),
])
def test_downstream_recheck_rejects_changed_schema_before_dispatch(registry, change, error):
    manager = DownstreamMCPManager()
    old = DownstreamToolInfo("lookup", "docs__lookup", "lookup", {"type": "object"}, "docs")
    manager._tools = {old.namespaced_name: old}
    manager.get_tools = lambda: list(manager._tools.values())
    calls = []
    class Client:
        is_running = True
        def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return {"content": []}
    manager._clients = {"docs": Client()}
    r = ToolRegistry(registry.config, registry.audit, downstream_manager=manager)
    validate = r._validate_capability_arguments
    def change_after_validation(tool, arguments):
        validate(tool, arguments)
        manager._tools[old.namespaced_name] = replace(old, **change)
    r._validate_capability_arguments = change_after_validation
    with pytest.raises(StdioMCPClientError, match=error):
        r.call_tool(old.namespaced_name, {})
    assert calls == []


def test_binding_survives_unrelated_catalog_change_but_not_target_change(registry):
    from test_capabilities import FakeDownstream
    manager = FakeDownstream()
    r = ToolRegistry(registry.config, registry.audit, downstream_manager=manager)
    bindings = r.bind_capabilities(("docs__lookup",))
    context = CallContext(capability_bindings=bindings, allowed_tools=frozenset({"docs__lookup"}))
    previous_catalog = r.catalog_version
    manager.tools.append(DownstreamToolInfo("other", "docs__other", "other", {"type": "object"}, "docs"))
    r.call_tool("docs__lookup", {"query": "x"}, context=context)
    assert r.catalog_version != previous_catalog
    assert len(manager.calls) == 1
    manager.tools[0] = replace(manager.tools[0], description="new revision")
    with pytest.raises(ValueError, match="capability_revision_changed"):
        r.call_tool("docs__lookup", {"query": "x"}, context=context)
    assert len(manager.calls) == 1


def test_binding_does_not_grant_permission_or_allow_unbound_targets(registry):
    bindings = registry.bind_capabilities(("server_info",))
    with pytest.raises(ValueError, match="capability_not_allowed"):
        registry.call_tool("server_info", {}, context=CallContext(
            capability_bindings=bindings, allowed_tools=frozenset()))
    with pytest.raises(ValueError, match="capability_not_bound"):
        registry.call_tool("pdf_inspect", {"path": "x"}, context=CallContext(capability_bindings=bindings))


def test_revision_round_trip_and_source_metadata(registry):
    from test_capabilities import FakeDownstream
    manager = FakeDownstream()
    r = ToolRegistry(registry.config, registry.audit, downstream_manager=manager)
    definition = r.call_tool("capability_get", {"name": "docs__lookup"})["structuredContent"]
    assert definition["canonical_name"] == "docs__lookup"
    assert definition["source"] == "downstream:docs" and definition["backend_id"] == "docs"
    args = {"name": "docs__lookup", "arguments": {"query": "x"},
            "expected_revision": definition["capability_revision"]}
    r.call_tool("capability_call", args)
    manager.tools[0] = replace(manager.tools[0], original_name="new_target")
    with pytest.raises(ValueError, match="capability_revision_changed"):
        r.call_tool("capability_call", args)
    assert len(manager.calls) == 1
    events = [json.loads(line) for line in registry.config.audit_log.read_text().splitlines()]
    assert events[-1]["error_code"] == "capability_revision_changed"


def test_bound_local_alias_uses_canonical_revision(registry):
    bindings = registry.bind_capabilities(("server_info",))
    assert registry.call_tool("MCP4ChatGPT.server_info", {}, context=CallContext(
        capability_bindings=bindings))["structuredContent"]["name"] == "mcp4chatgpt"


def test_real_downstream_restart_invalidates_binding_with_identical_schema(registry):
    import sys
    from pathlib import Path
    from mcp4chatgpt.downstream.models import DownstreamConfig
    cfg = DownstreamConfig(id="docs", command=sys.executable,
                           args=[str(Path(__file__).with_name("fake_mcp_server.py"))])
    manager = DownstreamMCPManager()
    manager._configs[cfg.id] = cfg
    try:
        manager._start_one(cfg)
        r = ToolRegistry(registry.config, registry.audit, downstream_manager=manager)
        context = CallContext(capability_bindings=r.bind_capabilities(("docs__list_pages",)))
        before = r.call_tool("capability_get", {"name": "docs__list_pages"})["structuredContent"]
        r.call_tool("docs__list_pages", {}, context=context)
        manager._clients[cfg.id].stop()
        manager._start_one(cfg)
        after = r.call_tool("capability_get", {"name": "docs__list_pages"})["structuredContent"]
        assert before["tool"] == after["tool"]
        assert before["backend_instance_id"] != after["backend_instance_id"]
        with pytest.raises(ValueError, match="capability_revision_changed"):
            r.call_tool("docs__list_pages", {}, context=context)
        assert r.call_tool("docs__list_pages", {})
    finally:
        manager.stop_all()


def test_transport_rejects_stale_instance_before_writing():
    import asyncio
    from mcp4chatgpt.downstream.client import StdioMCPClient
    client = StdioMCPClient(downstream_id="docs", command="unused", args=[])
    # No process exists: the binding check must precede all transport access.
    with pytest.raises(StdioMCPClientError, match="capability_backend_changed"):
        asyncio.run(client._async_call_tool("lookup", {}, 1, "stale-instance"))


def test_same_client_restart_changes_incarnation():
    import sys
    from pathlib import Path
    from mcp4chatgpt.downstream.client import StdioMCPClient
    client = StdioMCPClient(downstream_id="docs", command=sys.executable,
                           args=[str(Path(__file__).with_name("fake_mcp_server.py"))])
    try:
        client.start()
        previous = client.instance_id
        client.stop()
        client.start()
        assert client.instance_id != previous
        with pytest.raises(StdioMCPClientError, match="capability_backend_changed"):
            client.call_tool("list_pages", {}, expected_instance_id=previous)
        assert client.call_tool("list_pages", {}, expected_instance_id=client.instance_id)
    finally:
        client.stop()
