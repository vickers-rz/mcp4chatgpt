from __future__ import annotations

import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from mcp4chatgpt import oauth

_PROCESS_BARRIER = None


def _init_process_barrier(barrier):
    global _PROCESS_BARRIER
    _PROCESS_BARRIER = barrier


def _register_in_process(config, index):
    _PROCESS_BARRIER.wait()
    return oauth.register_client(config, {"client_name": f"process-{index}", "redirect_uris": []})["client_id"]


def test_parallel_registration_preserves_all_clients(tmp_path):
    config = SimpleNamespace(data_dir=tmp_path, public_base_url="http://localhost", auth_secret="test")
    barrier = Barrier(12)

    def register(index):
        barrier.wait()
        return oauth.register_client(config, {"client_name": f"client-{index}", "redirect_uris": []})

    with ThreadPoolExecutor(max_workers=12) as pool:
        clients = list(pool.map(register, range(12)))
    saved = json.loads((tmp_path / "oauth_clients.json").read_text())
    assert len(clients) == len(saved) == 12


def test_parallel_process_registration_preserves_all_clients(tmp_path):
    config = SimpleNamespace(data_dir=tmp_path, public_base_url="http://localhost", auth_secret="test")
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    with context.Pool(4, initializer=_init_process_barrier, initargs=(barrier,)) as pool:
        clients = pool.starmap(_register_in_process, [(config, i) for i in range(4)])
    saved = json.loads((tmp_path / "oauth_clients.json").read_text())
    assert len(clients) == len(saved) == 4


def test_failed_atomic_client_write_preserves_existing_file(tmp_path, monkeypatch):
    config = SimpleNamespace(data_dir=tmp_path, public_base_url="http://localhost", auth_secret="test")
    path = tmp_path / "oauth_clients.json"
    path.write_text('{"existing": {}}')
    monkeypatch.setattr(oauth.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk error")))
    with pytest.raises(OSError, match="disk error"):
        oauth.register_client(config, {"redirect_uris": []})
    assert json.loads(path.read_text()) == {"existing": {}}


def test_client_authentication_and_public_pkce(tmp_path):
    import base64
    import hashlib
    from urllib.parse import parse_qs, urlparse

    config = SimpleNamespace(data_dir=tmp_path, auth_secret="admin", mcp_url="https://example.test/mcp")
    redirect = "https://client.test/callback"
    for method in ("client_secret_post", "none"):
        client = oauth.register_client(config, {"redirect_uris": [redirect], "token_endpoint_auth_method": method})
        verifier = "a" * 43
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        params = {"client_id": client["client_id"], "redirect_uri": redirect, "code_challenge": challenge, "code_challenge_method": "S256"}
        code = parse_qs(urlparse(oauth.create_auth_redirect(config, params, "admin")).query)["code"][0]
        request = {"grant_type": "authorization_code", "client_id": client["client_id"], "redirect_uri": redirect, "code": code, "code_verifier": verifier}
        if method == "client_secret_post":
            with pytest.raises(ValueError, match="client secret"):
                oauth.issue_token(config, {**request, "client_secret": "wrong"})
            request["client_secret"] = client["client_secret"]
        else:
            assert "client_secret" not in client
            with pytest.raises(ValueError, match="PKCE"):
                oauth.issue_token(config, {**request, "code_verifier": "b" * 43})
        assert oauth.issue_token(config, request)["access_token"]
        with pytest.raises(ValueError, match="authorization code"):
            oauth.issue_token(config, request)
