from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from mcp4chatgpt.audit import AuditLogger
from mcp4chatgpt.config import Config
from mcp4chatgpt import knowledge_ops, local_ops, web_ops
from mcp4chatgpt.oauth import issue_token, register_client, verify_token
from mcp4chatgpt.safety import resolve_allowed_path, validate_command
from mcp4chatgpt.tools import ToolRegistry


def make_config(tmp: Path) -> Config:
    root = tmp / "root"
    root.mkdir()
    return Config(
        public_base_url="http://127.0.0.1:8766",
        bind_host="127.0.0.1",
        bind_port=0,
        auth_secret="test-secret",
        allowed_roots=[root],
        co_te_path=tmp / "co-te.py",
        data_dir=tmp / "data",
        audit_log=tmp / "logs" / "audit.jsonl",
        firecrawl_api_key="",
        firecrawl_base_url="https://api.firecrawl.dev",
        knowledge_roots=[root],
        knowledge_store_dir=tmp / "knowledge",
        tls_cert_path="",
        tls_key_path="",
        max_output_chars=10000,
        log_rotate_bytes=20 * 1024 * 1024,
        log_retention_days=30,
        allowed_hosts=["localhost", "127.0.0.1", "::1"],
    )


class CoreTests(unittest.TestCase):
    def test_path_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            inside = config.allowed_roots[0] / "a.txt"
            self.assertEqual(resolve_allowed_path(str(inside), config.allowed_roots), inside.resolve())
            with self.assertRaises(ValueError):
                resolve_allowed_path("/etc/passwd", config.allowed_roots, must_exist=True)

    def test_dangerous_command(self) -> None:
        with self.assertRaises(ValueError):
            validate_command("sudo rm -rf /")
        self.assertEqual(validate_command("printf hello"), "printf hello")

    def test_local_file_and_command(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            path = config.allowed_roots[0] / "note.txt"
            local_ops.write_file(config, str(path), "hello world", overwrite=False)
            self.assertIn("hello", local_ops.read_text(config, str(path))["text"])
            result = local_ops.run_command(config, "printf hello", cwd=str(config.allowed_roots[0]))
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["stdout"], "hello")
            command_log = config.audit_log.parent / "commands.jsonl"
            self.assertTrue(command_log.exists())
            entry = json.loads(command_log.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(entry["tool"], "local_run_command")
            self.assertEqual(entry["command"], "printf hello")
            self.assertEqual(entry["stdout"], "hello")
            self.assertEqual(entry["exit_code"], 0)
            self.assertTrue(entry["ok"])

    def test_failed_local_command_writes_log(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            result = local_ops.run_command(config, "printf nope >&2; exit 7", cwd=str(config.allowed_roots[0]))
            self.assertEqual(result["exit_code"], 7)
            command_log = config.audit_log.parent / "commands.jsonl"
            entry = json.loads(command_log.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(entry["exit_code"], 7)
            self.assertFalse(entry["ok"])
            self.assertIn("nope", entry["stderr"])

    def test_local_command_log_truncates_long_output(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = replace(make_config(Path(d)), max_output_chars=20)
            local_ops.run_command(config, "printf 123456789012345678901234567890", cwd=str(config.allowed_roots[0]))
            command_log = config.audit_log.parent / "commands.jsonl"
            entry = json.loads(command_log.read_text(encoding="utf-8").splitlines()[-1])
            self.assertTrue(entry["truncated"])
            self.assertIn("[truncated]", entry["stdout"])

    def test_timeout_local_command_writes_log(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            with self.assertRaises(subprocess.TimeoutExpired):
                local_ops.run_command(config, "sleep 2", cwd=str(config.allowed_roots[0]), timeout_sec=1)
            command_log = config.audit_log.parent / "commands.jsonl"
            entry = json.loads(command_log.read_text(encoding="utf-8").splitlines()[-1])
            self.assertIsNone(entry["exit_code"])
            self.assertFalse(entry["ok"])
            self.assertEqual(entry["timeout_sec"], 1)
            self.assertIn("timed out", entry["stderr"])

    def test_local_command_log_redacts_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            local_ops.run_command(config, "printf 'Authorization: Bearer abc123'", cwd=str(config.allowed_roots[0]))
            command_log = config.audit_log.parent / "commands.jsonl"
            entry = json.loads(command_log.read_text(encoding="utf-8").splitlines()[-1])
            self.assertNotIn("abc123", entry["command"])
            self.assertNotIn("abc123", entry["stdout"])
            self.assertIn("[REDACTED]", entry["command"])
            self.assertIn("[REDACTED]", entry["stdout"])

    def test_local_command_log_tail_returns_recent_logs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            empty = local_ops.tail_command_log(config, limit=20)
            self.assertEqual(empty["entries"], [])
            self.assertEqual(empty["order"], "oldest_to_newest")

            local_ops.run_command(config, "printf first", cwd=str(config.allowed_roots[0]))
            local_ops.run_command(config, "printf second", cwd=str(config.allowed_roots[0]))

            tailed = local_ops.tail_command_log(config, limit=1)
            self.assertEqual(tailed["order"], "oldest_to_newest")
            self.assertEqual(len(tailed["entries"]), 1)
            self.assertEqual(tailed["entries"][0]["stdout"], "second")

    def test_local_command_log_tail_skips_malformed_lines(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            command_log = config.audit_log.parent / "commands.jsonl"
            command_log.parent.mkdir(parents=True)
            command_log.write_text(
                "\n".join([
                    json.dumps({"stdout": "first"}),
                    "not json",
                    json.dumps({"stdout": "second"}),
                    json.dumps({"stdout": "third"}),
                ]),
                encoding="utf-8",
            )

            tailed = local_ops.tail_command_log(config, limit=2)
            self.assertEqual([entry["stdout"] for entry in tailed["entries"]], ["second", "third"])

    def test_knowledge_add_search_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            added = knowledge_ops.add_source(config, title="Doc", text="Alpha beta gamma. Alpha topic.")
            results = knowledge_ops.search(config, "alpha")
            self.assertEqual(results["results"][0]["source_id"], added["source_id"])
            fetched = knowledge_ops.fetch(config, added["source_id"])
            self.assertIn("Alpha", fetched["text"])

    def test_corrupt_knowledge_store_is_quarantined_before_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            from mcp4chatgpt.knowledge_ops import _load_store, _store_path

            store_path = _store_path(config)
            store_path.write_text("{not json", encoding="utf-8")

            self.assertEqual(_load_store(config), {"sources": {}})
            self.assertFalse(store_path.exists())
            backups = list(store_path.parent.glob("sources.json.corrupt.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "{not json")

            added = knowledge_ops.add_source(config, title="New", text="fresh source")
            self.assertTrue(store_path.exists())
            self.assertEqual(len(list(store_path.parent.glob("sources.json.corrupt.*"))), 1)
            self.assertEqual(_load_store(config)["sources"][added["source_id"]]["title"], "New")

    def test_invalid_knowledge_store_shape_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            from mcp4chatgpt.knowledge_ops import _load_store, _store_path

            store_path = _store_path(config)
            store_path.write_text('{"sources": []}', encoding="utf-8")

            self.assertEqual(_load_store(config), {"sources": {}})
            self.assertFalse(store_path.exists())
            backups = list(store_path.parent.glob("sources.json.corrupt.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), '{"sources": []}')

    def test_web_ops_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            with self.assertRaises(web_ops.WebOpsNotConfigured):
                web_ops.search(config, "test")

    def test_oauth_token(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            client = register_client(config, {"redirect_uris": ["https://chat.openai.com/aip/callback"]})
            from mcp4chatgpt import oauth

            code = "code-test"
            oauth.AUTH_CODES[code] = {
                "client_id": client["client_id"],
                "redirect_uri": "https://chat.openai.com/aip/callback",
                "created_at": time.time(),
            }
            token = issue_token(config, {"code": code, "client_id": client["client_id"]})["access_token"]
            self.assertEqual(verify_token(config, token), client["client_id"])

    def test_oauth_register_rejects_invalid_redirect_uris(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            with self.assertRaises(ValueError):
                register_client(config, {"redirect_uris": "https://bad.example/cb"})
            with self.assertRaises(ValueError):
                register_client(config, {"redirect_uris": [1]})
            client = register_client(config, {"redirect_uris": ["https://chat.openai.com/aip/callback"]})
            self.assertEqual(client["redirect_uris"], ["https://chat.openai.com/aip/callback"])

    def test_oauth_code_is_single_use_under_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            from mcp4chatgpt import oauth

            code = "concurrent-code"
            client_id = "client-test"
            oauth.AUTH_CODES[code] = {
                "client_id": client_id,
                "redirect_uri": "https://chat.openai.com/aip/callback",
                "created_at": time.time(),
            }
            successes = []
            failures = []

            def consume() -> None:
                try:
                    successes.append(issue_token(config, {"code": code, "client_id": client_id}))
                except ValueError as exc:
                    failures.append(str(exc))

            threads = [threading.Thread(target=consume) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 7)

    def test_corrupt_oauth_clients_are_quarantined_before_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            from mcp4chatgpt.oauth import _clients_path, _load_clients

            clients_path = _clients_path(config)
            clients_path.write_text("{not json", encoding="utf-8")

            self.assertEqual(_load_clients(config), {})
            self.assertFalse(clients_path.exists())
            backups = list(clients_path.parent.glob("oauth_clients.json.corrupt.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "{not json")

            client = register_client(config, {"redirect_uris": ["https://chat.openai.com/aip/callback"]})
            self.assertTrue(clients_path.exists())
            self.assertEqual(len(list(clients_path.parent.glob("oauth_clients.json.corrupt.*"))), 1)
            self.assertIn(client["client_id"], _load_clients(config))

    def test_invalid_oauth_clients_shape_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            from mcp4chatgpt.oauth import _clients_path, _load_clients

            clients_path = _clients_path(config)
            clients_path.write_text("[]", encoding="utf-8")

            self.assertEqual(_load_clients(config), {})
            self.assertFalse(clients_path.exists())
            backups = list(clients_path.parent.glob("oauth_clients.json.corrupt.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "[]")

    def test_oauth_code_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            from mcp4chatgpt import oauth

            code = "expired-code"
            oauth.AUTH_CODES[code] = {
                "client_id": "client-test",
                "redirect_uri": "https://chat.openai.com/aip/callback",
                "created_at": 0,  # epoch 0 → always expired
            }
            with self.assertRaises(ValueError) as ctx:
                issue_token(config, {"code": code, "client_id": "client-test"})
            self.assertIn("expired", str(ctx.exception).lower())
            # Expired code must have been consumed; a second attempt must fail too.
            with self.assertRaises(ValueError):
                issue_token(config, {"code": code, "client_id": "client-test"})

    def test_oauth_pkce_requires_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            from mcp4chatgpt import oauth

            code = "pkce-code"
            oauth.AUTH_CODES[code] = {
                "client_id": "client-test",
                "redirect_uri": "https://chat.openai.com/aip/callback",
                "code_challenge": "challenge",
                "code_challenge_method": "plain",
                "created_at": 9999999999,
            }
            with self.assertRaises(ValueError):
                issue_token(config, {"code": code, "client_id": "client-test"})

    def test_chunk_overlap_is_capped(self) -> None:
        from mcp4chatgpt.knowledge_ops import _chunk_text

        chunks = _chunk_text("x" * 5000, chunk_chars=100, overlap=1000)
        self.assertLess(len(chunks), 120)
        self.assertGreater(len(chunks), 1)

    def test_list_sources_none_created_at(self) -> None:
        """list_sources must not crash when a source has created_at=None."""
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            knowledge_ops.add_source(config, title="A", text="hello")
            # Manually inject a record with missing created_at.
            from mcp4chatgpt.knowledge_ops import _load_store, _save_store
            store = _load_store(config)
            for rec in store["sources"].values():
                rec["created_at"] = None
            _save_store(config, store)
            # Must not raise TypeError.
            result = knowledge_ops.list_sources(config)
            self.assertIsInstance(result["sources"], list)

    def test_authorize_form_xss(self) -> None:
        """render_authorize_form must HTML-escape injected values and reject unknown keys."""
        from mcp4chatgpt.oauth import render_authorize_form
        html = render_authorize_form({
            "client_id": '<img src=x onerror=alert(1)>',
            "state": '"onmouseover=alert(2)',
            "unknown_injected": "<script>bad()</script>",
        }).decode()
        # Angle brackets must be entity-encoded so the injected text is inert.
        self.assertNotIn("<img", html)
        self.assertNotIn("<script>", html)
        # Double-quote in value must be encoded so it cannot break out of the attribute.
        self.assertNotIn('value=""onmouseover', html)
        self.assertIn("&lt;", html)   # < was encoded
        self.assertIn("&gt;", html)   # > was encoded
        self.assertIn("&quot;", html) # " was encoded
        # unknown key must be silently dropped (not whitelisted)
        self.assertNotIn("unknown_injected", html)

    def test_redact_does_not_overmatch_plain_words(self) -> None:
        from mcp4chatgpt.safety import redact

        # Plain prose words must never be redacted (no assignment present)
        self.assertEqual(redact("secretary of state"), "secretary of state")
        self.assertEqual(redact("notsecret=foo"), "notsecret=foo")
        self.assertEqual(redact("secretaries=5"), "secretaries=5")

        # Bare keyword forms (Pattern A) must be redacted
        self.assertIn("[REDACTED]", redact("password=p@ssw0rd"))
        self.assertIn("[REDACTED]", redact("api_key=supersecret"))
        self.assertIn("[REDACTED]", redact("token=abc123"))

        # Env-var prefixed forms (Pattern B) must be redacted
        self.assertIn("[REDACTED]", redact("MCP_AUTH_SECRET=foobar"))
        self.assertIn("[REDACTED]", redact("FIRECRAWL_API_KEY=fc-abc123xyz"))

    def test_tool_schema_contains_expected_tools(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            registry = ToolRegistry(config, AuditLogger(config.audit_log))
            names = {tool["name"] for tool in registry.list_tools()["tools"]}
            self.assertIn("web_search", names)
            self.assertIn("knowledge_search", names)
            self.assertIn("terminal_get_app_context", names)
            self.assertIn("local_run_command", names)
            self.assertIn("local_command_log_tail", names)

    def test_command_and_terminal_tool_descriptions_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            registry = ToolRegistry(config, AuditLogger(config.audit_log))
            tools = {tool["name"]: tool for tool in registry.list_tools()["tools"]}

            self.assertIn("local execution log", tools["local_run_command"]["description"])
            self.assertIn("background shell execution logs", tools["local_command_log_tail"]["description"])
            self.assertIn("press Return", tools["terminal_run_command"]["description"])
            self.assertIn("press_return=false", tools["terminal_send_input"]["description"])

    def test_audit_log_rotates_and_compresses(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "logs" / "audit.jsonl"
            logger = AuditLogger(log_path, rotate_bytes=1024 * 1024, retention_days=30)
            logger.log("first")
            # Make the active file look old; the next write must rotate it.
            old_ts = time.time() - 86400
            log_path.touch()
            import os
            os.utime(log_path, (old_ts, old_ts))
            logger.log("second")

            self.assertTrue(log_path.exists())
            compressed = list(log_path.parent.glob("audit.*.jsonl.gz"))
            self.assertEqual(len(compressed), 1)
            self.assertGreater(compressed[0].stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
