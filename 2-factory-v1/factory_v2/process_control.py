"""F5R2/R3: process group lifecycle — terminate entire descendant tree.

ProcessGroupHandle captures the PGID at spawn time (while proc.pid is
guaranteed alive). If the root exits before terminate() is called, the PGID
is still valid for os.killpg and all surviving descendants are killed.

Root cause of the original bug: terminate_process_group(proc) called
os.getpgid(proc.pid) at terminate time. If the root had already exited,
that raised ProcessLookupError → early return → descendants escaped SIGKILL.
"""
import os
import signal
import subprocess
import time
from dataclasses import dataclass


@dataclass
class ProcessGroupHandle:
    """Immutable (pid, pgid) pair captured at spawn time.

    Always use capture_pgid() right after Popen() — while proc.pid is
    guaranteed alive — then pass the handle to terminate_process_group().
    This decouples the PGID lookup from terminate time so root-exit-before-
    terminate is safe.
    """
    pid: int
    pgid: int


def capture_pgid(proc: subprocess.Popen) -> ProcessGroupHandle:
    """Capture (pid, pgid) right after Popen() while the root is alive.

    Falls back pgid=pid when os.getpgid raises (process already gone in a
    race — extremely unlikely right after Popen, but handled defensively).
    """
    pid = proc.pid
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        pgid = pid
    return ProcessGroupHandle(pid=pid, pgid=pgid)


def _group_alive(pgid: int) -> bool:
    """True if any process still belongs to pgid (signal 0 = existence check)."""
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def terminate_process_group(
    proc: "subprocess.Popen | ProcessGroupHandle",
    grace_s: float = 5.0,
) -> None:
    """Send SIGTERM to the process group, then SIGKILL if the group survives grace_s.

    Accepts either a ProcessGroupHandle (preferred — PGID captured at spawn)
    or a Popen object (backward compat — PGID fetched at call time, unsafe if
    the root has already exited).

    Falls back to single-pid kill when the process shares a PGID with the
    caller (start_new_session was not used) — killpg on the parent's own group
    would kill factoryd itself.
    """
    own_pgid = os.getpgrp()

    if isinstance(proc, ProcessGroupHandle):
        pgid = proc.pgid
        if pgid == own_pgid:
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            return
    else:
        # Popen backward-compat path — PGID looked up now; root may be gone.
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError):
            # Root already gone: we can't discover the PGID. Fall back to
            # single-process kill (best-effort; descendants may escape).
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError):
                pass
            return
        if pgid == own_pgid:
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError):
                pass
            return

    # Graceful: SIGTERM to the entire group.
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return  # group already empty

    # Wait up to grace_s for the ENTIRE group to exit (not just the root).
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return
        time.sleep(0.1)

    # Force: SIGKILL to entire group — catches survivors even if root is gone.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # group already empty
