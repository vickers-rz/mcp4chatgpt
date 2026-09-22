"""Process lifecycle helpers for durable local jobs."""

from __future__ import annotations

import os
import signal
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


def process_group_alive(pgid: int | None) -> bool:
    if pgid is None or int(pgid) <= 0:
        return False
    pgid = int(pgid)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Some sandboxed macOS execution contexts reject group signalling even
        # when the group leader is our own child. Since durable jobs create a
        # new session with PGID == child PID, the leader remains a useful
        # liveness fallback.
        return pid_alive(pgid)
    return True


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
        # Restricted macOS runners can deny killpg() while still allowing a
        # signal to the group leader. Durable jobs create the child with
        # start_new_session=True, so PGID == child PID.
        return signal_pid(pgid, sig)
    return True


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
