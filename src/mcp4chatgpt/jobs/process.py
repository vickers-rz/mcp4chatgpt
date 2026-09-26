"""Process lifecycle helpers for durable local jobs."""

from __future__ import annotations

import os
import signal
import subprocess
import time


def pid_alive(pid: int | None) -> bool:
    if pid is None or int(pid) <= 0:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_identity(pid: int | None) -> str | None:
    """Return an OS start-time/command identity suitable for stale-PID checks."""
    if pid is None or int(pid) <= 0:
        return None
    try:
        result = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "lstart=", "-o", "command="],
            capture_output=True, text=True, timeout=1.0, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value or None


def process_group_alive(pgid: int | None) -> bool:
    if pgid is None or int(pgid) <= 0:
        return False
    pgid = int(pgid)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return bool(_group_members(pgid))
    members = _group_members(pgid)
    return bool(members) if members else False


def signal_pid(pid: int | None, sig: int) -> bool:
    if pid is None or int(pid) <= 0:
        return False
    try:
        os.kill(int(pid), sig)
    except ProcessLookupError:
        return False
    return True


def signal_process_group(pgid: int | None, sig: int) -> bool:
    if pgid is None or int(pgid) <= 0:
        return False
    pgid = int(pgid)
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Some macOS app sandboxes deny killpg() while still allowing signals
        # to descendants. Enumerate only members of the unique child PGID.
        members = _group_members(pgid)
        if not members:
            return signal_pid(pgid, sig)
        signalled = False
        for pid in members:
            signalled = signal_pid(pid, sig) or signalled
        return signalled
    return True


def _group_members(pgid: int) -> list[int]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,pgid=,stat="], capture_output=True, text=True,
            timeout=1.0, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [pgid] if pid_alive(pgid) else []
    members: list[int] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 3:
            try:
                pid, group = map(int, fields[:2])
            except ValueError:
                continue
            if group == pgid and not fields[2].startswith("Z"):
                members.append(pid)
    return members


def terminate_process_group(
    pgid: int | None,
    *,
    grace_sec: float = 2.0,
) -> None:
    if pgid is None:
        return
    if not signal_process_group(pgid, signal.SIGTERM):
        return

    deadline = time.monotonic() + max(0.0, grace_sec)
    while time.monotonic() < deadline:
        if not process_group_alive(pgid):
            return
        time.sleep(0.05)

    signal_process_group(pgid, signal.SIGKILL)


def terminate_pid(
    pid: int | None,
    *,
    grace_sec: float = 2.0,
) -> None:
    if pid is None:
        return
    if not signal_pid(pid, signal.SIGTERM):
        return

    deadline = time.monotonic() + max(0.0, grace_sec)
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.05)

    signal_pid(pid, signal.SIGKILL)
