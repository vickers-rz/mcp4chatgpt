from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp4chatgpt import ext_jobs, ext_ops
from mcp4chatgpt.audit import AuditLogger
from mcp4chatgpt.ext_jobs import ExtJobError, ExtensionJobManager
from mcp4chatgpt.tools import ToolRegistry


class FakeBridge:
    def __init__(self) -> None:
        self.connected = True
        self.connected_at = 1234.5
        self.tab_id = 77
        self.url = "https://example.com/start"
        self.batch_payloads: list[dict] = []
        self.run_js_calls = 0
        self.block_event: threading.Event | None = None
        self.run_js_started = threading.Event()

    def is_connected(self) -> bool:
        return self.connected

    def connection_info(self) -> dict:
        if not self.connected:
            return {"connected": False}
        return {"connected": True, "connected_at": self.connected_at}

    def send_command(self, cmd: str, args: dict | None = None, timeout: float = 15.0):
        args = args or {}
        if not self.connected:
            raise RuntimeError("Extension disconnected")
        if cmd == "get_active_tab":
            return {"tabId": self.tab_id, "url": self.url}
        if cmd == "get_selection":
            if int(args.get("tabId", -1)) != self.tab_id:
                raise RuntimeError("No tab")
            return {"tabId": self.tab_id, "url": self.url, "selection": ""}
        if cmd == "run_js":
            self.run_js_calls += 1
            self.run_js_started.set()
            if self.block_event is not None:
                self.block_event.wait(timeout=2)
            payload = self.batch_payloads.pop(0) if self.batch_payloads else {"done": True}
            return {
                "tabId": self.tab_id,
                "result": json.dumps(payload, ensure_ascii=False),
                "resultType": "object",
                "executionWorld": "USER_SCRIPT",
            }
        raise AssertionError(f"Unexpected command: {cmd}")


def install_fake_bridge(monkeypatch: pytest.MonkeyPatch, fake: FakeBridge) -> None:
    monkeypatch.setattr(ext_jobs.ext_bridge, "is_connected", fake.is_connected)
    monkeypatch.setattr(ext_jobs.ext_bridge, "connection_info", fake.connection_info)
    monkeypatch.setattr(ext_jobs.ext_bridge, "send_command", fake.send_command)


def wait_terminal(manager: ExtensionJobManager, job_id: str, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager.get_job(job_id)
        if state["state"] in ext_jobs.TERMINAL_STATES:
            return state
        time.sleep(0.01)
    pytest.fail(f"job {job_id} did not reach a terminal state")


def make_config(tmp_path: Path):
    return SimpleNamespace(data_dir=tmp_path, max_output_chars=50_000)


@pytest.fixture(autouse=True)
def reset_managers():
    ext_jobs._reset_job_managers_for_tests()
    yield
    ext_jobs._reset_job_managers_for_tests()


class TestExtensionJobManager:
    def test_submit_poll_progress_records_and_result(self, tmp_path, monkeypatch):
        fake = FakeBridge()
        fake.batch_payloads = [
            {
                "done": False,
                "checkpoint": {"page": 2},
                "records": [{"name": "a"}, {"name": "b"}],
                "progress": 0.5,
                "counters": {"files": 2},
            },
            {
                "done": True,
                "checkpoint": {"page": 3},
                "records": [{"name": "c"}],
                "progress": 1.0,
                "counters": {"files": 3},
                "result": {"complete": True},
            },
        ]
        install_fake_bridge(monkeypatch, fake)
        manager = ExtensionJobManager(tmp_path)

        submitted = manager.start_js_job(
            code="async (checkpoint, batchIndex) => ({done: true})",
            initial_checkpoint={"page": 1},
            max_batches=5,
            batch_timeout_sec=2,
        )
        assert submitted["state"] in {"queued", "running"}
        final = wait_terminal(manager, submitted["job_id"])
        assert final["state"] == "completed"
        assert final["progress"] == 1.0
        assert final["counters"] == {"files": 3}
        assert final["record_count"] == 3
        assert final["batch_index"] == 2
        assert fake.run_js_calls == 2

        result = manager.get_job_result(submitted["job_id"], limit=10)
        assert [r["name"] for r in result["records"]] == ["a", "b", "c"]
        assert result["result"] == {"complete": True}
        assert Path(result["records_artifact_path"]).is_file()
        assert Path(result["result_artifact_path"]).is_file()

    def test_chunked_result_cursor(self, tmp_path, monkeypatch):
        fake = FakeBridge()
        fake.batch_payloads = [{
            "done": True,
            "records": [{"i": i} for i in range(5)],
        }]
        install_fake_bridge(monkeypatch, fake)
        manager = ExtensionJobManager(tmp_path)
        job = manager.start_js_job(code="(c, i) => ({done:true})", batch_timeout_sec=2)
        wait_terminal(manager, job["job_id"])

        first = manager.get_job_result(job["job_id"], cursor=0, limit=2)
        assert [r["i"] for r in first["records"]] == [0, 1]
        assert first["has_more"] is True
        assert first["next_cursor"] == 2
        second = manager.get_job_result(job["job_id"], cursor=2, limit=10)
        assert [r["i"] for r in second["records"]] == [2, 3, 4]
        assert second["has_more"] is False
        assert second["next_cursor"] is None

    def test_large_result_uses_artifact_and_preview(self, tmp_path, monkeypatch):
        fake = FakeBridge()
        fake.batch_payloads = [{"done": True, "result": {"text": "x" * 20_000}}]
        install_fake_bridge(monkeypatch, fake)
        manager = ExtensionJobManager(tmp_path)
        job = manager.start_js_job(code="(c, i) => ({done:true})", batch_timeout_sec=2)
        wait_terminal(manager, job["job_id"])

        result = manager.get_job_result(job["job_id"], max_chars=500)
        assert result["result_truncated"] is True
        assert len(result["result_preview"]) == 500
        assert Path(result["result_artifact_path"]).stat().st_size > 500
        assert "result" not in result

    def test_start_returns_while_worker_is_still_running(self, tmp_path, monkeypatch):
        fake = FakeBridge()
        fake.block_event = threading.Event()
        fake.batch_payloads = [{"done": True, "result": {"ok": True}}]
        install_fake_bridge(monkeypatch, fake)
        manager = ExtensionJobManager(tmp_path)

        started = time.monotonic()
        job = manager.start_js_job(code="async (c, i) => ({done:true})", batch_timeout_sec=2)
        elapsed = time.monotonic() - started
        assert elapsed < 0.2
        assert fake.run_js_started.wait(timeout=1)
        assert manager.get_job(job["job_id"])["state"] == "running"
        fake.block_event.set()
        assert wait_terminal(manager, job["job_id"])["state"] == "completed"

    def test_cancel_running_job(self, tmp_path, monkeypatch):
        fake = FakeBridge()
        fake.block_event = threading.Event()
        fake.batch_payloads = [{"done": False, "checkpoint": {"n": 1}}]
        install_fake_bridge(monkeypatch, fake)
        manager = ExtensionJobManager(tmp_path)
        job = manager.start_js_job(code="async (c, i) => ({done:false})", batch_timeout_sec=2)
        assert fake.run_js_started.wait(timeout=1)

        cancelled = manager.cancel_job(job["job_id"])
        assert cancelled["cancel_requested"] is True
        fake.block_event.set()
        final = wait_terminal(manager, job["job_id"])
        assert final["state"] == "cancelled"

    def test_extension_disconnect_interrupts(self, tmp_path, monkeypatch):
        fake = FakeBridge()
        fake.batch_payloads = [{"done": False, "checkpoint": {"n": 1}}]
        install_fake_bridge(monkeypatch, fake)
        manager = ExtensionJobManager(tmp_path)
        job = manager.start_js_job(code="(c, i) => ({done:false})", max_batches=5, batch_timeout_sec=2)
        assert fake.run_js_started.wait(timeout=1)
        fake.connected = False
        final = wait_terminal(manager, job["job_id"])
        assert final["state"] == "interrupted"
        assert "Extension" in final["error"] or "tab" in final["error"]

    def test_origin_change_interrupts(self, tmp_path, monkeypatch):
        fake = FakeBridge()
        fake.block_event = threading.Event()
        fake.batch_payloads = [{"done": False}, {"done": True}]
        install_fake_bridge(monkeypatch, fake)
        manager = ExtensionJobManager(tmp_path)
        job = manager.start_js_job(code="(c, i) => ({done:false})", max_batches=5, batch_timeout_sec=2)
        assert fake.run_js_started.wait(timeout=1)
        # First batch completes, then the next context check sees a new origin.
        fake.url = "https://other.example/path"
        fake.block_event.set()
        final = wait_terminal(manager, job["job_id"])
        assert final["state"] == "interrupted"
        assert "origin changed" in final["error"]

    def test_extension_session_change_interrupts(self, tmp_path, monkeypatch):
        fake = FakeBridge()
        fake.block_event = threading.Event()
        fake.batch_payloads = [{"done": False}, {"done": True}]
        install_fake_bridge(monkeypatch, fake)
        manager = ExtensionJobManager(tmp_path)
        job = manager.start_js_job(code="(c, i) => ({done:false})", max_batches=5, batch_timeout_sec=2)
        assert fake.run_js_started.wait(timeout=1)
        fake.connected_at += 1
        fake.block_event.set()
        final = wait_terminal(manager, job["job_id"])
        assert final["state"] == "interrupted"
        assert "session changed" in final["error"]

    def test_restart_marks_active_job_interrupted(self, tmp_path):
        root = tmp_path / "jobs"
        job_id = "a" * 32
        job_dir = root / job_id
        job_dir.mkdir(parents=True)
        state = {
            "job_id": job_id,
            "state": "running",
            "created_at": time.time() - 10,
            "started_at": time.time() - 9,
            "finished_at": None,
            "progress": 0.4,
            "counters": {},
            "batch_index": 2,
            "record_count": 3,
            "error": None,
            "cancel_requested": False,
            "tab_id": 77,
            "origin": "https://example.com",
            "initial_url": "https://example.com/start",
        }
        (job_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

        manager = ExtensionJobManager(tmp_path)
        recovered = manager.get_job(job_id)
        assert recovered["state"] == "interrupted"
        assert "automatic resume is disabled" in recovered["error"]

    def test_cleanup_ttl(self, tmp_path):
        root = tmp_path / "jobs"
        job_id = "b" * 32
        job_dir = root / job_id
        job_dir.mkdir(parents=True)
        state = {
            "job_id": job_id,
            "state": "completed",
            "created_at": time.time() - 1000,
            "finished_at": time.time() - 1000,
        }
        (job_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        (job_dir / "records.jsonl").write_text("{}\n", encoding="utf-8")
        manager = ExtensionJobManager(tmp_path, ttl_sec=60)
        assert not job_dir.exists()
        assert manager.cleanup_expired() == 0

    def test_safety_rejections_and_path_traversal(self, tmp_path, monkeypatch):
        fake = FakeBridge()
        install_fake_bridge(monkeypatch, fake)
        manager = ExtensionJobManager(tmp_path)
        with pytest.raises(ValueError, match="cannot be empty"):
            manager.start_js_job(code="", batch_timeout_sec=2)
        with pytest.raises(ValueError, match="batch_timeout_sec"):
            manager.start_js_job(code="() => ({done:true})", batch_timeout_sec=30)
        with pytest.raises(ValueError, match="max_batches"):
            manager.start_js_job(code="() => ({done:true})", max_batches=0, batch_timeout_sec=2)
        with pytest.raises(ExtJobError, match="Invalid job_id"):
            manager.get_job("../../etc/passwd")
        assert not (tmp_path / "jobs" / ".." / "etc").exists()

    def test_non_web_url_requires_exact_url_stability(self, tmp_path, monkeypatch):
        fake = FakeBridge()
        fake.url = "file:///tmp/a.html"
        fake.block_event = threading.Event()
        fake.batch_payloads = [{"done": False}, {"done": True}]
        install_fake_bridge(monkeypatch, fake)
        manager = ExtensionJobManager(tmp_path)
        job = manager.start_js_job(code="(c, i) => ({done:false})", max_batches=5, batch_timeout_sec=2)
        assert fake.run_js_started.wait(timeout=1)
        fake.url = "file:///tmp/b.html"
        fake.block_event.set()
        final = wait_terminal(manager, job["job_id"])
        assert final["state"] == "interrupted"
        assert "URL changed" in final["error"]


class TestExtOpsAndToolRegistry:
    def test_ext_run_js_timeout_is_bounded_and_backward_compatible(self, tmp_path, monkeypatch):
        calls: list[float] = []

        monkeypatch.setattr(ext_ops.ext_bridge, "is_connected", lambda: True)

        def send_command(cmd, args, timeout):
            calls.append(timeout)
            return {
                "tabId": 1,
                "result": "ok",
                "resultType": "string",
                "executionWorld": "USER_SCRIPT",
            }

        monkeypatch.setattr(ext_ops.ext_bridge, "send_command", send_command)
        config = make_config(tmp_path)
        result = ext_ops.ext_run_js(config, "document.title")
        assert result["result"] == "ok"
        assert calls[-1] == 30
        ext_ops.ext_run_js(config, "document.title", timeout_sec=999)
        assert calls[-1] == 120

    def test_job_tools_are_registered_with_expected_annotations(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_EXT_ASYNC_JOBS_ENABLED", "1")
        config = SimpleNamespace(max_output_chars=50_000)
        audit = AuditLogger(tmp_path / "audit.jsonl")
        registry = ToolRegistry(config, audit)
        definitions = {t["name"]: t for t in registry.list_tools(auth_required=False)["tools"]}
        for name in ["ext_start_js_job", "ext_get_job", "ext_get_job_result", "ext_cancel_job"]:
            assert name in definitions
            assert definitions[name]["annotations"]["openWorldHint"] is True
        assert definitions["ext_start_js_job"]["annotations"]["readOnlyHint"] is False
        assert definitions["ext_get_job"]["annotations"]["readOnlyHint"] is True
        assert definitions["ext_cancel_job"]["annotations"]["readOnlyHint"] is False

    def test_job_tool_audit_channel_is_extension(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_EXT_ASYNC_JOBS_ENABLED", "1")
        config = SimpleNamespace(max_output_chars=50_000)
        audit_path = tmp_path / "audit.jsonl"
        audit = AuditLogger(audit_path)
        monkeypatch.setattr(ext_ops, "ext_get_job", lambda _c, job_id: {"job_id": job_id, "state": "completed"})
        registry = ToolRegistry(config, audit)
        result = registry.call_tool("ext_get_job", {"job_id": "a" * 32}, client_id="test")
        assert result["structuredContent"]["state"] == "completed"
        audit_text = audit_path.read_text(encoding="utf-8").replace(" ", "")
        assert '"channel":"extension"' in audit_text
        assert "completed" not in audit_text
