import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf
import pytest

from mcp4chatgpt import pdf_ops, computer_cua_backend as cua
from mcp4chatgpt.jobs import manager
from mcp4chatgpt.jobs.store import JobStore


def make_pdf(tmp_path, text):
    source = tmp_path / 'source.pdf'
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=24)
        doc.save(source)
    return source, SimpleNamespace(allowed_roots=[tmp_path])


@pytest.mark.parametrize('replacement', ['Ω', '中文'])
def test_unencodable_pdf_replacement_is_rejected(tmp_path, replacement):
    source, cfg = make_pdf(tmp_path, 'OriginalLong')
    target = tmp_path / 'output.pdf'
    with pytest.raises(ValueError, match='unsupported'):
        pdf_ops.redact_text(cfg, str(source), 'OriginalLong', replacement=replacement, output_path=str(target))
    assert not target.exists()


@pytest.mark.parametrize('replacement', ['é', 'OK'])
def test_pdf_supported_replacement_roundtrip(tmp_path, replacement):
    source, cfg = make_pdf(tmp_path, 'OriginalLong')
    result = pdf_ops.redact_text(cfg, str(source), 'OriginalLong', replacement=replacement)
    with pymupdf.open(result['output_path']) as doc:
        assert doc[0].get_text().strip() == replacement


def test_casefold_requires_complete_original_characters(tmp_path):
    source, cfg = make_pdf(tmp_path, 'ß')
    with pytest.raises(ValueError, match='No matches'):
        pdf_ops.redact_text(cfg, str(source), 's', case_sensitive=False)
    result = pdf_ops.redact_text(cfg, str(source), 'ss', case_sensitive=False)
    assert result['matches'] == 1
    with pymupdf.open(result['output_path']) as doc:
        assert not doc[0].get_text().strip()


def test_invalid_utf8_closes_transport_and_preserves_unknown_effect(monkeypatch):
    process = SimpleNamespace(poll=lambda: None, stdout=None)
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, 'rb', buffering=0) as stream:
        process.stdout = stream
        monkeypatch.setattr(cua, '_process', process)
        monkeypatch.setattr(cua, '_read_buffer', bytearray(b'\xff\n'))
        monkeypatch.setattr(cua, '_call_locked', lambda *a, **k: cua._wait_response(1, operation='type_keyboard', args={}, timeout=.2, effectful=True))
        try:
            with patch.object(cua, 'stop') as stop:
                with pytest.raises(cua.CUABackendError) as error:
                    cua.call('type_keyboard', {}, effectful=True)
                assert error.value.code == 'cua_protocol_error'
                assert error.value.effect == 'outcome_unknown'
                assert not error.value.fallback_allowed
                stop.assert_called_once()
        finally:
            os.close(write_fd)


@pytest.mark.parametrize('actual', ['another-process', None, 'expected'])
def test_job_status_checks_actual_identity(tmp_path, monkeypatch, actual):
    monkeypatch.setattr(manager.process, 'process_identity', lambda pid: actual)
    monkeypatch.setattr(manager.process, 'pid_alive', lambda pid: True)
    metadata = dict(job_id='job_' + 'a' * 24, state='running', supervisor_pid=123,
                    supervisor_identity='expected', child_pgid=None)
    result = manager._augment_status(JobStore(tmp_path), metadata)
    assert result['state'] == ('running' if actual == 'expected' else 'unknown')
    assert result['process_identity_verified'] is (actual == 'expected')


def test_invalid_transport_is_reaped_and_buffer_cleared(monkeypatch):
    child = subprocess.Popen(
        [sys.executable, '-c', 'import os,time;os.write(1,bytes([255,10]));time.sleep(10)'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True, bufsize=0,
    )
    try:
        os.set_blocking(child.stdout.fileno(), False)
        monkeypatch.setattr(cua, '_process', child)
        monkeypatch.setattr(cua, '_read_buffer', bytearray())
        monkeypatch.setattr(cua, '_call_locked', lambda *a, **k: cua._wait_response(1, operation='type_keyboard', args={}, timeout=1, effectful=True))
        with pytest.raises(cua.CUABackendError) as error:
            cua.call('type_keyboard', {}, effectful=True)
        assert error.value.effect == 'outcome_unknown'
        assert cua._process is None
        assert not cua._read_buffer
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        child.stdin.close()
        child.stdout.close()
