from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from mcp4chatgpt.oauth import issue_token
from mcp4chatgpt.server import create_server

from test_core import make_config


def post_json(url: str, payload: dict, token: str | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


class ServerTests(unittest.TestCase):
    def test_mcp_initialize_and_tools_list(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            server = create_server(config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            try:
                from mcp4chatgpt import oauth

                client_id = "client-test"
                code = "server-code"
                oauth.AUTH_CODES[code] = {"client_id": client_id, "redirect_uri": "https://example.test/cb", "created_at": time.time()}
                token = issue_token(config, {"code": code, "client_id": client_id})["access_token"]
                base = f"http://{host}:{port}"
                init = post_json(base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, token)
                self.assertEqual(init["result"]["serverInfo"]["name"], "mcp4chatgpt")
                tools = post_json(base + "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, token)
                names = {tool["name"] for tool in tools["result"]["tools"]}
                self.assertIn("knowledge_search", names)
                self.assertIn("web_scrape", names)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
