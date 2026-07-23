from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from base64 import b64encode
from dataclasses import replace
from pathlib import Path
from unittest import mock

from mcp4chatgpt.audit import AuditLogger
from mcp4chatgpt.config import Config
from mcp4chatgpt import ext_ops, knowledge_ops, local_ops, terminal_ops, web_ops
from mcp4chatgpt.oauth import issue_token, register_client, verify_token
from mcp4chatgpt.safety import resolve_allowed_path, validate_command
from mcp4chatgpt.tools import ToolRegistry


class FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


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
        brave_api_key="",
        brave_base_url="https://api.search.brave.com/res/v1",
        open_webui_search_default_engine="brave",
        knowledge_roots=[root],
        knowledge_store_dir=tmp / "knowledge",
        tls_cert_path="",
        tls_key_path="",
        max_output_chars=10000,
        log_rotate_bytes=20 * 1024 * 1024,
        log_retention_days=30,
        allowed_hosts=["localhost", "127.0.0.1", "::1"],
        local_auth_disabled=False,
        ext_bridge_port=8765,
        ext_screenshot_dir=tmp / "screenshots",
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

    def test_timeout_local_command_kills_child_processes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            pid_file = config.allowed_roots[0] / "child.pid"
            command = (
                f"{shlex.quote(sys.executable)} -c "
                + shlex.quote(
                    "import pathlib, subprocess, sys; "
                    "p = subprocess.Popen(['sleep', '30']); "
                    "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); "
                    "p.wait()"
                )
                + f" {shlex.quote(str(pid_file))}"
            )

            with self.assertRaises(subprocess.TimeoutExpired):
                local_ops.run_command(config, command, cwd=str(config.allowed_roots[0]), timeout_sec=1)

            child_pid = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.time() + 3
            while time.time() < deadline:
                if not self._pid_exists(child_pid):
                    break
                time.sleep(0.05)
            self.assertFalse(self._pid_exists(child_pid))

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

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

    def test_brave_search_uses_subscription_header(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = replace(make_config(Path(d)), brave_api_key="brave-key")
            payload = {"web": {"results": [{"title": "Example", "url": "https://example.test", "description": "Snippet"}]}}

            def fake_urlopen(req, timeout):
                self.assertEqual(timeout, 30)
                self.assertIn("/web/search?", req.full_url)
                self.assertIn("q=test", req.full_url)
                self.assertEqual(req.headers["X-subscription-token"], "brave-key")
                return FakeHTTPResponse(payload)

            with mock.patch("urllib.request.urlopen", fake_urlopen):
                result = web_ops.brave_search(config, "test", limit=3)
            self.assertEqual(result, payload)

    def test_brave_get_retries_one_transient_network_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = replace(make_config(Path(d)), brave_api_key="brave-key")
            payload = {"web": {"results": [{"title": "Example", "url": "https://example.test"}]}}

            with (
                mock.patch(
                    "urllib.request.urlopen",
                    side_effect=[urllib.error.URLError("temporary"), FakeHTTPResponse(payload)],
                ) as urlopen,
                mock.patch("mcp4chatgpt.web_ops.time.sleep"),
            ):
                result = web_ops.brave_search(config, "test")

            self.assertEqual(result, payload)
            self.assertEqual(urlopen.call_count, 2)

    def test_firecrawl_post_does_not_retry_network_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = replace(make_config(Path(d)), firecrawl_api_key="fc-key")

            with mock.patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError("response lost"),
            ) as urlopen:
                with self.assertRaisesRegex(RuntimeError, "Firecrawl request failed"):
                    web_ops.crawl(config, "https://example.test")

            self.assertEqual(urlopen.call_count, 1)

    def test_firecrawl_read_only_search_retries_one_network_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = replace(make_config(Path(d)), firecrawl_api_key="fc-key")
            payload = {"data": [{"title": "Example", "url": "https://example.test"}]}

            with (
                mock.patch(
                    "urllib.request.urlopen",
                    side_effect=[urllib.error.URLError("temporary"), FakeHTTPResponse(payload)],
                ) as urlopen,
                mock.patch("mcp4chatgpt.web_ops.time.sleep"),
            ):
                result = web_ops.search(config, "test")

            self.assertEqual(result, payload)
            self.assertEqual(urlopen.call_count, 2)

    def test_combined_search_can_fetch_brave_results_with_firecrawl(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = replace(make_config(Path(d)), brave_api_key="brave-key", firecrawl_api_key="fc-key")
            calls = []

            def fake_urlopen(req, timeout):
                calls.append((req.get_method(), req.full_url))
                if req.get_method() == "GET":
                    return FakeHTTPResponse({
                        "web": {
                            "results": [
                                {"title": "Example", "url": "https://example.test", "description": "Snippet"},
                            ]
                        }
                    })
                return FakeHTTPResponse({"data": {"markdown": "# Example\nBody"}})

            with mock.patch("urllib.request.urlopen", fake_urlopen):
                result = web_ops.combined_search(config, "test", engine="brave", fetch_content=True)

            self.assertEqual(result["results"][0]["source"], "brave")
            self.assertEqual(result["results"][0]["markdown"], "# Example\nBody")
            self.assertEqual([method for method, _ in calls], ["GET", "POST"])

    def test_brave_extra_snippets_are_merged_into_snippet(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = replace(make_config(Path(d)), brave_api_key="brave-key")
            payload = {
                "web": {
                    "results": [
                        {
                            "title": "Example",
                            "url": "https://example.test",
                            "description": "Primary summary",
                            "extra_snippets": ["Additional context", "Primary summary"],
                        },
                    ]
                }
            }

            with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
                result = web_ops.combined_search(config, "test", engine="brave")

            self.assertEqual(result["results"][0]["snippet"], "Primary summary\nAdditional context")

    def test_combined_search_auto_falls_back_when_brave_has_no_results(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = replace(make_config(Path(d)), brave_api_key="brave-key", firecrawl_api_key="fc-key")
            responses = [
                {"type": "search", "query": {"original": "test"}},
                {"data": [{"title": "Fallback", "url": "https://example.test", "description": "Found"}]},
            ]

            with mock.patch(
                "urllib.request.urlopen",
                side_effect=[FakeHTTPResponse(payload) for payload in responses],
            ):
                result = web_ops.combined_search(config, "test", engine="auto")

            self.assertEqual(result["engine"], "firecrawl")
            self.assertEqual(result["results"][0]["source"], "firecrawl")
            self.assertIn("no web results", result["fallback_reason"])

    def test_combined_search_keeps_results_when_firecrawl_fetch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = replace(make_config(Path(d)), brave_api_key="brave-key")
            brave_payload = {
                "web": {
                    "results": [
                        {"title": "Example", "url": "https://example.test", "description": "Snippet"},
                    ]
                }
            }

            with (
                mock.patch("mcp4chatgpt.web_ops.brave_search", return_value=brave_payload),
                mock.patch(
                    "mcp4chatgpt.web_ops.scrape",
                    side_effect=web_ops.WebOpsNotConfigured("FIRECRAWL_API_KEY is not set"),
                ),
            ):
                result = web_ops.combined_search(config, "test", engine="brave", fetch_content=True)

            self.assertEqual(result["results"][0]["url"], "https://example.test")
            self.assertIn("FIRECRAWL_API_KEY", result["results"][0]["fetch_error"])


    def test_chrome_tools_are_listed_as_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            registry = ToolRegistry(make_config(Path(d)), AuditLogger(Path(d) / "audit.jsonl"))
            tools = {tool["name"]: tool for tool in registry.list_tools()["tools"]}
            self.assertIn("chrome_list_tabs", tools)
            self.assertIn("chrome_get_active_tab_context", tools)
            self.assertIn("browser_current_tab", tools)
            self.assertIn("browser_get_page_text", tools)
            self.assertIn("browser_get_selection", tools)
            self.assertIn("browser_get_links", tools)
            self.assertTrue(tools["chrome_list_tabs"]["annotations"]["readOnlyHint"])
            self.assertTrue(tools["chrome_get_active_tab_context"]["annotations"]["readOnlyHint"])
            self.assertTrue(tools["browser_current_tab"]["annotations"]["readOnlyHint"])

    def test_ext_connection_status_disconnected_is_structured(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            with mock.patch("mcp4chatgpt.ext_bridge.connection_info", return_value={"connected": False}):
                status = ext_ops.ext_connection_status(config)
            self.assertFalse(status["connected"])
            self.assertIn("NOT connected", status["hint"])

    def test_ext_list_tabs_requires_connection(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            with mock.patch("mcp4chatgpt.ext_bridge.is_connected", return_value=False):
                with self.assertRaises(ext_ops.ExtNotConnectedError):
                    ext_ops.ext_list_tabs(config)

    def test_ext_list_tabs_sends_command_and_redacts(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            send = mock.Mock(return_value={
                "tabs": [{
                    "windowId": 1,
                    "tabId": 2,
                    "index": 0,
                    "active": True,
                    "pinned": False,
                    "title": "Token tab",
                    "url": "https://example.test/?token=secret-value",
                    "status": "complete",
                }],
                "truncated": False,
            })
            with mock.patch("mcp4chatgpt.ext_bridge.is_connected", return_value=True), \
                    mock.patch("mcp4chatgpt.ext_bridge.send_command", send):
                result = ext_ops.ext_list_tabs(config, max_tabs=5)

            send.assert_called_once_with("list_tabs", {"maxTabs": 5})
            self.assertEqual(result["count"], 1)
            self.assertIn("[REDACTED]", result["tabs"][0]["url"])

    def test_ext_screenshot_saves_png_to_configured_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            png_bytes = b"\x89PNG\r\n\x1a\n"
            send = mock.Mock(return_value={
                "tabId": 7,
                "url": "https://example.test",
                "dataUrl": "data:image/png;base64," + b64encode(png_bytes).decode("ascii"),
                "width": 1,
                "height": 1,
            })
            with mock.patch("mcp4chatgpt.ext_bridge.is_connected", return_value=True), \
                    mock.patch("mcp4chatgpt.ext_bridge.send_command", send):
                result = ext_ops.ext_screenshot(config, quality=95)

            send.assert_called_once_with("screenshot", {"quality": 95}, timeout=20)
            saved = Path(result["file_path"])
            self.assertEqual(saved.read_bytes(), png_bytes)
            self.assertEqual(saved.parent, config.ext_screenshot_dir)
            self.assertNotIn("data_base64", result)

    def test_ext_fill_input_preserves_tab_target_and_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            send = mock.Mock(return_value={
                "tabId": 7,
                "filled": False,
                "submitted": False,
                "tagName": "TEXTAREA",
                "error": "setter failed",
            })
            with mock.patch("mcp4chatgpt.ext_bridge.is_connected", return_value=True), \
                    mock.patch("mcp4chatgpt.ext_bridge.send_command", send):
                result = ext_ops.ext_fill_input(
                    config,
                    "#search",
                    "AI progress",
                    tab_id=7,
                    submit=True,
                )

            send.assert_called_once_with(
                "fill_input",
                {
                    "selector": "#search",
                    "value": "AI progress",
                    "submit": True,
                    "tabId": 7,
                },
                timeout=15,
            )
            self.assertEqual(result["element_tag"], "TEXTAREA")
            self.assertEqual(result["error"], "setter failed")

    def test_ext_fill_input_preserves_attempted_submission_status(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            send = mock.Mock(return_value={
                "tabId": 7,
                "filled": True,
                "submitted": False,
                "submitAttempted": True,
                "submissionStatus": "attempted",
                "submitMethod": "synthetic_enter",
                "tagName": "INPUT",
            })
            with mock.patch("mcp4chatgpt.ext_bridge.is_connected", return_value=True), \
                    mock.patch("mcp4chatgpt.ext_bridge.send_command", send):
                result = ext_ops.ext_fill_input(
                    config,
                    "#search",
                    "AI progress",
                    tab_id=7,
                    submit=True,
                )

            self.assertTrue(result["filled"])
            self.assertFalse(result["submitted"])
            self.assertTrue(result["submit_attempted"])
            self.assertEqual(result["submission_status"], "attempted")
            self.assertEqual(result["submit_method"], "synthetic_enter")

    def test_ext_run_js_preserves_execution_world(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            send = mock.Mock(return_value={
                "tabId": 7,
                "result": "42",
                "resultType": "number",
                "executionWorld": "USER_SCRIPT",
            })
            with mock.patch("mcp4chatgpt.ext_bridge.is_connected", return_value=True), \
                    mock.patch("mcp4chatgpt.ext_bridge.send_command", send):
                result = ext_ops.ext_run_js(config, "6 * 7", tab_id=7)

            send.assert_called_once_with(
                "run_js",
                {"code": "6 * 7", "tabId": 7},
                timeout=30,
            )
            self.assertEqual(result["result"], "42")
            self.assertEqual(result["type"], "number")
            self.assertEqual(result["execution_world"], "USER_SCRIPT")

    def test_ext_run_js_preserves_error_world_and_stack(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            send = mock.Mock(return_value={
                "tabId": 7,
                "result": None,
                "error": "Error: intentional test",
                "errorStack": "Error: intentional test\n    at test.js:1:1",
                "resultType": "error",
                "executionWorld": "USER_SCRIPT",
            })
            with mock.patch("mcp4chatgpt.ext_bridge.is_connected", return_value=True), \
                    mock.patch("mcp4chatgpt.ext_bridge.send_command", send):
                result = ext_ops.ext_run_js(config, "throw new Error('intentional test')", tab_id=7)

            self.assertIsNone(result["result"])
            self.assertEqual(result["error"], "Error: intentional test")
            self.assertIn("test.js", result["error_stack"])
            self.assertEqual(result["execution_world"], "USER_SCRIPT")

    def test_ext_listen_changes_waits_and_unsubscribes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            callbacks = []

            def subscribe(callback):
                callbacks.append(callback)

            def send_command(cmd, args, timeout):
                self.assertEqual(cmd, "subscribe_changes")
                self.assertEqual(args, {"durationSec": 3, "tabId": 9})
                self.assertEqual(timeout, 10)
                callbacks[0]({
                    "event": "dom_mutation",
                    "tabId": 9,
                    "url": "https://example.test",
                    "title": "Changed",
                    "timestamp": 123.0,
                })
                return {"subscribed": True}

            with mock.patch("mcp4chatgpt.ext_bridge.is_connected", return_value=True), \
                    mock.patch("mcp4chatgpt.ext_bridge.subscribe_changes", side_effect=subscribe) as sub, \
                    mock.patch("mcp4chatgpt.ext_bridge.unsubscribe_changes") as unsub, \
                    mock.patch("mcp4chatgpt.ext_bridge.send_command", side_effect=send_command), \
                    mock.patch("mcp4chatgpt.ext_ops.time.sleep") as sleep:
                result = ext_ops.ext_listen_changes(config, duration_sec=3, tab_id=9)

            self.assertEqual(result["count"], 1)
            self.assertEqual(result["events"][0]["type"], "dom_mutation")
            sleep.assert_called_once_with(3)
            sub.assert_called_once()
            unsub.assert_called_once_with(callbacks[0])

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
        self.assertIn("[REDACTED]", redact("BRAVE_SEARCH_API_KEY=BSAabc123xyz"))

    def test_tool_schema_contains_expected_tools(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            registry = ToolRegistry(config, AuditLogger(config.audit_log))
            names = {tool["name"] for tool in registry.list_tools()["tools"]}
            self.assertIn("web_search", names)
            self.assertIn("web_brave_search", names)
            self.assertIn("web_combined_search", names)
            self.assertIn("knowledge_search", names)
            self.assertIn("terminal_get_app_context", names)
            self.assertIn("app_get_context", names)
            self.assertIn("app_write_text", names)
            self.assertIn("apple_notes_inspect_store", names)
            self.assertIn("apple_notes_list_sqlite", names)
            self.assertIn("apple_notes_read_sqlite", names)
            self.assertIn("apple_notes_search_sqlite", names)
            self.assertIn("local_run_command", names)
            self.assertIn("local_command_log_tail", names)
            # Browser extension tools
            self.assertIn("ext_connection_status", names)
            self.assertIn("ext_list_tabs", names)
            self.assertIn("ext_get_active_tab", names)
            self.assertIn("ext_get_dom", names)
            self.assertIn("ext_get_selection", names)
            self.assertIn("ext_screenshot", names)
            self.assertIn("ext_navigate", names)
            self.assertIn("ext_click_element", names)
            self.assertIn("ext_fill_input", names)
            self.assertIn("ext_run_js", names)
            self.assertIn("ext_listen_changes", names)

    def test_ext_tool_annotations_match_read_and_write_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            registry = ToolRegistry(config, AuditLogger(config.audit_log))
            tools = {tool["name"]: tool for tool in registry.list_tools()["tools"]}

            self.assertTrue(tools["ext_get_active_tab"]["annotations"]["readOnlyHint"])
            self.assertTrue(tools["ext_screenshot"]["annotations"]["readOnlyHint"])
            self.assertTrue(tools["ext_listen_changes"]["annotations"]["readOnlyHint"])
            self.assertFalse(tools["ext_navigate"]["annotations"]["readOnlyHint"])
            self.assertTrue(tools["ext_navigate"]["annotations"]["destructiveHint"])
            self.assertFalse(tools["ext_run_js"]["annotations"]["readOnlyHint"])
            self.assertTrue(tools["ext_run_js"]["annotations"]["destructiveHint"])

    def test_command_and_terminal_tool_descriptions_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            registry = ToolRegistry(config, AuditLogger(config.audit_log))
            tools = {tool["name"]: tool for tool in registry.list_tools()["tools"]}

            self.assertIn("local execution log", tools["local_run_command"]["description"])
            self.assertIn("background shell execution logs", tools["local_command_log_tail"]["description"])
            self.assertIn("press Return", tools["terminal_run_command"]["description"])
            self.assertIn("press_return=false", tools["terminal_send_input"]["description"])
            self.assertIn("any co-te supported macOS app", tools["app_get_context"]["description"])
            self.assertIn("Accessibility", tools["app_write_text"]["description"])
            self.assertIn("reuse the returned tab_id", tools["ext_navigate"]["description"])
            self.assertIn("same tab_id", tools["ext_fill_input"]["description"])

    def test_co_te_app_schemas_expose_non_terminal_apps(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            registry = ToolRegistry(config, AuditLogger(config.audit_log))
            tools = {tool["name"]: tool for tool in registry.list_tools()["tools"]}

            terminal_apps = tools["terminal_get_app_context"]["inputSchema"]["properties"]["app"]["enum"]
            app_apps = tools["app_get_context"]["inputSchema"]["properties"]["app"]["enum"]

            self.assertIn("apple_notes", terminal_apps)
            self.assertIn("vscode", terminal_apps)
            self.assertIn("cursor", app_apps)
            self.assertIn("warp", app_apps)
            self.assertEqual(
                tools["app_write_text"]["inputSchema"]["properties"]["mode"]["enum"],
                ["insert", "replace_selection", "replace_all"],
            )
            self.assertFalse(tools["app_write_text"]["annotations"]["readOnlyHint"])
            self.assertTrue(tools["apple_notes_search_sqlite"]["annotations"]["readOnlyHint"])

    def test_co_te_wrappers_forward_new_tools(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            calls: list[tuple[str, dict[str, object]]] = []

            class FakeCoTe:
                def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
                    calls.append((name, arguments))
                    return {"content": [{"type": "text", "text": name}]}

            with mock.patch.object(terminal_ops, "_load_co_te", return_value=FakeCoTe()):
                write_result = terminal_ops.write_app_text(config, "vscode", "hello", mode="replace_selection", press_return=True, sensitive=True, label="main.py")
                inspect_result = terminal_ops.inspect_apple_notes_store(config)
                terminal_ops.list_apple_notes_sqlite(config, limit=3, folder="work")
                terminal_ops.read_apple_note_sqlite(config, "42")
                terminal_ops.search_apple_notes_sqlite(config, "needle", limit=4)

            self.assertEqual(write_result, "write_app_text")
            self.assertEqual(inspect_result, "inspect_apple_notes_store")
            self.assertEqual(calls[0][0], "write_app_text")
            self.assertEqual(calls[0][1]["app"], "vscode")
            self.assertEqual(calls[0][1]["mode"], "replace_selection")
            self.assertEqual(calls[1], ("inspect_apple_notes_store", {}))
            self.assertEqual(calls[2], ("list_apple_notes_sqlite", {"limit": 3, "folder": "work"}))
            self.assertEqual(calls[3], ("read_apple_note_sqlite", {"note_id": "42"}))
            self.assertEqual(calls[4], ("search_apple_notes_sqlite", {"query": "needle", "limit": 4}))

    def test_co_te_tool_registry_response_is_not_double_wrapped(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))

            class FakeCoTe:
                def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
                    return {"content": [{"type": "text", "text": "Apple Notes count: 263"}]}

            with mock.patch.object(terminal_ops, "_load_co_te", return_value=FakeCoTe()):
                registry = ToolRegistry(config, AuditLogger(config.audit_log))
                result = registry.call_tool("apple_notes_inspect_store", {})

            self.assertEqual(result["content"][0]["text"], "Apple Notes count: 263")
            self.assertEqual(result["structuredContent"], "Apple Notes count: 263")
            self.assertNotIn('"content"', result["content"][0]["text"])

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
