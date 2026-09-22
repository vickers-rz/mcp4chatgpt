"""Owned, serial macOS helper process for native computer operations."""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import platform
import plistlib
import re
import selectors
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any


IO_TIMEOUT = 30.0
MAX_LINE = 12 * 1024 * 1024
HELPER_APP_NAME = "MCP4ChatGPT Computer Use"
HELPER_BUNDLE_ID = "uk.runzhe.mcp4chatgpt.computeruse"
HELPER_EXECUTABLE = "mcp4chatgpt-computer-use"
_lock = threading.RLock()
_process: subprocess.Popen[bytes] | None = None


class ComputerBackendError(RuntimeError):
    def __init__(self, code: str, effect: str = "not_started") -> None:
        self.code = code
        self.effect = effect
        super().__init__(code)


def _source() -> Path:
    return Path(__file__).resolve().parent / "native" / "ComputerUseHelper.swift"


def _codesign_identity() -> str:
    explicit = os.environ.get("MCP_COMPUTER_CODESIGN_IDENTITY", "").strip()
    if explicit:
        return explicit
    try:
        found = subprocess.run(
            ["security", "find-identity", "-v", "-p", "codesigning"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "-"
    match = re.search(r'^\s*\d+\)\s+([0-9A-F]{40})\s+"Apple Development:', found, re.MULTILINE)
    return match.group(1) if match else "-"


def _binary() -> Path:
    source = _source()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    root = Path.home() / "Library" / "Application Support" / "MCP4ChatGPT" / "ComputerUse"
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    root_stat = root.lstat()
    if root.is_symlink() or root_stat.st_uid != os.getuid() or root_stat.st_mode & 0o077:
        raise ComputerBackendError("helper_cache_permissions_invalid")

    app = root / f"{HELPER_APP_NAME}.app"
    binary = app / "Contents" / "MacOS" / HELPER_EXECUTABLE
    stamp = app / "Contents" / "Resources" / "source.sha256"
    if binary.exists() and stamp.exists() and stamp.read_text(encoding="utf-8").strip() == digest:
        binary_stat = binary.lstat()
        if binary.is_symlink() or not binary.is_file() or binary_stat.st_uid != os.getuid() or binary_stat.st_mode & 0o022:
            raise ComputerBackendError("helper_binary_permissions_invalid")
        return binary

    candidate = root / f".{HELPER_APP_NAME}-{uuid.uuid4().hex}.app"
    backup = root / f".{HELPER_APP_NAME}-{uuid.uuid4().hex}.old"
    contents = candidate / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    candidate_binary = macos / HELPER_EXECUTABLE
    try:
        macos.mkdir(parents=True, mode=0o755)
        resources.mkdir(parents=True, mode=0o755)
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleDevelopmentRegion": "en",
                    "CFBundleExecutable": HELPER_EXECUTABLE,
                    "CFBundleIdentifier": HELPER_BUNDLE_ID,
                    "CFBundleInfoDictionaryVersion": "6.0",
                    "CFBundleName": HELPER_APP_NAME,
                    "CFBundlePackageType": "APPL",
                    "CFBundleShortVersionString": "1.0",
                    "CFBundleVersion": "1",
                    "LSUIElement": True,
                },
                handle,
            )
        subprocess.run(
            [
                "xcrun",
                "swiftc",
                "-O",
                "-target",
                "arm64-apple-macosx14.0" if platform.machine() == "arm64" else "x86_64-apple-macosx14.0",
                str(source),
                "-o",
                str(candidate_binary),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        candidate_binary.chmod(0o755)
        (resources / "source.sha256").write_text(digest + "\n", encoding="utf-8")
        identity = _codesign_identity()
        subprocess.run(
            ["codesign", "--force", "--sign", identity, "--timestamp=none", str(candidate)],
            check=True,
            capture_output=True,
            timeout=60,
        )
        subprocess.run(
            ["codesign", "--verify", "--strict", "--verbose=2", str(candidate)],
            check=True,
            capture_output=True,
            timeout=30,
        )
        if app.exists():
            app.replace(backup)
        candidate.replace(app)
        shutil.rmtree(backup, ignore_errors=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        if backup.exists() and not app.exists():
            backup.replace(app)
        raise ComputerBackendError("helper_compile_failed") from exc
    finally:
        shutil.rmtree(candidate, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
    return binary


def stop() -> None:
    global _process
    with _lock:
        proc, _process = _process, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        for stream in (proc.stdin, proc.stdout):
            if stream:
                stream.close()


atexit.register(stop)


def call(operation: str, args: dict[str, Any], *, effectful: bool = False) -> dict[str, Any]:
    """Bound both write and read; never replay an uncertain dispatched operation."""
    global _process
    if platform.system() != "Darwin":
        raise ComputerBackendError("unsupported_platform")
    request_id = uuid.uuid4().hex
    payload = json.dumps({"request_id": request_id, "operation": operation, "args": args}, ensure_ascii=False).encode() + b"\n"
    if len(payload) > 100_000:
        raise ComputerBackendError("request_too_large")
    if not _lock.acquire(timeout=IO_TIMEOUT):
        raise ComputerBackendError("helper_busy")
    dispatched = False

    def failure(code: str) -> ComputerBackendError:
        stop()
        return ComputerBackendError(code, "outcome_unknown" if effectful and dispatched else "not_started")

    try:
        if _process is None or _process.poll() is not None:
            stop()
            try:
                _process = subprocess.Popen([str(_binary())], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            except OSError as exc:
                raise ComputerBackendError("helper_start_failed") from exc
        proc = _process
        assert proc.stdin is not None and proc.stdout is not None
        deadline = time.monotonic() + IO_TIMEOUT
        chunks = bytearray()
        try:
            os.set_blocking(proc.stdin.fileno(), False)
            os.set_blocking(proc.stdout.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdin, selectors.EVENT_WRITE)
                sent = 0
                while sent < len(payload):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise failure("helper_timeout")
                    try:
                        # Even a failed write can be partial: classify conservatively.
                        dispatched = True
                        count = os.write(proc.stdin.fileno(), payload[sent:])
                    except BlockingIOError:
                        continue
                    if count <= 0:
                        raise failure("helper_disconnected")
                    sent += count
                selector.unregister(proc.stdin)
                selector.register(proc.stdout, selectors.EVENT_READ)
                while b"\n" not in chunks:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise failure("helper_timeout")
                    try:
                        part = os.read(proc.stdout.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not part:
                        raise failure("helper_protocol_error")
                    chunks.extend(part)
                    if len(chunks) > MAX_LINE:
                        raise failure("helper_protocol_error")
        except OSError as exc:
            raise failure("helper_disconnected") from exc
        if not chunks.endswith(b"\n"):
            raise failure("helper_protocol_error")
        try:
            response = json.loads(chunks)
        except (ValueError, UnicodeError) as exc:
            raise failure("helper_protocol_error") from exc
        if not isinstance(response, dict) or response.get("request_id") != request_id:
            raise failure("helper_protocol_error")
        if response.get("ok") is False:
            effect = response.get("effect")
            error = response.get("error")
            # Only the helper's explicit, well-formed failure is a no-effect proof.
            if effect not in {"not_started", "outcome_unknown"} or not isinstance(error, str) or not re.fullmatch(r"[a-z0-9_]{1,100}", error):
                raise failure("helper_protocol_error")
            raise ComputerBackendError(error, effect)
        if response.get("ok") is not True or not isinstance(response.get("result"), dict):
            raise failure("helper_protocol_error")
        return response["result"]
    finally:
        _lock.release()
