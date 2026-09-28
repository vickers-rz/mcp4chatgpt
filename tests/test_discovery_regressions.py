"""Regression evidence for the 2026-09-27 discovery review."""
import json
from dataclasses import replace

import pytest

from mcp4chatgpt.audit import AuditLogger
from mcp4chatgpt.tools import ToolRegistry
from test_capabilities import FakeDownstream
from test_core import make_config


def registry(tmp_path):
    config = make_config(tmp_path)
    manager = FakeDownstream()
    return ToolRegistry(config, AuditLogger(config.audit_log), downstream_manager=manager), manager


def test_mutable_downstream_schema_changes_both_versions(tmp_path):
    r, m = registry(tmp_path)
    versions = r.catalog_version, r.toolset_hash
    m.tools[0].input_schema['properties']['new_field'] = {'type': 'integer'}
    assert r.refresh_catalog()
    assert r.catalog_version != versions[0]
    assert r.toolset_hash != versions[1]
    tool = r._capability_get({'name': 'docs__lookup'})['tool']
    tool['inputSchema']['properties']['injected'] = {}
    assert 'injected' not in r._capability_get({'name': 'docs__lookup'})['tool']['inputSchema']['properties']


def test_unknown_schema_dialect_rejects_before_handler(tmp_path):
    r, m = registry(tmp_path)
    m.tools[0].input_schema['$schema'] = 'https://example.invalid/unknown'
    with pytest.raises(ValueError, match='capability_schema'):
        r.call_tool('capability_call', {'name': 'docs__lookup', 'arguments': {'query': 'x'}})
    assert not m.calls


def test_ref_named_properties_and_example_data_are_not_references(tmp_path):
    r, _ = registry(tmp_path)
    tool = replace(r.tools['server_info'], input_schema={
        'type': 'object', 'properties': {'$ref': {'type': 'string'}},
        'examples': [{'$ref': 'https://example.invalid/literal'}],
    })
    r._validate_capability_arguments(tool, {'$ref': 'literal'})


def test_downstream_error_result_is_failed_audit(tmp_path):
    r, m = registry(tmp_path)
    m.call_tool = lambda *_: {'content': [], 'isError': True}
    result = r.call_tool('capability_call', {'name': 'docs__lookup', 'arguments': {'query': 'x'}}, 'client')
    assert result['isError'] is True
    event = json.loads(r.config.audit_log.read_text().splitlines()[-1])
    assert event['ok'] is False
    assert event['tool'] == 'docs__lookup' and event['client_id'] == 'client'


def test_search_requires_all_terms_in_declared_search_fields(tmp_path):
    r, _ = registry(tmp_path)
    assert not r._capability_search({'query': 'documentation missingxyz'})['matches']
    assert not r._capability_search({'query': 'documentation query'})['matches']


def test_tools_list_and_audit_share_one_snapshot(tmp_path):
    import threading
    import urllib.request
    from mcp4chatgpt.server import create_server
    class Changing(FakeDownstream):
        def __init__(self):
            super().__init__()
            self.reads = 0
        def get_tools(self):
            self.reads += 1
            return super().get_tools() if self.reads <= 2 else []
    config = replace(make_config(tmp_path), local_auth_disabled=True)
    server = create_server(config, downstream_manager=Changing())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(f'http://127.0.0.1:{server.server_address[1]}/mcp',
            data=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}}).encode(),
            headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req) as response:
            result = json.load(response)['result']
        events = [json.loads(x) for x in config.audit_log.read_text().splitlines()]
        event = next(e for e in events if e.get('method') == 'tools/list')
        assert event['tool_count'] == len(result['tools'])
        assert event['toolset_hash'] == server.registry.toolset_hash
    finally:
        server.shutdown(); server.server_close(); thread.join(5)

@pytest.mark.parametrize('exposure', ['full', 'compact'])
def test_real_downstream_runtime_notification_updates_directory(tmp_path, exposure):
    import sys
    import time
    from pathlib import Path
    from mcp4chatgpt.downstream.manager import DownstreamMCPManager
    from test_downstream import _make_toml
    _make_toml(tmp_path, {'docs': {'command':sys.executable,
        'args':[str(Path(__file__).with_name('fake_dynamic_mcp_server.py'))],
        'deny_tools':['blocked'], 'startup_timeout':3.0}})
    m = DownstreamMCPManager()
    m.start_all(tmp_path)
    client = m._clients["docs"]
    try:
        c = replace(make_config(tmp_path), tool_exposure=exposure)
        r = ToolRegistry(c, AuditLogger(c.audit_log), downstream_manager=m)
        assert 'docs__lookup' in r._catalog_name_set
        assert 'docs__blocked' not in r._catalog_name_set
        versions = r.catalog_version
        for generation in (1, 2):
            r.call_tool('docs__advance', {})
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                r.refresh_catalog()
                if r.catalog_version != versions:
                    break
                time.sleep(.01)
            assert r.catalog_version != versions
            versions = r.catalog_version
            discovered = r._capability_get({'name':'docs__fetch'})
            assert discovered['availability'] == {'status': 'running', 'evidence': 'downstream_manager'}
            definition = discovered['tool']
            kind = 'string' if generation == 1 else 'integer'
            assert definition['inputSchema']['properties']['id']['type'] == kind
            assert 'docs__lookup' not in r._catalog_name_set
            assert 'docs__blocked' not in r._catalog_name_set
            assert r._capability_search({'query':'docs__fetch'})['matches'][0]['name'] == 'docs__fetch'
            uri = 'mcp4chatgpt://tools/docs__fetch'
            assert uri in {x['uri'] for x in r.list_tool_resources()['resources']}
            assert json.loads(r.read_tool_resource(uri, auth_required=False)['contents'][0]['text']) == definition_without_auth(definition)
            result = r.call_tool('capability_call', {'name':'docs__fetch', 'arguments':{'id':'x' if generation == 1 else 2}})
            assert result['structuredContent']['version'] == generation
        with pytest.raises(ValueError, match='Unknown tool'):
            r.call_tool('docs__lookup', {'id':'x'})
        with pytest.raises(ValueError, match='capability_arguments_invalid'):
            r.call_tool('capability_call', {'name':'docs__fetch','arguments':{'id':'wrong'}})
    finally:
        m.stop_all()
    assert client._catalog_refresh_task is None or client._catalog_refresh_task.done()
    assert not m.get_tools()


def definition_without_auth(definition):
    definition = json.loads(json.dumps(definition))
    definition.pop('securitySchemes', None)
    definition.get('_meta', {}).pop('securitySchemes', None)
    return definition


def test_call_audit_uses_channel_from_selected_tool_snapshot(tmp_path, monkeypatch):
    r, m = registry(tmp_path)
    validate = r._validate_capability_arguments
    def remove_after_selection(tool, arguments):
        validate(tool, arguments)
        if tool.name == "docs__lookup":
            m.tools = []
            r.refresh_catalog()
    monkeypatch.setattr(r, '_validate_capability_arguments', remove_after_selection)
    r.call_tool('capability_call', {'name':'docs__lookup','arguments':{'query':'x'}})
    event = json.loads(r.config.audit_log.read_text().splitlines()[-1])
    assert event['channel'] == 'docs'


def test_notification_during_failed_refresh_is_not_lost():
    import asyncio
    from mcp4chatgpt.downstream.client import StdioMCPClient, StdioMCPClientError
    client = StdioMCPClient(downstream_id='test', command='unused', args=[])
    calls = []
    async def discover():
        calls.append(1)
        if len(calls) == 1:
            client._tools_generation += 1
            raise StdioMCPClientError('old generation failed')
        return [{'name':'fresh','inputSchema':{}}]
    client._discover_tools = discover
    asyncio.run(client._refresh_tools())
    assert [t['name'] for t in client.tools] == ['fresh']
    assert client.catalog_error is None


def test_cancelled_catalog_write_releases_pending_request():
    import asyncio
    from types import SimpleNamespace
    from mcp4chatgpt.downstream.client import StdioMCPClient
    async def check():
        writing = asyncio.Event()
        class Stdin:
            def write(self, _):
                writing.set()
            async def drain(self):
                await asyncio.Event().wait()
        client = StdioMCPClient(downstream_id='test', command='unused', args=[])
        client._loop = asyncio.get_running_loop()
        client._process = SimpleNamespace(stdin=Stdin())
        task = asyncio.create_task(client._send_request('tools/list', {}))
        await writing.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not client._pending
    asyncio.run(check())
