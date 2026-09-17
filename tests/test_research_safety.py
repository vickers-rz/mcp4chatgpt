from __future__ import annotations

import socket
from unittest import mock

import pytest

from mcp4chatgpt.safety import validate_research_url


def test_research_url_allows_local_and_private_targets_by_default(monkeypatch):
    monkeypatch.delenv("MCP_BROWSER_RESEARCH_STRICT_NETWORK", raising=False)
    monkeypatch.delenv("MCP_BROWSER_RESEARCH_ALLOW_HOSTS", raising=False)
    for url in [
        "http://localhost/",
        "http://router.local/",
        "http://intranet/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://100.64.0.1/",
        "http://169.254.1.2/",
        "http://[::1]/",
        "http://[fe80::1]/",
    ]:
        assert validate_research_url(url, resolve_dns=False) == url


def test_research_url_strict_mode_rejects_local_and_private_literals(monkeypatch):
    monkeypatch.setenv("MCP_BROWSER_RESEARCH_STRICT_NETWORK", "1")
    monkeypatch.delenv("MCP_BROWSER_RESEARCH_ALLOW_HOSTS", raising=False)
    for url in [
        "http://localhost/",
        "http://router.local/",
        "http://intranet/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://169.254.1.2/",
        "http://[::1]/",
        "http://[fe80::1]/",
    ]:
        with pytest.raises(ValueError):
            validate_research_url(url, resolve_dns=False)


def test_research_url_rejects_dns_resolution_to_private_address(monkeypatch):
    monkeypatch.setenv("MCP_BROWSER_RESEARCH_STRICT_NETWORK", "1")
    monkeypatch.delenv("MCP_BROWSER_RESEARCH_ALLOW_HOSTS", raising=False)
    fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.9", 443))]
    with mock.patch("mcp4chatgpt.safety.socket.getaddrinfo", return_value=fake):
        with pytest.raises(ValueError, match="non-global"):
            validate_research_url("https://example.org/path")


def test_research_url_accepts_proxy_fake_ip_dns_but_not_literal(monkeypatch):
    monkeypatch.setenv("MCP_BROWSER_RESEARCH_STRICT_NETWORK", "1")
    monkeypatch.delenv("MCP_BROWSER_RESEARCH_ALLOW_HOSTS", raising=False)
    fake = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.32", 443)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:0:c612:20", 443, 0, 0)),
    ]
    with mock.patch("mcp4chatgpt.safety.socket.getaddrinfo", return_value=fake):
        assert validate_research_url("https://ipinfo.io/json") == "https://ipinfo.io/json"
    with pytest.raises(ValueError, match="non-global"):
        validate_research_url("https://198.18.0.32/", resolve_dns=False)


def test_research_url_accepts_global_dns_and_explicit_private_allowlist(monkeypatch):
    monkeypatch.setenv("MCP_BROWSER_RESEARCH_STRICT_NETWORK", "1")
    monkeypatch.delenv("MCP_BROWSER_RESEARCH_ALLOW_HOSTS", raising=False)
    fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    with mock.patch("mcp4chatgpt.safety.socket.getaddrinfo", return_value=fake):
        assert validate_research_url("https://example.org/path") == "https://example.org/path"

    monkeypatch.setenv("MCP_BROWSER_RESEARCH_ALLOW_HOSTS", "router.local,*.corp.internal")
    assert validate_research_url("http://router.local/", resolve_dns=False) == "http://router.local/"
    assert validate_research_url("https://docs.corp.internal/", resolve_dns=False) == "https://docs.corp.internal/"
