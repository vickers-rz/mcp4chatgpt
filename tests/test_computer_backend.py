"""Real pipe/child-process fault injection; no GUI interaction required."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from mcp4chatgpt import computer_backend as backend


@pytest.fixture
def helper(monkeypatch, tmp_path):
    backend.stop()
    monkeypatch.setattr(backend.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(backend, "_binary", lambda: Path("/unused"))
    real_popen = subprocess.Popen
    marker = tmp_path / "dispatched"

    def start(body, *, read=True):
        code = ("import sys,json,time\nfrom pathlib import Path\n"
                + ("r=json.loads(sys.stdin.readline())\n" if read else "") +
                f"Path({str(marker)!r}).write_text('effect')\n" + body)
        monkeypatch.setattr(backend.subprocess, "Popen", lambda *a, **kw: real_popen([sys.executable, "-c", code], **kw))
        return marker
    yield start
    backend.stop()


@pytest.mark.parametrize("effectful", [False, True])
@pytest.mark.parametrize("body", [
    'print(json.dumps({"request_id":r["request_id"],"ok":True,"result":None}),flush=True)',
    'print(json.dumps({"request_id":"wrong","ok":True,"result":{}}),flush=True)',
    'print("[]",flush=True)',
    'print("broken-json",flush=True)',
    'sys.stdout.write("partial");sys.stdout.flush()',
    'print(json.dumps({"request_id":r["request_id"],"ok":False,"error":"unknown"}),flush=True)',
])
def test_dispatch_protocol_failures_are_uncertain(helper, body, effectful):
    marker = helper(body)
    with pytest.raises(backend.ComputerBackendError) as error:
        backend.call("click", {}, effectful=effectful)
    assert marker.read_text() == "effect"
    assert error.value.effect == ("outcome_unknown" if effectful else "not_started")


@pytest.mark.parametrize("effect", ["not_started", "outcome_unknown"])
def test_explicit_helper_effect_is_preserved(helper, effect):
    helper(f'print(json.dumps({{"request_id":r["request_id"],"ok":False,"error":"stale_snapshot","effect":{effect!r}}}),flush=True)')
    with pytest.raises(backend.ComputerBackendError) as error:
        backend.call("click", {}, effectful=True)
    assert error.value.effect == effect


def test_valid_result(helper):
    helper('print(json.dumps({"request_id":r["request_id"],"ok":True,"result":{"effect":"completed"}}),flush=True)')
    assert backend.call("click", {}, effectful=True) == {"effect": "completed"}


@pytest.mark.parametrize("effectful", [False, True])
def test_read_timeout(helper, monkeypatch, effectful):
    marker = helper('time.sleep(10)')
    monkeypatch.setattr(backend, "IO_TIMEOUT", .15)
    with pytest.raises(backend.ComputerBackendError) as error:
        backend.call("click", {}, effectful=effectful)
    assert marker.exists()
    assert error.value.code == "helper_timeout"
    assert error.value.effect == ("outcome_unknown" if effectful else "not_started")
    assert backend._process is None


def test_stalled_pipe_write_has_deadline(helper, monkeypatch):
    helper('time.sleep(10)', read=False)
    monkeypatch.setattr(backend, "IO_TIMEOUT", .15)
    with pytest.raises(backend.ComputerBackendError) as error:
        backend.call("type_text", {"text": "😀" * 20_000}, effectful=True)
    assert error.value.code == "helper_timeout"
    assert error.value.effect == "outcome_unknown"
    assert backend._process is None


def test_large_response(helper, monkeypatch):
    helper('print("x"*1000,flush=True)')
    monkeypatch.setattr(backend, "MAX_LINE", 100)
    with pytest.raises(backend.ComputerBackendError) as error:
        backend.call("click", {}, effectful=True)
    assert error.value.effect == "outcome_unknown"


def test_oversize_request_never_launches(helper):
    marker = helper('print("unexpected")')
    with pytest.raises(backend.ComputerBackendError) as error:
        backend.call("click", {"text": "x" * 100_000}, effectful=True)
    assert error.value.effect == "not_started"
    assert not marker.exists()


def test_start_failure_is_not_started(helper, monkeypatch):
    helper('print("unexpected")')
    def fail(*args, **kwargs):
        raise OSError("not executable")
    monkeypatch.setattr(backend.subprocess, "Popen", fail)
    with pytest.raises(backend.ComputerBackendError) as error:
        backend.call("click", {}, effectful=True)
    assert error.value.effect == "not_started"
