from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_coding_tasks import _config, _git
from mcp4chatgpt.local_ops import _run_process
from mcp4chatgpt.workspace import coding_task_tool_api as api
from mcp4chatgpt.web_archive import _download_attachment


def test_sync_timeout_kills_descendant_holding_pipe(tmp_path):
    script = "import os,signal,time\np=os.fork()\nif p == 0:\n signal.signal(signal.SIGTERM, signal.SIG_IGN)\n time.sleep(15)\nelse:\n time.sleep(15)"
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _run_process([sys.executable, '-c', script], cwd=tmp_path, timeout_sec=1)
    assert time.monotonic() - start < 6


def test_strict_attachment_blocks_loopback(tmp_path, monkeypatch):
    monkeypatch.setenv('MCP_BROWSER_RESEARCH_STRICT_NETWORK', '1')
    monkeypatch.delenv('MCP_BROWSER_RESEARCH_ALLOW_HOSTS', raising=False)
    result = _download_attachment('http://127.0.0.1:1/x', tmp_path / 'x', referer='https://example.org', max_bytes=100)
    assert result['status'] == 'failed'
    assert not (tmp_path / 'x').exists()


def test_coding_task_reverts_and_replays_after_head_moves(tmp_path):
    c = _config(tmp_path)
    repo = c.allowed_roots[0] / 'repo'
    args = {'cwd': str(repo), 'task_id': 'review', 'paths': ['app.txt', 'other.txt']}
    scope = {'cwd': str(repo), 'task_id': 'review'}
    task = api.create(c, args, 'a')
    worktree = Path(task['path'])
    (worktree / 'app.txt').write_text('changed\n')
    api.validate(c, {**scope, 'operation_id': 'v1', 'command': 'true'}, 'a')
    (worktree / 'app.txt').write_text('initial\n')
    (worktree / 'other.txt').write_text('changed\n')
    validated = api.validate(c, {**scope, 'operation_id': 'v2', 'command': 'true'}, 'a')
    assert validated['validation']['validated']
    api.publish(c, scope, 'a')
    api.integrate(c, {**scope, 'expected_source_commit': task['base_commit']}, 'a')
    replay = api.create(c, args, 'a')
    assert replay['replayed'] and replay['base_commit'] == task['base_commit']
    assert (repo / 'app.txt').read_text() == 'initial\n'
    assert _git(repo, 'status', '--porcelain') == ''


def test_strict_attachment_checks_redirect_before_request(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    visited = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            visited.append(self.path)
            self.send_response(302)
            self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/private')
            self.end_headers()
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('MCP_BROWSER_RESEARCH_STRICT_NETWORK', '1')
    monkeypatch.setenv('MCP_BROWSER_RESEARCH_ALLOW_HOSTS', 'localhost')
    try:
        result = _download_attachment(f'http://localhost:{server.server_port}/public', tmp_path / 'x', referer='https://example.org', max_bytes=100)
        assert result['status'] == 'failed'
        assert visited == ['/public']
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_ignored_file_is_not_overwritten_on_integration(tmp_path):
    c = _config(tmp_path)
    repo = c.allowed_roots[0] / 'repo'
    (repo / '.git/info/exclude').write_text('notes.txt\n')
    (repo / 'notes.txt').write_text('original\n')
    args = {'cwd': str(repo), 'task_id': 'ignored', 'paths': ['notes.txt']}
    scope = {'cwd': str(repo), 'task_id': 'ignored'}
    task = api.create(c, args, 'a')
    worktree = Path(task['path'])
    (worktree / 'notes.txt').write_text('replacement\n')
    _git(worktree, 'add', '-f', 'notes.txt')
    api.validate(c, {**scope, 'operation_id': 'v', 'command': 'true'}, 'a')
    api.publish(c, scope, 'a')
    with pytest.raises(ValueError, match='coding_task_integration_conflict'):
        api.integrate(c, {**scope, 'expected_source_commit': task['base_commit']}, 'a')
    assert (repo / 'notes.txt').read_text() == 'original\n'
    assert _git(repo, 'rev-parse', 'HEAD') == task['base_commit']


def test_validation_gate_does_not_execute_before_marker_persists(tmp_path, monkeypatch):
    from mcp4chatgpt.workspace.worktree_store import WorktreeStore
    c = _config(tmp_path)
    repo = c.allowed_roots[0] / 'repo'
    scope = {'cwd': str(repo), 'task_id': 'gate'}
    task = api.create(c, {**scope, 'paths': ['app.txt']}, 'a')
    (Path(task['path']) / 'app.txt').write_text('candidate\n')
    effect = tmp_path / 'effect'
    original = WorktreeStore.write_task
    def crash(self, key, record):
        if record.get('state') == 'validation_running':
            raise SystemExit('injected crash before persisted marker')
        return original(self, key, record)
    with monkeypatch.context() as patch:
        patch.setattr(WorktreeStore, 'write_task', crash)
        with pytest.raises(SystemExit):
            api.validate(c, {**scope, 'operation_id': 'v', 'command': f'touch {effect}'}, 'a')
    time.sleep(0.3)
    assert not effect.exists()
    result = api.validate(c, {**scope, 'operation_id': 'v', 'command': f'touch {effect}'}, 'a')
    assert result['validation']['validated']
    assert effect.exists()


def test_audit_concurrent_rotation_retains_records(tmp_path):
    import gzip
    import json
    from concurrent.futures import ThreadPoolExecutor
    from mcp4chatgpt.audit import AuditLogger
    path = tmp_path / 'audit.jsonl'
    path.write_text('x' * (1024 * 1024) + '\n')
    logger = AuditLogger(path, rotate_bytes=1024 * 1024)
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda i: logger.log('review', index=i), range(60)))
    lines = path.read_text().splitlines()
    for archived in tmp_path.glob('audit.*.jsonl.gz'):
        with gzip.open(archived, 'rt') as fh:
            lines.extend(fh.read().splitlines())
    records = [json.loads(line) for line in lines if line.startswith('{')]
    assert sorted(item['index'] for item in records) == list(range(60))


def test_rename_requires_both_declared_paths(tmp_path):
    c = _config(tmp_path)
    repo = c.allowed_roots[0] / 'repo'
    _git(repo, 'config', 'diff.renames', 'true')
    scope = {'cwd': str(repo), 'task_id': 'rename'}
    task = api.create(c, {**scope, 'paths': ['new.txt']}, 'a')
    _git(Path(task['path']), 'mv', 'app.txt', 'new.txt')
    with pytest.raises(ValueError, match='coding_task_undeclared_changes'):
        api.validate(c, {**scope, 'operation_id': 'v', 'command': 'true'}, 'a')


def test_same_task_id_publishes_separate_client_refs(tmp_path):
    c = _config(tmp_path)
    repo = c.allowed_roots[0] / 'repo'
    scope = {'cwd': str(repo), 'task_id': 'same'}
    refs = []
    for principal in ('a', 'b'):
        task = api.create(c, {**scope, 'paths': ['app.txt']}, principal)
        (Path(task['path']) / 'app.txt').write_text(principal + '\n')
        api.validate(c, {**scope, 'operation_id': 'v', 'command': 'true'}, principal)
        refs.append(api.publish(c, scope, principal)['task_ref'])
    assert refs[0] != refs[1]
    assert _git(repo, 'rev-parse', refs[0]) != _git(repo, 'rev-parse', refs[1])


def test_legacy_unknown_validation_requires_explicit_confirmations(tmp_path):
    from mcp4chatgpt.workspace.worktree_store import WorktreeStore
    c = _config(tmp_path)
    repo = c.allowed_roots[0] / 'repo'
    scope = {'cwd': str(repo), 'task_id': 'legacy'}
    task = api.create(c, {**scope, 'paths': ['app.txt']}, 'a')
    store = WorktreeStore(c.data_dir / 'managed-worktrees')
    record = store.read_task(task['task_key'])
    record['validation_attempt'] = {'operation_id': 'old', 'state': 'running', 'fingerprint': 'old'}
    store.write_task(task['task_key'], record)
    args = {**scope, 'operation_id': 'old', 'resolution': 'abandon_unknown_process', 'confirm_effects_reviewed': True}
    with pytest.raises(ValueError, match='confirmation_invalid'):
        api.reconcile_validation(c, args, 'a')
    result = api.reconcile_validation(c, {**args, 'confirm_no_process_running': True}, 'a')
    assert result['reconciled'] and result['abandoned_attempt']['confirm_no_process_running']
    with pytest.raises(ValueError, match='validation_operation_abandoned'):
        api.validate(c, {**scope, 'operation_id': 'old', 'command': 'true'}, 'a')


@pytest.mark.parametrize("legacy", [False, True])
def test_create_retry_after_interrupted_task_record_and_head_advances(tmp_path, monkeypatch, legacy):
    from mcp4chatgpt.workspace.worktree_store import WorktreeStore
    c = _config(tmp_path)
    repo = c.allowed_roots[0] / 'repo'
    original_head = _git(repo, 'rev-parse', 'HEAD')
    scope = {'cwd': str(repo), 'task_id': 'intent', 'paths': ['app.txt']}
    with monkeypatch.context() as patch:
        patch.setattr(WorktreeStore, 'write_task', lambda *args: (_ for _ in ()).throw(OSError('injected')))
        with pytest.raises(OSError, match='injected'):
            api.create(c, scope, 'a')
    if legacy:
        intent, = (c.data_dir / 'managed-worktrees' / 'task-intents').glob('*.json')
        intent.unlink()
    (repo / 'other.txt').write_text('advanced\n')
    _git(repo, 'add', 'other.txt')
    _git(repo, 'commit', '-m', 'advance')
    if legacy:
        with pytest.raises(ValueError, match='idempotency_conflict'):
            api.create(c, scope, 'a')
        scope['base_ref'] = original_head
    task = api.create(c, scope, 'a')
    assert task['base_commit'] == original_head
    assert (Path(task['path']) / 'other.txt').read_text() == 'other\n'


@pytest.mark.parametrize("drift", [False, True])
def test_legacy_published_ref_migrates_on_integration(tmp_path, monkeypatch, drift):
    from mcp4chatgpt.workspace import coding_tasks
    from mcp4chatgpt.workspace.worktree_store import WorktreeStore
    c = _config(tmp_path)
    repo = c.allowed_roots[0] / 'repo'
    scope = {'cwd': str(repo), 'task_id': 'migrate'}
    task = api.create(c, {**scope, 'paths': ['app.txt']}, 'a')
    (Path(task['path']) / 'app.txt').write_text('candidate\n')
    validated = api.validate(c, {**scope, 'operation_id': 'v', 'command': 'true'}, 'a')
    candidate = validated['validation']['candidate_commit']
    old_ref = 'refs/mcp4chatgpt/tasks/migrate'
    _git(repo, 'update-ref', old_ref, candidate)
    store = WorktreeStore(c.data_dir / 'managed-worktrees')
    record = store.read_task(task['task_key'])
    record['task_ref'] = old_ref
    record['published_commit'] = candidate
    store.write_task(task['task_key'], record)
    if drift:
        run_git = coding_tasks._git_result

        def move_legacy_ref(cwd, *args, **kwargs):
            if args == ('update-ref', '--stdin'):
                _git(repo, 'update-ref', old_ref, task['base_commit'])
            return run_git(cwd, *args, **kwargs)

        monkeypatch.setattr(coding_tasks, '_git_result', move_legacy_ref)
        with pytest.raises(ValueError, match='coding_task_publish_conflict'):
            api.integrate(c, {**scope, 'expected_source_commit': task['base_commit']}, 'a')
        assert store.read_task(task['task_key'])['task_ref'] == old_ref
        assert coding_tasks._ref_target(repo, task['task_ref']) is None
        return
    integrated = api.integrate(c, {**scope, 'expected_source_commit': task['base_commit']}, 'a')
    assert integrated['integrated']
    assert integrated['task_ref'].startswith('refs/mcp4chatgpt/scoped-tasks/')
    assert _git(repo, 'rev-parse', old_ref) == candidate


def test_strict_attachment_refuses_rebound_dns_before_connect(tmp_path, monkeypatch):
    import socket
    monkeypatch.setenv('MCP_BROWSER_RESEARCH_STRICT_NETWORK', '1')
    monkeypatch.delenv('MCP_BROWSER_RESEARCH_ALLOW_HOSTS', raising=False)
    calls = []
    def resolve(*args, **kwargs):
        calls.append(args[0])
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 1234))]
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    result = _download_attachment('http://public.example/test', tmp_path / 'x', referer='https://example.org', max_bytes=100)
    assert result['status'] == 'failed'
    assert calls == ['public.example']
    assert not (tmp_path / 'x').exists()


def test_audit_failure_does_not_escape(tmp_path, monkeypatch, capsys):
    from mcp4chatgpt.audit import AuditLogger
    logger = AuditLogger(tmp_path / 'audit.jsonl')
    def fail():
        raise OSError('private details')
    monkeypatch.setattr(logger, '_rotate_if_needed', fail)
    logger.log('test')
    error = capsys.readouterr().err
    assert 'OSError' in error
    assert 'private details' not in error


def _kill_validation_worker(config, request, effect, phase):
    import signal
    from mcp4chatgpt.workspace.worktree_store import WorktreeStore

    write_task = WorktreeStore.write_task

    def checkpoint(store, key, record):
        if record.get('state') != 'validation_running':
            return write_task(store, key, record)
        if phase == 'before_marker':
            os.kill(os.getpid(), signal.SIGKILL)
        write_task(store, key, record)
        if phase == 'before_release':
            os.kill(os.getpid(), signal.SIGKILL)

    WorktreeStore.write_task = checkpoint
    if phase == 'after_release':
        write = os.write

        def release(fd, data):
            count = write(fd, data)
            if data == b'1':
                deadline = time.monotonic() + 5
                while not effect.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if effect.exists():
                    os.kill(os.getpid(), signal.SIGKILL)
            return count

        os.write = release
    api.validate(config, request, 'a')


@pytest.mark.parametrize('phase', ['before_marker', 'before_release', 'after_release'])
def test_validation_recovers_after_worker_sigkill(tmp_path, phase):
    import multiprocessing
    import shlex
    import signal
    from mcp4chatgpt.workspace.coding_tasks import _validation_group_alive

    config = _config(tmp_path)
    scope = {'cwd': str(config.allowed_roots[0] / 'repo'), 'task_id': 'killed'}
    task = api.create(config, {**scope, 'paths': ['app.txt']}, 'a')
    (Path(task['path']) / 'app.txt').write_text('candidate\n')
    effect = tmp_path / 'effect'
    request = {**scope, 'operation_id': 'v', 'command': f'printf x >> {shlex.quote(str(effect))}'}
    worker = multiprocessing.get_context('spawn').Process(
        target=_kill_validation_worker, args=(config, request, effect, phase),
    )
    worker.start()
    try:
        worker.join(15)
        assert worker.exitcode == -signal.SIGKILL
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join(5)

    if phase == 'before_marker':
        assert not effect.exists()
        assert api.validate(config, request, 'a')['validation']['validated']
        assert effect.read_text() == 'x'
        return

    with pytest.raises(ValueError, match='validation_uncertain'):
        api.validate(config, request, 'a')
    attempt = api.inspect(config, scope, 'a')['validation_attempt']
    deadline = time.monotonic() + 5
    while _validation_group_alive(attempt['pgid']) and time.monotonic() < deadline:
        time.sleep(0.02)
    result = api.reconcile_validation(config, {
        **scope, 'operation_id': 'v', 'resolution': 'abandon', 'confirm_effects_reviewed': True,
    }, 'a')
    assert result['reconciled']
    with pytest.raises(ValueError, match='validation_operation_abandoned'):
        api.validate(config, request, 'a')
    if phase == 'after_release':
        assert effect.read_text() == 'x'
    else:
        assert not effect.exists()
