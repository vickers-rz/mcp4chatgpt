from __future__ import annotations

import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from mcp4chatgpt import oauth, safety
from mcp4chatgpt.jobs.tool_api import _workspace


@pytest.mark.parametrize("address", ["fd00::c612:1", "fe80::c612:1"])
def test_fake_ip_suffix_does_not_exempt_private_ipv6(monkeypatch, address):
    monkeypatch.setenv("MCP_BROWSER_RESEARCH_STRICT_NETWORK", "1")
    monkeypatch.delenv("MCP_BROWSER_RESEARCH_ALLOW_HOSTS", raising=False)
    with pytest.raises(ValueError, match="non-global"):
        safety.validate_research_url("https://review.example", resolved_addresses={address})


def test_token_resource_checked_before_code_consumption(tmp_path):
    config = SimpleNamespace(data_dir=tmp_path, auth_secret="test", mcp_url="https://test.example/mcp")
    redirect = "https://client.example/callback"
    client = oauth.register_client(config, {"redirect_uris": [redirect]})
    params = {"client_id": client["client_id"], "redirect_uri": redirect}
    code = parse_qs(urlparse(oauth.create_auth_redirect(config, params, "test")).query)["code"][0]
    request = {**params, "code": code, "grant_type": "authorization_code",
               "client_secret": client["client_secret"]}
    with pytest.raises(ValueError, match="resource"):
        oauth.issue_token(config, {**request, "resource": "https://other.example/mcp"})
    token = oauth.issue_token(config, {**request, "resource": config.mcp_url})["access_token"]
    assert oauth.verify_token(config, token) == client["client_id"]
    other = SimpleNamespace(auth_secret="test", mcp_url="https://other.example/mcp")
    with pytest.raises(ValueError, match="audience"):
        oauth.verify_token(other, token)


def test_legacy_token_without_audience_remains_valid():
    config = SimpleNamespace(auth_secret="test", mcp_url="https://test.example/mcp")
    body = oauth._b64(json.dumps({"sub": "legacy", "exp": int(time.time()) + 60}).encode())
    assert oauth.verify_token(config, body + "." + oauth._sign(config, body)) == "legacy"


def test_unrelated_missing_allowed_root(tmp_path):
    config = SimpleNamespace(allowed_roots=[tmp_path, tmp_path / "missing"])
    assert _workspace(config, str(tmp_path))[0] == tmp_path


def test_runtime_health_checks_host_before_downstream():
    from mcp4chatgpt.runtime import RuntimeHandler
    from unittest.mock import patch
    handler = SimpleNamespace(path="/health", server=SimpleNamespace(config=object()))
    with patch("mcp4chatgpt.runtime._host_allowed", return_value=False), patch(
        "mcp4chatgpt.runtime._json_response"
    ) as response:
        RuntimeHandler.do_GET(handler)
    assert response.call_args.args[1] == 421


@pytest.mark.parametrize("state,expected", [("Z", False), ("S", True)])
def test_group_permission_error_still_checks_process_state(monkeypatch, state, expected):
    from mcp4chatgpt.workspace import coding_tasks

    def denied(*args):
        raise PermissionError()

    monkeypatch.setattr(coding_tasks.os, "killpg", denied)
    monkeypatch.setattr(coding_tasks.subprocess, "run",
                        lambda *args, **kwargs: SimpleNamespace(stdout=f"123 {state}\n"))
    assert coding_tasks._validation_group_alive(123) is expected
