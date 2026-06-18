from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .config import Config


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _sign(config: Config, payload: str) -> str:
    return _b64(hmac.new(config.auth_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest())


def _clients_path(config: Config) -> Path:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    return config.data_dir / "oauth_clients.json"


def _quarantine_corrupt_clients(path: Path) -> None:
    if not path.exists():
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.corrupt.{stamp}")
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.name}.corrupt.{stamp}.{counter}")
        counter += 1
    try:
        path.rename(target)
    except OSError:
        return


def _load_clients(config: Config) -> dict[str, Any]:
    path = _clients_path(config)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # Isolate a corrupted clients file before treating it as empty.
        _quarantine_corrupt_clients(path)
        return {}
    if not isinstance(data, dict):
        _quarantine_corrupt_clients(path)
        return {}
    return data


def _save_clients(config: Config, clients: dict[str, Any]) -> None:
    _clients_path(config).write_text(json.dumps(clients, ensure_ascii=False, indent=2), encoding="utf-8")


AUTH_CODE_TTL_SECONDS = 600
MAX_AUTH_CODES = 1024
AUTH_CODES: dict[str, dict[str, Any]] = {}


def _cleanup_expired_codes(now: float | None = None) -> None:
    now = now or time.time()
    expired = [code for code, record in AUTH_CODES.items() if now - float(record.get("created_at", 0)) > AUTH_CODE_TTL_SECONDS]
    for code in expired:
        AUTH_CODES.pop(code, None)


def issuer(config: Config) -> str:
    return config.public_base_url.rstrip("/")


def metadata(config: Config) -> dict[str, Any]:
    base = issuer(config)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
    }


def protected_resource_metadata(config: Config) -> dict[str, Any]:
    base = issuer(config)
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
    }


def register_client(config: Config, payload: dict[str, Any]) -> dict[str, Any]:
    # Dynamic registration keeps ChatGPT setup friction low. Registered clients
    # are persisted, while short-lived authorization codes remain in memory.
    client_id = "client_" + secrets.token_urlsafe(18)
    client_secret = secrets.token_urlsafe(32)
    clients = _load_clients(config)
    clients[client_id] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_name": payload.get("client_name", "ChatGPT"),
        "redirect_uris": payload.get("redirect_uris", []),
        "created_at": time.time(),
    }
    _save_clients(config, clients)
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_id_issued_at": int(time.time()),
        "client_secret_expires_at": 0,
        "redirect_uris": clients[client_id]["redirect_uris"],
        "token_endpoint_auth_method": "client_secret_post",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }


_HTML_ESCAPE = str.maketrans({"&": "&amp;", '"': "&quot;", "'": "&#x27;", "<": "&lt;", ">": "&gt;"})


def _he(s: str) -> str:
    """HTML-escape a string for safe use in attribute values and text nodes."""
    return s.translate(_HTML_ESCAPE)


# OAuth params that are allowed through to the hidden form fields.
_AUTHORIZE_FORM_PARAMS = frozenset({
    "client_id", "redirect_uri", "response_type", "scope",
    "state", "code_challenge", "code_challenge_method", "nonce",
})


def render_authorize_form(params: dict[str, str]) -> bytes:
    # Only forward known OAuth params to avoid reflective injection via
    # unexpected query-string keys.
    fields = "\n".join(
        f'<input type="hidden" name="{_he(k)}" value="{_he(str(v))}">'
        for k, v in params.items()
        if k in _AUTHORIZE_FORM_PARAMS
    )
    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>MCP4ChatGPT Authorization</title></head>
<body>
<h1>MCP4ChatGPT Authorization</h1>
<p>Enter MCP_AUTH_SECRET to authorize this ChatGPT connector.</p>
<form method="post" action="/oauth/authorize">
{fields}
<label>Admin secret <input name="admin_secret" type="password" autofocus></label>
<button type="submit">Authorize</button>
</form>
</body>
</html>"""
    return html.encode("utf-8")


def create_auth_redirect(config: Config, params: dict[str, str], admin_secret: str) -> str:
    if not hmac.compare_digest(admin_secret, config.auth_secret):
        raise ValueError("Invalid admin secret.")
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    if not client_id or not redirect_uri:
        raise ValueError("client_id and redirect_uri are required.")
    clients = _load_clients(config)
    client = clients.get(client_id)
    if not client:
        raise ValueError("Unknown OAuth client.")
    if client.get("redirect_uris") and redirect_uri not in client["redirect_uris"]:
        raise ValueError("redirect_uri is not registered for this client.")
    _cleanup_expired_codes()
    if len(AUTH_CODES) >= MAX_AUTH_CODES:
        raise ValueError("Too many pending authorization codes; retry later.")
    code = secrets.token_urlsafe(32)
    AUTH_CODES[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": params.get("code_challenge"),
        "code_challenge_method": params.get("code_challenge_method", "plain"),
        "created_at": time.time(),
    }
    query = {"code": code}
    if params.get("state"):
        query["state"] = params["state"]
    return redirect_uri + ("&" if "?" in redirect_uri else "?") + urlencode(query)


def issue_token(config: Config, payload: dict[str, Any]) -> dict[str, Any]:
    code = payload.get("code", "")
    client_id = payload.get("client_id", "")
    # Use get() first so we can distinguish "unknown code" from "expired code"
    # before removing the entry.  _cleanup runs after to purge other stale codes.
    record = AUTH_CODES.get(code)
    if not record or record["client_id"] != client_id:
        raise ValueError("Invalid authorization code.")
    if time.time() - float(record.get("created_at", 0)) > AUTH_CODE_TTL_SECONDS:
        AUTH_CODES.pop(code, None)
        raise ValueError("Authorization code has expired.")
    AUTH_CODES.pop(code, None)  # consume — single use
    _cleanup_expired_codes()    # opportunistic cleanup of other stale codes
    verifier = payload.get("code_verifier")
    challenge = record.get("code_challenge")
    if challenge:
        if not verifier:
            raise ValueError("code_verifier is required when code_challenge was set.")
        method = record.get("code_challenge_method", "plain")
        if method not in {"plain", "S256"}:
            raise ValueError("Unsupported code_challenge_method.")
        expected = _b64(hashlib.sha256(verifier.encode("utf-8")).digest()) if method == "S256" else verifier
        if not hmac.compare_digest(expected, challenge):
            raise ValueError("Invalid PKCE verifier.")
    now = int(time.time())
    claims = {"sub": client_id, "iat": now, "exp": now + 86400}
    body = _b64(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    # The token is a compact HMAC-signed payload, not a JWT. It is sufficient
    # for this single-service connector and avoids adding a dependency.
    token = body + "." + _sign(config, body)
    return {"access_token": token, "token_type": "Bearer", "expires_in": 86400, "scope": "local web knowledge"}


def verify_token(config: Config, token: str) -> str:
    if not token or "." not in token:
        raise ValueError("Missing bearer token.")
    body, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(config, body), signature):
        raise ValueError("Invalid bearer token signature.")
    padded = body + "=" * (-len(body) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    if int(claims.get("exp", 0)) < int(time.time()):
        raise ValueError("Bearer token expired.")
    return str(claims.get("sub", ""))
